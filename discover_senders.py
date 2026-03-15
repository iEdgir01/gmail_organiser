"""
discover_senders.py — Find high-volume senders NOT yet covered by any rule.

Run this after fetch.py to find gaps in your classification rules, then
add new entries to config/rules.yaml.

Usage:
    python discover_senders.py             # show top 100 unclassified senders
    python discover_senders.py --min 5     # only show senders with >= 5 messages
    python discover_senders.py --limit 50  # show top 50
"""

import json, re, argparse
from pathlib import Path
from collections import Counter

BASE     = Path(__file__).parent
HDR_FILE = BASE / "data" / "headers.jsonl"

# Import rules from analyze.py
import sys
sys.path.insert(0, str(BASE))
from analyze import RULES, extract_addr, classify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min",   type=int, default=3,   help="Min message count")
    parser.add_argument("--limit", type=int, default=100, help="Max senders to show")
    args = parser.parse_args()

    if not HDR_FILE.exists():
        print(f"ERROR: {HDR_FILE} not found. Run fetch.py first.")
        return

    if not RULES:
        print("WARNING: No rules loaded. Copy config/rules.example.yaml to config/rules.yaml first.")

    unclassified: Counter = Counter()
    addr_to_name: dict    = {}
    total = 0

    with open(HDR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg  = json.loads(line)
                frm  = msg.get("from", "")
                addr = extract_addr(frm)
                if not addr:
                    continue
                total += 1
                if classify(addr) is None:
                    unclassified[addr] += 1
                    # capture display name
                    m = re.match(r'^"?([^"<]+)"?\s*<', frm)
                    if m and addr not in addr_to_name:
                        addr_to_name[addr] = m.group(1).strip().strip('"')
            except Exception:
                pass

    covered = total - sum(unclassified.values())
    print(f"\nTotal messages : {total:,}")
    print(f"Covered        : {covered:,}  ({covered/total*100:.1f}%)" if total else "")
    print(f"Uncovered      : {sum(unclassified.values()):,}")
    print(f"\nTop unclassified senders (>= {args.min} messages):\n")
    print(f"  {'COUNT':>6}  {'ADDRESS':<50}  DISPLAY NAME")
    print(f"  {'─'*6}  {'─'*50}  {'─'*30}")

    shown = 0
    for addr, cnt in unclassified.most_common():
        if cnt < args.min:
            break
        if shown >= args.limit:
            break
        name = addr_to_name.get(addr, "")
        print(f"  {cnt:>6,}  {addr:<50}  {name}")
        shown += 1

    print(f"\nShowing {shown} senders. Add them to config/rules.yaml.")


if __name__ == "__main__":
    main()
