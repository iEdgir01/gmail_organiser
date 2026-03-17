"""
fetch.py — Pull all Gmail message IDs then fetch From/Subject/List headers.

Uses the Gmail API directly via data/token.json (from auth_setup.py).
Falls back to gws CLI subprocesses if token.json is not present.

Direct API mode:  ~100+ messages/sec
gws CLI fallback: ~10  messages/sec

Saves progress every 500 messages — safe to interrupt and resume.

Usage:
    python auth_setup.py   # first time only
    python fetch.py        # run / resume
    python fetch.py --reset  # wipe and start fresh

Environment variables:
    GMAIL_CLIENT_SECRET  — path to client secret JSON (default: config/client_secret.json)
    GWS_CMD              — path to gws CLI binary (default: auto-detected from PATH)
"""

import os
import sys
import json
import time
import argparse
import threading
import subprocess
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as req_lib
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ── Paths & config ────────────────────────────────────────────────────────────
BASE       = Path(__file__).parent
DATA_DIR   = BASE / "data"
IDS_FILE   = DATA_DIR / "message_ids.txt"
HDR_FILE   = DATA_DIR / "headers.jsonl"
TOKEN_FILE = DATA_DIR / "token.json"

CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET") or str(BASE / "config" / "client_secret.json")
GWS_CMD = os.environ.get("GWS_CMD") or shutil.which("gws") or "gws"

WORKERS_DIRECT = 50
WORKERS_GWS    = 50
PAGE_SIZE     = 500
SAVE_EVERY    = 500
BASE_ENV      = {**os.environ, "PYTHONUTF8": "1"}
SCOPES        = ["https://www.googleapis.com/auth/gmail.modify"]
API_BASE      = "https://www.googleapis.com/gmail/v1/users/me"


# ── Auth ──────────────────────────────────────────────────────────────────────
class TokenManager:
    """Thread-safe token manager with auto-refresh."""
    def __init__(self, creds: Credentials):
        self._creds = creds
        self._lock  = threading.Lock()

    @property
    def token(self) -> str:
        with self._lock:
            if not self._creds.valid:
                self._creds.refresh(Request())
            return self._creds.token

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


def load_token() -> TokenManager | None:
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except Exception as e:
            print(f"  Token refresh failed: {e}")
            return None
    if not creds.valid:
        return None
    return TokenManager(creds)


# ── Direct API helpers ────────────────────────────────────────────────────────
def api_get(session: req_lib.Session, token_mgr: TokenManager,
            path: str, params: dict = None, retry: int = 4) -> dict:
    url = f"{API_BASE}/{path}"
    backoff = [1, 2, 5, 10]
    for attempt in range(retry):
        r = session.get(url, headers=token_mgr.headers, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            with token_mgr._lock:
                token_mgr._creds.expiry = None
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(backoff[min(attempt, len(backoff) - 1)])
            continue
        break
    return {}


# ── gws CLI fallback ──────────────────────────────────────────────────────────
def gws(*args, params: dict = None, retry: int = 3) -> dict:
    cmd = [GWS_CMD, *args]
    if params:
        cmd += ["--params", json.dumps(params)]
    for attempt in range(retry):
        r = subprocess.run(cmd, capture_output=True,
                           encoding="utf-8", errors="replace", env=BASE_ENV)
        text = r.stdout
        start = text.find("{")
        if start != -1:
            try:
                return json.loads(text[start:])
            except Exception:
                pass
        if attempt < retry - 1:
            time.sleep(1 + attempt)
    return {}


# ── Step 1 — Fetch all message IDs ───────────────────────────────────────────
def fetch_all_ids(token_mgr: TokenManager | None) -> list[str]:
    if IDS_FILE.exists():
        ids = [l.strip() for l in IDS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        print(f"  Loaded {len(ids):,} existing IDs from disk.")
        return ids

    print("  Paginating through mailbox to collect all message IDs...")
    all_ids = []
    page_num = 0

    if token_mgr:
        session = req_lib.Session()
        params  = {"maxResults": PAGE_SIZE}
        while True:
            data     = api_get(session, token_mgr, "messages", params)
            msgs     = data.get("messages", [])
            all_ids += [m["id"] for m in msgs]
            page_num += 1
            sys.stdout.write(f"  Page {page_num:3d} — {len(all_ids):,} IDs\r")
            sys.stdout.flush()
            nxt = data.get("nextPageToken")
            if not nxt:
                break
            params = {"maxResults": PAGE_SIZE, "pageToken": nxt}
    else:
        params = {"userId": "me", "maxResults": PAGE_SIZE}
        while True:
            data     = gws("gmail", "users", "messages", "list", params=params)
            msgs     = data.get("messages", [])
            all_ids += [m["id"] for m in msgs]
            page_num += 1
            sys.stdout.write(f"  Page {page_num:3d} — {len(all_ids):,} IDs\r")
            sys.stdout.flush()
            nxt = data.get("nextPageToken")
            if not nxt:
                break
            params = {"userId": "me", "maxResults": PAGE_SIZE, "pageToken": nxt}

    print(f"\n  {len(all_ids):,} total IDs collected.")
    IDS_FILE.write_text("\n".join(all_ids), encoding="utf-8")
    return all_ids


# ── Step 2 — Fetch headers ────────────────────────────────────────────────────
def load_done_ids() -> set[str]:
    if not HDR_FILE.exists():
        return set()
    done = set()
    with open(HDR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:
                    pass
    return done


def parse_record(data: dict) -> dict | None:
    if not data or "payload" not in data:
        return None
    hdrs = {h["name"]: h["value"] for h in data["payload"].get("headers", [])}
    return {
        "id":      data.get("id", ""),
        "from":    hdrs.get("From",             ""),
        "subject": hdrs.get("Subject",          ""),
        "list":    hdrs.get("List-Unsubscribe", ""),
        "labels":  data.get("labelIds",         []),
    }


def fetch_all_headers(all_ids: list[str], token_mgr: TokenManager | None) -> None:
    done_ids  = load_done_ids()
    remaining = [mid for mid in all_ids if mid not in done_ids]
    total     = len(all_ids)
    n_done    = len(done_ids)

    if not remaining:
        print(f"  All {total:,} headers already fetched.")
        return

    workers = WORKERS_DIRECT if token_mgr else WORKERS_GWS
    mode    = "direct API" if token_mgr else "gws CLI"
    est     = int(len(remaining) / (100 if token_mgr else 10) / 60)
    print(f"  {n_done:,} done — {len(remaining):,} remaining  "
          f"({workers} workers, {mode}, est. ~{est}m)")

    write_lock = threading.Lock()
    buf: list  = []
    errors     = 0

    def flush(force: bool = False) -> None:
        nonlocal buf
        if not buf:
            return
        if force or len(buf) >= SAVE_EVERY:
            with write_lock:
                with open(HDR_FILE, "a", encoding="utf-8") as f:
                    for rec in buf:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                buf = []

    def fetch_direct(mid: str) -> dict | None:
        session = req_lib.Session()
        data = api_get(session, token_mgr, f"messages/{mid}", {"format": "metadata"})
        return parse_record(data)

    def fetch_gws(mid: str) -> dict | None:
        data = gws("gmail", "users", "messages", "get", params={
            "userId": "me", "id": mid, "format": "metadata",
        })
        return parse_record(data)

    worker_fn = fetch_direct if token_mgr else fetch_gws
    t0 = time.time()
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker_fn, mid): mid for mid in remaining}
        for fut in as_completed(futures):
            try:
                rec = fut.result()
                if rec:
                    with write_lock:
                        buf.append(rec)
                    flush()
                else:
                    errors += 1
            except Exception:
                errors += 1

            completed += 1
            total_done = n_done + completed
            elapsed    = time.time() - t0
            rate       = completed / elapsed if elapsed > 0 else 0.001
            eta        = (len(remaining) - completed) / rate
            sys.stdout.write(
                f"  {total_done:>6,}/{total:,}  "
                f"({total_done/total*100:5.1f}%)  "
                f"{rate:6.1f}/s  "
                f"ETA {int(eta//60)}m{int(eta%60):02d}s  "
                f"errors:{errors}   \r"
            )
            sys.stdout.flush()

    flush(force=True)
    elapsed = time.time() - t0
    print(f"\n  Done in {int(elapsed//60)}m{int(elapsed%60):02d}s  "
          f"({errors} errors, {completed-errors:,} successful)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Wipe data and start fresh")
    args = parser.parse_args()

    if args.reset:
        for f in [IDS_FILE, HDR_FILE]:
            if f.exists():
                f.unlink()
                print(f"  Deleted {f.name}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("\nLoading auth token...")
    token_mgr = load_token()
    if token_mgr:
        print("  Direct API mode (fast)")
    else:
        print("  No token.json found — using gws CLI fallback (slower)")
        print("  Run  python auth_setup.py  for fast mode")

    print("\n[1/2] Collecting message IDs...")
    all_ids = fetch_all_ids(token_mgr)
    print(f"      {len(all_ids):,} messages in mailbox")

    print("\n[2/2] Fetching message headers...")
    fetch_all_headers(all_ids, token_mgr)

    n = sum(1 for _ in open(HDR_FILE, encoding="utf-8"))
    print(f"\n  {n:,} records saved to {HDR_FILE.name}")
    print("\nNext:  python auto_classify.py")


if __name__ == "__main__":
    main()
