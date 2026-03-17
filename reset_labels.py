"""
reset_labels.py — Full mailbox reset: remove all user labels from every
message, restore INBOX to all messages, then delete all user labels.

Run this before a clean recalibration to start with a blank slate.

Steps performed:
  1. Fetch all user-created label IDs
  2. Fetch all message IDs in the mailbox (~50k, paginated)
  3. batchModify all messages: add INBOX, remove every user label
  4. Delete all user labels (now empty)

Usage:
    python reset_labels.py --dry-run   # show counts, no changes
    python reset_labels.py             # perform full reset
"""

import sys, time, argparse, threading
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE       = Path(__file__).parent
TOKEN_FILE = BASE / "data" / "token.json"
SCOPES     = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
API_BASE   = "https://www.googleapis.com/gmail/v1/users/me"
BATCH_SIZE = 1000

SYSTEM_LABELS = {
    "INBOX", "SENT", "TRASH", "SPAM", "STARRED", "IMPORTANT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS", "CHAT", "DRAFT", "UNREAD",
}


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
        print(f"  GET {path} → {r.status_code}: {r.text[:200]}")
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
        print(f"  POST {path} → {r.status_code}: {r.text[:200]}")
        break
    return False


def api_delete(session, token_mgr, path):
    url = f"{API_BASE}/{path}"
    for attempt in range(3):
        r = session.delete(url, headers=token_mgr.headers, timeout=30)
        if r.status_code in (200, 204):
            return True
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        print(f"  DELETE {path} → {r.status_code}")
        break
    return False


# ── Step 1: get all user label IDs ───────────────────────────────────────────
def get_user_labels(session, token_mgr) -> list[dict]:
    data = api_get(session, token_mgr, "labels")
    return [l for l in data.get("labels", [])
            if l.get("type") == "user" and l["name"] not in SYSTEM_LABELS]


# ── Step 2: fetch all message IDs ────────────────────────────────────────────
def fetch_all_message_ids(session, token_mgr) -> list[str]:
    ids   = []
    token = None
    while True:
        params = {"maxResults": 500}
        if token:
            params["pageToken"] = token
        data = api_get(session, token_mgr, "messages", params)
        msgs = data.get("messages", [])
        ids.extend(m["id"] for m in msgs)
        sys.stdout.write(f"\r  Fetched {len(ids):,} message IDs...")
        sys.stdout.flush()
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(0.05)
    print()
    return ids


# ── Step 3: strip all user labels + restore INBOX ────────────────────────────
LABEL_CHUNK = 40   # Gmail rejects requests with too many label IDs at once

def restore_inbox(session, token_mgr, msg_ids: list[str],
                  user_label_ids: list[str], dry_run: bool):
    total        = len(msg_ids)
    msg_batches  = [msg_ids[i:i+BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    label_chunks = [user_label_ids[i:i+LABEL_CHUNK]
                    for i in range(0, len(user_label_ids), LABEL_CHUNK)]

    if dry_run:
        print(f"  [DRY RUN] Would batchModify {total:,} messages:")
        print(f"    Pass 1: addLabelIds [INBOX] + remove first {LABEL_CHUNK} labels")
        print(f"    Pass 2-{len(label_chunks)}: remove remaining labels in chunks of {LABEL_CHUNK}")
        return

    total_passes = len(label_chunks)
    for pass_idx, label_chunk in enumerate(label_chunks, 1):
        add    = ["INBOX"] if pass_idx == 1 else []
        remove = label_chunk
        for idx, batch in enumerate(msg_batches, 1):
            body = {"ids": batch, "removeLabelIds": remove}
            if add:
                body["addLabelIds"] = add
            ok = api_post(session, token_mgr, "messages/batchModify", body)
            sys.stdout.write(
                f"  Pass {pass_idx}/{total_passes}  Batch {idx}/{len(msg_batches)}"
                f" ({len(batch)} msgs){'  ✓' if ok else '  ✗'}\r"
            )
            sys.stdout.flush()
            time.sleep(0.1)

    print(f"  Done: {total:,} messages restored to inbox.            ")


# ── Step 4: delete all user labels ───────────────────────────────────────────
def delete_user_labels(session, token_mgr, user_labels: list[dict], dry_run: bool):
    if dry_run:
        print(f"  [DRY RUN] Would delete {len(user_labels)} user labels:")
        for l in sorted(user_labels, key=lambda x: x["name"]):
            print(f"    {l['name']}")
        return

    deleted = 0
    for l in sorted(user_labels, key=lambda x: x["name"]):
        ok = api_delete(session, token_mgr, f"labels/{l['id']}")
        status = "✓" if ok else "✗"
        print(f"  {status} {l['name']}")
        if ok:
            deleted += 1
        time.sleep(0.05)

    print(f"\n  Deleted {deleted}/{len(user_labels)} labels.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would happen without making changes")
    args = parser.parse_args()

    if not args.dry_run:
        print("⚠  This will remove ALL labels from ALL messages and restore everything")
        print("   to the inbox, then delete all user-created labels.")
        print("   This is irreversible without a full re-backfill.")
        print()
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return
        print()

    token_mgr = load_token()
    session   = requests.Session()

    # Step 1
    print("── Step 1: Fetching user labels ──")
    user_labels    = get_user_labels(session, token_mgr)
    user_label_ids = [l["id"] for l in user_labels]
    print(f"  Found {len(user_labels)} user-created labels.")

    if not user_labels:
        print("  No user labels found — nothing to reset.")
        return

    # Step 2
    print("\n── Step 2: Fetching all message IDs ──")
    msg_ids = fetch_all_message_ids(session, token_mgr)
    print(f"  Total messages: {len(msg_ids):,}")

    # Step 3
    print("\n── Step 3: Restoring inbox + removing user labels from all messages ──")
    if args.dry_run:
        print("  DRY RUN — no changes will be made.\n")
    restore_inbox(session, token_mgr, msg_ids, user_label_ids, args.dry_run)

    # Step 4
    print("\n── Step 4: Deleting all user labels ──")
    delete_user_labels(session, token_mgr, user_labels, args.dry_run)

    if not args.dry_run:
        print("\n✓ Reset complete.")
        print("\nNext steps:")
        print("  1. python fetch.py")
        print("  2. python auto_classify.py --reclassify --min-msgs 1")
        print("  3. python apply_labels.py")
        print("  4. python apply_labels.py --update-colors")
        print("  5. python backfill.py")
        print("  6. python export_filters_xml.py --forward-tier1")
        print("     → Gmail Settings → Import filters → output/gmail_filters.xml")


if __name__ == "__main__":
    main()
