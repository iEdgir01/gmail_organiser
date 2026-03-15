"""
mark_read.py — Bulk mark all unread messages as read.

Uses the direct Gmail API (token.json) for speed — processes large mailboxes
in ~1 minute via batchModify (1000 IDs per call).

Optionally scope to inbox-only, or a specific label.

Usage:
    python mark_read.py                    # mark everything as read
    python mark_read.py --inbox-only       # only inbox messages
    python mark_read.py --label "Finance"  # only a specific label
    python mark_read.py --dry-run          # count only, no changes
"""

import sys, json, time, argparse, threading
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE       = Path(__file__).parent
TOKEN_FILE = BASE / "data" / "token.json"
SCOPES     = ["https://www.googleapis.com/auth/gmail.modify"]
API_BASE   = "https://www.googleapis.com/gmail/v1/users/me"
BATCH_SIZE = 1000
PAGE_SIZE  = 500


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


def load_token():
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
        r = session.post(url, headers={**token_mgr.headers,
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


# ── Core logic ────────────────────────────────────────────────────────────────
def collect_unread_ids(session, token_mgr, inbox_only, label):
    """Paginate through all UNREAD messages and return their IDs."""
    query = "is:unread"
    if inbox_only:
        query += " in:inbox"
    if label:
        query += f" label:{label}"

    print(f"  Query: {query}")
    all_ids = []
    params  = {"maxResults": PAGE_SIZE, "q": query}

    while True:
        data = api_get(session, token_mgr, "messages", params)
        msgs = data.get("messages", [])
        all_ids += [m["id"] for m in msgs]
        sys.stdout.write(f"  Found {len(all_ids):,} unread so far...\r")
        sys.stdout.flush()
        nxt = data.get("nextPageToken")
        if not nxt:
            break
        params = {"maxResults": PAGE_SIZE, "q": query, "pageToken": nxt}

    print(f"  Found {len(all_ids):,} unread messages.          ")
    return all_ids


def batch_mark_read(session, token_mgr, ids, dry_run):
    total   = len(ids)
    batches = [ids[i:i+BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    done    = 0

    for i, batch in enumerate(batches, 1):
        if not dry_run:
            api_post(session, token_mgr, "messages/batchModify", {
                "ids":            batch,
                "removeLabelIds": ["UNREAD"],
            })
        done += len(batch)
        pct = done / total * 100
        sys.stdout.write(f"  Batch {i}/{len(batches)}  —  {done:,}/{total:,}  ({pct:.0f}%)\r")
        sys.stdout.flush()
        if not dry_run:
            time.sleep(0.1)   # gentle rate limit

    print(f"\n  {'[DRY RUN] Would mark' if dry_run else 'Marked'} {total:,} messages as read.")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox-only", action="store_true",
                        help="Only mark inbox messages as read")
    parser.add_argument("--label",      default="",
                        help="Only mark messages with this label as read")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Count only — no changes made")
    args = parser.parse_args()

    token_mgr = load_token()
    session   = requests.Session()

    print("\nSearching for unread messages...")
    ids = collect_unread_ids(session, token_mgr, args.inbox_only, args.label)

    if not ids:
        print("  Nothing to do — no unread messages found.")
        return

    if args.dry_run:
        print(f"\n[DRY RUN] {len(ids):,} messages would be marked as read.")
        return

    print(f"\nMarking {len(ids):,} messages as read...")
    batch_mark_read(session, token_mgr, ids, dry_run=False)
    print("\nDone.")


if __name__ == "__main__":
    main()
