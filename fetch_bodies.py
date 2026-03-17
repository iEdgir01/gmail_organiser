"""
fetch_bodies.py — Fetch email bodies for uncertain senders.

Reads data/uncertain_senders.json (produced by scan_fresh.py), picks up to
--sample-count message IDs per sender, fetches the full message body via the
Gmail API, and writes a readable review file.

Use this to decide the correct label/tier for senders that scan_fresh.py
could not confidently classify from subject lines alone.

Output:
    output/body_review.txt

Usage:
    python fetch_bodies.py                     # all uncertain senders
    python fetch_bodies.py --sender gmail.com  # single sender (substring match)
    python fetch_bodies.py --sample-count 3    # up to 3 emails per sender
    python fetch_bodies.py --min-msgs 5        # skip senders with fewer than 5 msgs
"""

import json, re, sys, time, argparse, threading, html
from pathlib import Path
from collections import defaultdict

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

BASE           = Path(__file__).parent
HDR_FILE       = BASE / "data"   / "headers.jsonl"
UNCERTAIN_FILE = BASE / "data"   / "uncertain_senders.json"
TOKEN_FILE     = BASE / "data"   / "token.json"
OUT_DIR        = BASE / "output"
SCOPES         = ["https://www.googleapis.com/auth/gmail.modify"]
API_BASE       = "https://www.googleapis.com/gmail/v1/users/me"


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


# ── Body extraction ────────────────────────────────────────────────────────────
def decode_body(data_b64: str) -> str:
    import base64
    try:
        return base64.urlsafe_b64decode(data_b64 + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_text(payload: dict, depth: int = 0) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    if depth > 5:
        return ""
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return decode_body(body_data)

    if mime == "text/html" and body_data:
        raw = decode_body(body_data)
        # Strip HTML tags, decode entities
        raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.DOTALL | re.I)
        raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = html.unescape(raw)
        raw = re.sub(r"\s{2,}", " ", raw).strip()
        return raw

    for part in payload.get("parts", []):
        text = extract_text(part, depth + 1)
        if text.strip():
            return text

    return ""


def truncate(text: str, max_chars: int = 800) -> str:
    text = text.strip()
    # Remove leading whitespace lines
    lines = [l for l in text.splitlines() if l.strip()]
    text  = "\n".join(lines)
    if len(text) > max_chars:
        return text[:max_chars] + "\n  [... truncated]"
    return text


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--sender",       default="",  help="Filter to senders matching this substring")
    parser.add_argument("--sample-count", type=int, default=2,
                        help="Number of message bodies to fetch per sender (default: 2)")
    parser.add_argument("--min-msgs",     type=int, default=1,
                        help="Skip senders with fewer than N total messages (default: 1)")
    parser.add_argument("--max-senders",  type=int, default=0,
                        help="Stop after N senders (0 = all)")
    args = parser.parse_args()

    if not UNCERTAIN_FILE.exists():
        print(f"ERROR: {UNCERTAIN_FILE} not found. Run  python scan_fresh.py  first.")
        sys.exit(1)

    uncertain = json.loads(UNCERTAIN_FILE.read_text(encoding="utf-8"))

    # Filter
    if args.sender:
        uncertain = [s for s in uncertain if args.sender.lower() in s["addr"]]
    if args.min_msgs > 1:
        uncertain = [s for s in uncertain if s["total"] >= args.min_msgs]
    if args.max_senders:
        uncertain = uncertain[:args.max_senders]

    if not uncertain:
        print("No matching uncertain senders found.")
        sys.exit(0)

    print(f"Processing {len(uncertain)} uncertain sender(s), "
          f"up to {args.sample_count} emails each.")

    # Build a reverse-lookup: msg_id → sender addr  (for cross-referencing headers.jsonl)
    # We already have sample_ids in uncertain_senders.json, but let's also let the
    # user fetch more by looking up all IDs for that sender in headers.jsonl.
    needed_senders = {s["addr"] for s in uncertain}
    sender_all_ids: dict[str, list] = defaultdict(list)

    print(f"Scanning headers.jsonl for message IDs...")
    with open(HDR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            addr = msg.get("from", "")
            m    = re.search(r"<([^>]+)>", addr)
            addr = (m.group(1) if m else addr).lower().strip()
            if addr in needed_senders:
                sender_all_ids[addr].append(msg["id"])

    token_mgr = load_token()
    session   = requests.Session()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_lines = [
        "BODY REVIEW — UNCERTAIN SENDERS",
        f"Senders: {len(uncertain)}   Samples per sender: {args.sample_count}",
        "Use this to decide the correct label + tier for each sender.",
        "═" * 80,
    ]

    for idx, sender in enumerate(uncertain, 1):
        addr     = sender["addr"]
        total    = sender["total"]
        scores   = sender.get("scores", {})
        score_str = "  ".join(f"{k}:{v:.0f}" for k, v in list(scores.items())[:4]) or "no signal"

        # Pick sample IDs — prefer ones already in the uncertain record, then full list
        all_ids     = sender_all_ids.get(addr, sender.get("sample_ids", []))
        sample_ids  = all_ids[:args.sample_count]

        out_lines += [
            "",
            f"{'─'*80}",
            f"  [{idx}/{len(uncertain)}]  {addr}  ({total:,} msgs)  signals: [{score_str}]",
            f"{'─'*80}",
        ]

        if not sample_ids:
            out_lines.append("  (no message IDs found)")
            continue

        for mid in sample_ids:
            sys.stdout.write(f"  Fetching {mid} ({addr})...    \r")
            sys.stdout.flush()

            data = api_get(session, token_mgr, f"messages/{mid}", {"format": "full"})
            if not data:
                out_lines.append(f"  [FETCH FAILED: {mid}]")
                time.sleep(0.5)
                continue

            hdrs    = {h["name"]: h["value"]
                       for h in data.get("payload", {}).get("headers", [])}
            subject = hdrs.get("Subject", "(no subject)")
            date    = hdrs.get("Date", "")
            frm     = hdrs.get("From", addr)

            body_text = extract_text(data.get("payload", {}))
            snippet   = truncate(body_text) if body_text.strip() else f"[snippet: {data.get('snippet', '')}]"

            out_lines += [
                f"",
                f"  From:    {frm}",
                f"  Date:    {date}",
                f"  Subject: {subject}",
                f"  {'─'*74}",
            ]
            for line in snippet.splitlines():
                out_lines.append(f"  {line}")
            out_lines.append("")

            time.sleep(0.1)

    sys.stdout.write(" " * 60 + "\r")
    report   = "\n".join(out_lines)
    out_file = OUT_DIR / "body_review.txt"
    out_file.write_text(report, encoding="utf-8")
    print(f"Body review → {out_file}")


if __name__ == "__main__":
    main()
