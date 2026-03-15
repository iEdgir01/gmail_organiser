"""
export_filters_xml.py — Export filter rules as Gmail-importable XML.

Generates output/gmail_filters.xml which can be imported via:
  Gmail Settings → See all settings → Filters and Blocked Addresses
  → Import filters (at the bottom of the page)

Labels must already exist before importing — run apply_filters.py --labels-only first.

Usage:
    python export_filters_xml.py                              # all tiers, no forwarding
    python export_filters_xml.py --forward-to you@new.com    # include forward action on Tier 1
"""

import json, argparse, sys
from pathlib import Path
from xml.sax.saxutils import escape

BASE       = Path(__file__).parent
RULES_FILE = BASE / "output" / "filter_rules.json"
OUT_XML    = BASE / "output" / "gmail_filters.xml"


def prop(name: str, value: str) -> str:
    return f"    <apps:property name='{name}' value='{escape(value)}'/>"


def build_xml(rules: list[dict], forward_to: str) -> str:
    lines = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>",
        "  <title>Mail Filters</title>",
    ]

    for rule in sorted(rules, key=lambda r: r["tier"]):
        tier      = rule["tier"]
        label     = rule["label"]
        patterns  = rule["patterns"]
        from_val  = " OR ".join(patterns)

        lines += [
            "  <entry>",
            "    <category term='filter'></category>",
            "    <title>Mail Filter</title>",
            prop("from",            from_val),
            prop("label",           label),
            prop("shouldNeverSpam", "true"),
        ]

        if tier >= 2:
            lines.append(prop("shouldArchive", "true"))
        if tier == 4:
            lines.append(prop("shouldMarkAsRead", "true"))
        if tier == 1 and forward_to:
            lines.append(prop("forwardTo", forward_to))

        lines.append("  </entry>")

    lines.append("</feed>")
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-to", default="",
                        help="Include forward action on Tier 1 filters "
                             "(must be verified in Gmail Settings → Forwarding)")
    args = parser.parse_args()

    if not RULES_FILE.exists():
        print(f"ERROR: {RULES_FILE} not found. Run  python analyze.py  first.")
        return

    rules_data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    rules      = rules_data["rules"]
    forward_to = args.forward_to.strip()

    xml = build_xml(rules, forward_to)
    OUT_XML.parent.mkdir(parents=True, exist_ok=True)
    OUT_XML.write_text(xml, encoding="utf-8")

    tier_counts = {}
    for r in rules:
        tier_counts[r["tier"]] = tier_counts.get(r["tier"], 0) + 1

    print(f"Generated {len(rules)} filters → {OUT_XML}")
    for t, c in sorted(tier_counts.items()):
        fwd = f"  (+ forward to {forward_to})" if (t == 1 and forward_to) else ""
        print(f"  Tier {t}: {c} filters{fwd}")
    print()
    print("Next steps:")
    print("  1. python apply_filters.py --labels-only   # create labels via API")
    print("  2. Gmail Settings → Filters and Blocked Addresses")
    print("     → Import filters → select output/gmail_filters.xml")


if __name__ == "__main__":
    main()
