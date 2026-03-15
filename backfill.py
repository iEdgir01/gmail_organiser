"""
backfill.py — Retroactively label all existing messages using headers.jsonl.

Uses the direct Gmail API (token.json) for batchModify — 1000 IDs per call,
processes large mailboxes in a few minutes.

Tier logic (matches apply_filters.py):
  Tier 1 — add label only                              (leave in inbox)
  Tier 2 — add label + remove INBOX
  Tier 3 — add label + remove INBOX                    (leave as unread)
  Tier 4 — add label + remove INBOX + remove UNREAD

Usage:
    python backfill.py --dry-run        # count what would change, no API calls
    python backfill.py                  # apply all tiers
    python backfill.py --tier 2,3       # only move-out tiers (skip tier 1 & 4)
    python backfill.py --tier 4         # only unsubscribe queue
    python backfill.py --tier 1         # only add labels to important mail
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
    flat = [(pat, rule["label"], rule["tier"])
            for rule in rules for pat in rule["patterns"]]

    def classify(addr: str):
        for pat, label, tier in flat:
            if pat in addr:
                return label, tier
        return None

    return classify


# ── Label ID lookup ───────────────────────────────────────────────────────────
def get_label_ids(session, token_mgr) -> dict[str, str]:
    data = api_get(session, token_mgr, "labels")
    return {lbl["name"]: lbl["id"] for lbl in data.get("labels", [])}


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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tier",    default="1,2,3,4",
                        help="Comma-separated tiers to run (default: 1,2,3,4)")
    args      = parser.parse_args()
    run_tiers = {int(t) for t in args.tier.split(",")}

    for f in [RULES_FILE, HDR_FILE]:
        if not f.exists():
            print(f"ERROR: {f.name} not found.")
            if f == RULES_FILE:
                print("  Run  python analyze.py  first.")
            else:
                print("  Run  python fetch.py  first.")
            return

    token_mgr = load_token()
    session   = requests.Session()

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
        addr   = extract_addr(msg.get("from", ""))
        result = classify(addr)
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
        print(f"\nWARNING: {len(missing)} labels missing — run apply_filters.py first:")
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
