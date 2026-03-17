"""
apply_labels.py — Create Gmail labels via direct Gmail API.

Reads output/filter_rules.json produced by auto_classify.py (AI classifier).

This script ONLY creates labels (and optionally colours them).
Filter rules are managed separately:
  1. python export_filters_xml.py        — generate output/gmail_filters.xml
  2. Gmail Settings → Filters and Blocked Addresses → Import filters

Usage:
    python apply_labels.py --dry-run         # preview labels that would be created
    python apply_labels.py                   # create / ensure all labels exist
    python apply_labels.py --update-colors   # patch colours onto existing labels
"""

import json, argparse, time, sys, threading
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE       = Path(__file__).parent
RULES_FILE = BASE / "output" / "filter_rules.json"
TOKEN_FILE = BASE / "data"   / "token.json"
SCOPES     = ["https://www.googleapis.com/auth/gmail.modify",
              "https://www.googleapis.com/auth/gmail.settings.basic"]
API_BASE   = "https://www.googleapis.com/gmail/v1/users/me"


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
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type":  "application/json"}


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
        print(f"  API GET {path} failed: {r.status_code} {r.text[:200]}")
        break
    return {}


def api_post(session, token_mgr, path, body):
    url = f"{API_BASE}/{path}"
    for attempt in range(4):
        r = session.post(url, headers=token_mgr.headers, json=body, timeout=30)
        if r.status_code in (200, 201):
            return r.json()
        if r.status_code == 401:
            with token_mgr._lock:
                token_mgr._creds.expiry = None
            continue
        if r.status_code in (429, 500, 502, 503):
            print(f"  API POST {path} rate-limited/error: {r.status_code} — waiting {2**attempt}s")
            time.sleep(2 ** attempt)
            continue
        print(f"  API POST {path} failed: {r.status_code} {r.text[:300]}")
        break
    print(f"  API POST {path} — all retries exhausted")
    return {}



def api_patch(session, token_mgr, path, body):
    url = f"{API_BASE}/{path}"
    for attempt in range(4):
        r = session.patch(url, headers=token_mgr.headers, json=body, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            with token_mgr._lock:
                token_mgr._creds.expiry = None
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        print(f"  API PATCH {path} failed: {r.status_code} {r.text[:200]}")
        break
    return {}


# ── Label colours ─────────────────────────────────────────────────────────────
# backgroundColor / textColor — values must be from Gmail's allowed palette.
LABEL_COLORS: dict[str, tuple[str, str]] = {
    "Finance":           ("#16a766", "#ffffff"),  # green
    "Insurance":         ("#0b804b", "#ffffff"),  # dark green
    "Transport":         ("#3c78d8", "#ffffff"),  # blue
    "Dev":               ("#8e63ce", "#ffffff"),  # purple
    "Gaming":            ("#e66550", "#ffffff"),  # red-orange
    "Shopping":          ("#eaa041", "#ffffff"),  # amber
    "Lifestyle":         ("#d5ae49", "#ffffff"),  # gold
    "Entertainment":     ("#e07798", "#ffffff"),  # flamingo
    "Jobs":              ("#2a9c68", "#ffffff"),  # teal
    "Social":            ("#4a86e8", "#ffffff"),  # sky blue
    "Accounts":          ("#464646", "#ffffff"),  # charcoal
    "Property":          ("#cf8933", "#ffffff"),  # brown-gold
    "Personal":          ("#f691b3", "#000000"),  # pink
    "Promotions":        ("#cccccc", "#000000"),  # light grey
    "System":            ("#999999", "#000000"),  # grey
    "Unsubscribe Queue": ("#cc3a21", "#ffffff"),  # red
    "Technical":         ("#a479e2", "#ffffff"),  # lavender
    "Business":          ("#1c4587", "#ffffff"),  # navy blue
    "Travel":            ("#0d3472", "#ffffff"),  # dark blue
}


def label_color(name: str) -> dict | None:
    """Return a Gmail API color body for the label, keyed by top-level category."""
    top = name.split("/")[0]
    pair = LABEL_COLORS.get(top)
    if pair:
        return {"backgroundColor": pair[0], "textColor": pair[1]}
    return None


# ── Label management ──────────────────────────────────────────────────────────
def get_existing_labels(session, token_mgr) -> dict[str, str]:
    data = api_get(session, token_mgr, "labels")
    return {lbl["name"]: lbl["id"] for lbl in data.get("labels", [])}


def update_label_colors(session, token_mgr, dry_run: bool):
    """Patch color onto every existing user label that has a known category color."""
    data   = api_get(session, token_mgr, "labels")
    labels = [l for l in data.get("labels", []) if l.get("type") == "user"]
    print(f"  {len(labels)} user label(s) found.")
    updated = skipped = 0
    for lbl in sorted(labels, key=lambda l: l["name"]):
        color = label_color(lbl["name"])
        if not color:
            skipped += 1
            continue
        if dry_run:
            print(f"  [DRY RUN] Would colour: {lbl['name']}")
            updated += 1
            continue
        result = api_patch(session, token_mgr, f"labels/{lbl['id']}",
                           {"color": color})
        if result.get("id"):
            print(f"  Coloured: {lbl['name']}")
            updated += 1
        else:
            print(f"  WARNING: failed to colour: {lbl['name']}")
        time.sleep(0.05)
    print(f"  {updated} label(s) coloured, {skipped} skipped (no category match).")


def ensure_label(session, token_mgr, name: str,
                 existing: dict[str, str], dry_run: bool) -> str | None:
    """Create label (and any parent labels) if not present."""
    if name in existing:
        return existing[name]

    # Ensure parent exists first for nested labels (e.g. Finance/Discovery Bank)
    parts = name.split("/")
    if len(parts) > 1:
        parent = "/".join(parts[:-1])
        ensure_label(session, token_mgr, parent, existing, dry_run)

    if dry_run:
        print(f"  [DRY RUN] Would create label: {name}")
        return None

    body: dict = {
        "name":                   name,
        "labelListVisibility":    "labelShow",
        "messageListVisibility":  "show",
    }
    color = label_color(name)
    if color:
        body["color"] = color
    result = api_post(session, token_mgr, "labels", body)
    lid = result.get("id")
    if lid:
        existing[name] = lid
        print(f"  Created label: {name}  ({lid})")
    else:
        print(f"  WARNING: failed to create label: {name}  response: {result}")
    return lid


# (Filter creation removed — filters are managed via XML import.
#  Run export_filters_xml.py to generate output/gmail_filters.xml, then import
#  it in Gmail Settings → Filters and Blocked Addresses → Import filters.)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log_path = BASE / "output" / "apply_labels.log"
    log_fh   = open(log_path, "w", encoding="utf-8")

    class Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, data):
            for s in self.streams: s.write(data)
        def flush(self):
            for s in self.streams: s.flush()

    sys.stdout = Tee(sys.stdout, log_fh)
    print(f"Log: {log_path}\n")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",       action="store_true",
                        help="Preview labels that would be created, no API calls")
    parser.add_argument("--update-colors", action="store_true",
                        help="Patch colours onto all existing labels and exit")
    args = parser.parse_args()

    if not RULES_FILE.exists():
        print(f"ERROR: {RULES_FILE} not found. Run  python auto_classify.py  first.")
        return

    rules_data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    rules      = rules_data["rules"]
    total_msgs = rules_data.get("total_messages", "?")

    print(f"Loaded {len(rules)} rules  (from {total_msgs:,} messages analysed)")
    if args.dry_run:
        print("DRY RUN — no changes will be made\n")

    token_mgr = load_token()
    session   = requests.Session()

    # ── Colour-only mode ──────────────────────────────────────────────────
    if args.update_colors:
        print("\n── Updating label colours ──")
        update_label_colors(session, token_mgr, args.dry_run)
        print("\nDone.")
        return

    # ── Labels ────────────────────────────────────────────────────────────
    print("\nFetching existing labels...")
    existing = get_existing_labels(session, token_mgr)
    print(f"  {len(existing)} labels already exist.")

    print("\n── Creating labels ──")
    for name in sorted(set(r["label"] for r in rules)):
        ensure_label(session, token_mgr, name, existing, args.dry_run)

    verdict = "DRY RUN complete." if args.dry_run else "Done!"
    print(f"\n{verdict}")
    if not args.dry_run:
        print("\nNext steps:")
        print("  1. python export_filters_xml.py        # generate output/gmail_filters.xml")
        print("  2. Gmail Settings → Filters and Blocked Addresses → Import filters")
        print("  3. python backfill.py --dry-run        # preview")
        print("  4. python backfill.py                  # label existing emails")


if __name__ == "__main__":
    main()
