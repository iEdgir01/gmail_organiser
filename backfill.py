"""
backfill.py — Retroactively label all existing messages using headers.jsonl.

Uses the direct Gmail API (token.json) for batchModify — 1000 IDs per call,
processes large mailboxes in a few minutes.

Tier logic (matches apply_labels.py):
  Tier 1 — add label only                              (leave in inbox)
  Tier 2 — add label + remove INBOX
  Tier 3 — add label + remove INBOX                    (leave as unread)
  Tier 4 — add label (Unsubscribe Queue) + remove INBOX + remove UNREAD

Usage:
    python backfill.py --dry-run           # count what would change, no API calls
    python backfill.py                     # apply all tiers
    python backfill.py --tier 2,3          # only move-out tiers (skip tier 1)
    python backfill.py --tier 4            # only unsubscribe queue
    python backfill.py --tier 1            # only add labels to important mail
    python backfill.py --cleanup-labels    # delete empty labels not in filter_rules.json
    python backfill.py --cleanup-labels --dry-run  # preview which labels would be deleted
"""

import json, argparse, time, sys, re, threading
from pathlib import Path
from collections import defaultdict

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE       = Path(__file__).parent
RULES_FILE = BASE / "output" / "filter_rules.json"
HDR_FILE   = BASE / "data"   / "headers.jsonl"
TOKEN_FILE = BASE / "data"   / "token.json"
SCOPES     = ["https://www.googleapis.com/auth/gmail.modify"]
API_BASE   = "https://www.googleapis.com/gmail/v1/users/me"
BATCH_SIZE = 1000


# ── Auth ──────────────────────────────────────────────────────────────────────
class TokenManager:
    def __init__(self, creds):
        self._creds = creds
        self._lock  = threading.Lock()

    @property
    def token(self):
        with self._lock:
            if not self._creds.valid:
                self._creds.refresh(Request())
            return self._creds.token

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}


def load_token() -> TokenManager:
    if not TOKEN_FILE.exists():
        print("ERROR: data/token.json not found. Run  python auth_setup.py  first.")
        sys.exit(1)
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return TokenManager(creds)


# ── API helpers ───────────────────────────────────────────────────────────────
def api_get(session, token_mgr, path, params=None):
    url = f"{API_BASE}/{path}"
    for attempt in range(4):
        r = session.get(url, headers=token_mgr.headers, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            with token_mgr._lock:
                token_mgr._creds.expiry = None
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        break
    return {}


def api_post(session, token_mgr, path, body):
    url = f"{API_BASE}/{path}"
    for attempt in range(4):
        r = session.post(url,
                         headers={**token_mgr.headers,
                                  "Content-Type": "application/json"},
                         json=body, timeout=30)
        if r.status_code in (200, 204):
            return True
        if r.status_code == 401:
            with token_mgr._lock:
                token_mgr._creds.expiry = None
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        break
    return False


# ── Classification ────────────────────────────────────────────────────────────
def extract_addr(frm: str) -> str:
    m = re.search(r"<([^>]+)>", frm)
    addr = m.group(1) if m else frm
    return addr.lower().strip()


def build_classifier(rules: list[dict]):
    """
    Returns a classify(addr, subject) function that applies two-pass matching:

      Pass 1 — subject-constrained rules: addr pattern AND a subject keyword must
               match.  These rules have a "subject_patterns" list in the JSON.
               Example: Finance/Bills for vodacom.co.za only fires when the subject
               contains "invoice", "bill", "receipt", etc.

      Pass 2 — unconstrained rules: addr pattern match only (no subject check).

    Priority order ensures that broad domain patterns with subject constraints
    never steal promotional emails that should be caught by a more-specific
    unconstrained rule (e.g. noreply@vodacom.co.za → Unsubscribe Queue).
    """
    # Split into two lists for the two-pass check
    constrained:   list[tuple] = []  # (pat, label, tier, subj_kw_tuple)
    unconstrained: list[tuple] = []  # (pat, label, tier)

    for rule in rules:
        label    = rule["label"]
        tier     = rule["tier"]
        subj_kw  = tuple(kw.lower() for kw in rule.get("subject_patterns") or [])
        for pat in rule["patterns"]:
            if subj_kw:
                constrained.append((pat, label, tier, subj_kw))
            else:
                unconstrained.append((pat, label, tier))

    def classify(addr: str, subject: str = ""):
        subj_lower = subject.lower()

        # Pass 1: subject-constrained — both addr and subject must match
        for pat, label, tier, subj_kw in constrained:
            if pat in addr and any(kw in subj_lower for kw in subj_kw):
                return label, tier

        # Pass 2: unconstrained — addr match only
        for pat, label, tier in unconstrained:
            if pat in addr:
                return label, tier

        return None

    return classify


# ── Label ID lookup ───────────────────────────────────────────────────────────
def get_label_ids(session, token_mgr) -> dict[str, str]:
    data = api_get(session, token_mgr, "labels")
    return {lbl["name"]: lbl["id"] for lbl in data.get("labels", [])}


# ── Label cleanup ─────────────────────────────────────────────────────────────
# System labels Gmail creates internally — never touch these
SYSTEM_LABELS = {
    "INBOX", "SENT", "TRASH", "SPAM", "STARRED", "IMPORTANT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS", "CHAT", "DRAFT", "UNREAD",
}

def cleanup_labels(session, token_mgr, active_labels: set[str], dry_run: bool):
    """
    Delete user-created labels that:
      - are NOT in active_labels (the current filter_rules.json set)
      - have zero messages (messagesTotal == 0)
    System labels are always skipped.
    """
    data = api_get(session, token_mgr, "labels")
    all_labels = [l for l in data.get("labels", [])
                  if l.get("type") == "user" and l["name"] not in SYSTEM_LABELS]

    to_delete = []
    keep      = []

    for lbl in all_labels:
        name = lbl["name"]
        if name in active_labels:
            keep.append(name)
            continue
        # Fetch label detail to get message count
        detail = api_get(session, token_mgr, f"labels/{lbl['id']}")
        msg_count = detail.get("messagesTotal", 0)
        if msg_count == 0:
            to_delete.append((name, lbl["id"]))
        else:
            print(f"  SKIP  {name}  ({msg_count} messages — not empty)")

    if not to_delete:
        print("  No empty orphan labels found.")
        return

    print(f"\n  {'Would delete' if dry_run else 'Deleting'} {len(to_delete)} empty label(s):")
    for name, lid in sorted(to_delete):
        print(f"    {name}")
        if not dry_run:
            url = f"{API_BASE}/labels/{lid}"
            for attempt in range(3):
                r = session.delete(url, headers={"Authorization": f"Bearer {token_mgr.token}"},
                                   timeout=30)
                if r.status_code in (200, 204):
                    break
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(2 ** attempt)
                    continue
                print(f"    WARNING: delete failed ({r.status_code}): {name}")
                break
            time.sleep(0.1)

    if not dry_run:
        print(f"\n  Deleted {len(to_delete)} label(s).")


# ── Batch modify ──────────────────────────────────────────────────────────────
def batch_modify(session, token_mgr, msg_ids: list[str],
                 add_ids: list[str], remove_ids: list[str],
                 desc: str, dry_run: bool):
    total   = len(msg_ids)
    batches = [msg_ids[i:i+BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]

    if dry_run:
        print(f"  [DRY RUN] {total:>7,} msgs  →  {desc}")
        return

    for idx, batch in enumerate(batches, 1):
        body = {"ids": batch}
        if add_ids:
            body["addLabelIds"]    = add_ids
        if remove_ids:
            body["removeLabelIds"] = remove_ids
        ok = api_post(session, token_mgr, "messages/batchModify", body)
        sys.stdout.write(
            f"  Batch {idx}/{len(batches)} ({len(batch)} msgs): {desc}"
            f"{'  ✓' if ok else '  ✗'}\r"
        )
        sys.stdout.flush()
        time.sleep(0.1)

    print(f"  Done: {total:,} msgs — {desc}            ")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--tier",           default="1,2,3,4",
                        help="Comma-separated tiers to run (default: 1,2,3,4)")
    parser.add_argument("--cleanup-labels", action="store_true",
                        help="Delete empty labels not present in filter_rules.json")
    args      = parser.parse_args()
    run_tiers = {int(t) for t in args.tier.split(",")}

    for f in [RULES_FILE, HDR_FILE]:
        if not f.exists():
            print(f"ERROR: {f.name} not found.")
            return

    token_mgr = load_token()
    session   = requests.Session()

    # ── Label cleanup (optional, runs before backfill) ────────────────────
    if args.cleanup_labels:
        rules_data    = json.loads(RULES_FILE.read_text(encoding="utf-8"))
        active_labels = {r["label"] for r in rules_data["rules"]}
        print("\n── Cleaning up empty orphan labels ──")
        if args.dry_run:
            print("DRY RUN — no labels will be deleted\n")
        cleanup_labels(session, token_mgr, active_labels, args.dry_run)
        print()

    # Load rules
    rules_data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    classify   = build_classifier(rules_data["rules"])

    # Load headers
    print(f"Loading {HDR_FILE.name}...")
    msgs = []
    with open(HDR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    pass
    print(f"  {len(msgs):,} messages loaded.")

    # Classify → group by (label, tier)
    groups: dict = defaultdict(list)
    skipped = 0
    for msg in msgs:
        addr    = extract_addr(msg.get("from", ""))
        subject = msg.get("subject", "")
        result  = classify(addr, subject)
        if result:
            label, tier = result
            if tier in run_tiers:
                groups[(label, tier)].append(msg["id"])
        else:
            skipped += 1

    total_to_change = sum(len(v) for v in groups.values())
    print(f"  {total_to_change:,} to modify  |  {skipped:,} unclassified/skipped\n")

    if args.dry_run:
        print("DRY RUN — changes that would be applied:\n")
        TIER_NAMES = {
            1: "keep inbox",
            2: "skip inbox",
            3: "skip inbox (unread)",
            4: "Unsubscribe Queue",
        }
        for (label, tier), ids in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
            print(f"  {len(ids):>7,}  [{TIER_NAMES[tier]}]  {label}")
        print(f"\n  Total: {total_to_change:,} messages would be modified.")
        return

    # Get label IDs via direct API
    print("Fetching label IDs...")
    label_map = get_label_ids(session, token_mgr)
    missing   = {lbl for (lbl, _) in groups if lbl not in label_map}
    if missing:
        print(f"\nWARNING: {len(missing)} labels missing — run apply_labels.py first:")
        for m in sorted(missing):
            print(f"  {m}")
        print("Aborting.")
        return

    TIER_HEADERS = {
        1: "── TIER 1 — IMPORTANT (add label, keep inbox) ──",
        2: "── TIER 2 — USEFUL (add label, skip inbox) ──",
        3: "── TIER 3 — LOW NOISE (add label, skip inbox, leave unread) ──",
        4: "── TIER 4 — UNSUBSCRIBE (add label, skip inbox, mark read) ──",
    }
    current_tier = None

    for (label, tier), ids in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        if tier != current_tier:
            print(f"\n{TIER_HEADERS[tier]}")
            current_tier = tier

        lid        = label_map[label]
        add_ids    = [lid]
        remove_ids = []
        if tier >= 2:
            remove_ids.append("INBOX")
        if tier == 4:
            remove_ids.append("UNREAD")

        batch_modify(session, token_mgr, ids, add_ids, remove_ids,
                     f"{label} ({len(ids):,} msgs)", dry_run=False)

    print(f"\nBackfill complete — {total_to_change:,} messages updated.")


if __name__ == "__main__":
    main()
