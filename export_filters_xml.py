"""
export_filters_xml.py — Export filter rules as Gmail-importable XML.

Format matches Gmail's export: hasTheWord with from:(p1 OR p2), deduped patterns.
Labels must exist before importing — run apply_filters.py (or apply_labels.py) first.

Usage:
    python export_filters_xml.py                      # all tiers, no forwarding
    python export_filters_xml.py --forward-tier1      # add forwardTo for Tier 1 (set GMAIL_FORWARD_TO)

Environment (optional):
    GMAIL_USER, GMAIL_NAME  — author in XML (defaults: placeholder)
    GMAIL_FORWARD_TO        — used when --forward-tier1 is set
    GMAIL_EXTRA_FILTER_QUERY, GMAIL_EXTRA_FILTER_LABEL — optional extra filter (e.g. to:(user+forms))
"""

import json
import argparse
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape

BASE       = Path(__file__).parent
RULES_FILE = BASE / "output" / "filter_rules.json"
OUT_XML    = BASE / "output" / "gmail_filters.xml"
FORWARD_CHUNK = 50


def _query_term(pat: str) -> str:
    pat = pat.strip()
    if pat.startswith("@"):
        return pat[1:]
    return pat


def dedup_patterns(patterns: list) -> list:
    """Remove patterns covered by a broader pattern (substring)."""
    terms = [_query_term(p) for p in patterns]
    keep  = []
    for i, t in enumerate(terms):
        covered = any(q != t and q in t for j, q in enumerate(terms) if i != j)
        if not covered:
            keep.append(patterns[i])
    return keep


def build_from_query(patterns: list) -> str:
    terms = [_query_term(p) for p in patterns]
    if len(terms) == 1:
        return f"from:({terms[0]})"
    return "from:(" + " OR ".join(terms) + ")"


def build_full_query(patterns: list, subject_patterns: list | None = None) -> str:
    query = build_from_query(patterns)
    if subject_patterns:
        if len(subject_patterns) == 1:
            query += f" subject:({subject_patterns[0]})"
        else:
            query += " subject:(" + " OR ".join(subject_patterns) + ")"
    return query


def _gmail_user() -> str:
    return (os.environ.get("GMAIL_USER") or "user@gmail.com").strip()


def _gmail_name() -> str:
    return (os.environ.get("GMAIL_NAME") or "User").strip()


def _forward_to() -> str:
    return (os.environ.get("GMAIL_FORWARD_TO") or "").strip()


def _extra_filter_query() -> str:
    return (os.environ.get("GMAIL_EXTRA_FILTER_QUERY") or "").strip()


def _extra_filter_label() -> str:
    return (os.environ.get("GMAIL_EXTRA_FILTER_LABEL") or "").strip()


def prop(name: str, value: str) -> str:
    return f"\t\t<apps:property name='{name}' value='{escape(value)}'/>"


def build_entry(counter: int, query: str, label: str, tier: int) -> list:
    lines = [
        "\t<entry>",
        "\t\t<category term='filter'></category>",
        "\t\t<title>Mail Filter</title>",
        f"\t\t<id>tag:mail.google.com,2008:filter:{counter:020d}</id>",
        "\t\t<updated>2024-01-01T00:00:00Z</updated>",
        "\t\t<content></content>",
        prop("hasTheWord", query),
        prop("label",      label),
    ]
    if tier >= 2:
        lines.append(prop("shouldArchive",    "true"))
    if tier == 4:
        lines.append(prop("shouldMarkAsRead", "true"))
    lines.append("\t</entry>")
    return lines


def build_forwarding_entry(counter: int, query: str, forward_to: str) -> list:
    return [
        "\t<entry>",
        "\t\t<category term='filter'></category>",
        "\t\t<title>Mail Filter</title>",
        f"\t\t<id>tag:mail.google.com,2008:filter:{counter:020d}</id>",
        "\t\t<updated>2024-01-01T00:00:00Z</updated>",
        "\t\t<content></content>",
        prop("hasTheWord", query),
        prop("forwardTo",  forward_to),
        "\t</entry>",
    ]


def build_xml(rules: list, forward_to: str) -> tuple[str, int]:
    gmail_user = _gmail_user()
    gmail_name = _gmail_name()
    lines = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>",
        "\t<title>Mail Filters</title>",
        "\t<id>tag:mail.google.com,2008:filters:z0000000000000000001</id>",
        "\t<updated>2024-01-01T00:00:00Z</updated>",
        "\t<author>",
        f"\t\t<name>{gmail_name}</name>",
        f"\t\t<email>{gmail_user}</email>",
        "\t</author>",
        "",
    ]
    counter = 1
    tier1_terms = []

    for rule in sorted(rules, key=lambda r: r["tier"]):
        if rule.get("skip_filter"):
            continue
        deduped   = dedup_patterns(rule["patterns"])
        subj_pats = rule.get("subject_patterns") or None
        query     = build_full_query(deduped, subj_pats)
        lines += build_entry(counter, query, rule["label"], rule["tier"])
        lines.append("")
        counter += 1
        if rule["tier"] == 1:
            tier1_terms.extend(_query_term(p) for p in deduped)

    if forward_to and tier1_terms:
        seen = []
        for t in tier1_terms:
            if not any(q != t and q in t for q in seen) and t not in seen:
                seen.append(t)
        chunks = [seen[i:i + FORWARD_CHUNK] for i in range(0, len(seen), FORWARD_CHUNK)]
        for chunk in chunks:
            fwd_query = "from:(" + " OR ".join(chunk) + ")"
            lines += build_forwarding_entry(counter, fwd_query, forward_to)
            lines.append("")
            counter += 1

    extra_query = _extra_filter_query()
    extra_label = _extra_filter_label()
    if extra_query and extra_label:
        lines += build_entry(counter, extra_query, extra_label, 1)
        lines.append("")
        counter += 1

    lines.append("</feed>")
    return "\n".join(lines), counter - 1


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-tier1", action="store_true",
                        help="Add forwardTo entries for Tier 1 (set GMAIL_FORWARD_TO)")
    args = parser.parse_args()

    if not RULES_FILE.exists():
        print(f"ERROR: {RULES_FILE} not found. Run  python auto_classify.py  first.")
        sys.exit(1)

    rules_data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    rules      = rules_data["rules"]
    forward_to = _forward_to() if args.forward_tier1 else ""
    if args.forward_tier1 and not forward_to:
        print("WARNING: GMAIL_FORWARD_TO not set; --forward-tier1 has no effect.")

    xml, count = build_xml(rules, forward_to)
    OUT_XML.parent.mkdir(parents=True, exist_ok=True)
    OUT_XML.write_text(xml, encoding="utf-8")

    tier_rules   = {}
    tier_deduped = {}
    for r in rules:
        t = r["tier"]
        tier_rules[t] = tier_rules.get(t, 0) + 1
        tier_deduped[t] = tier_deduped.get(t, 0) + len(dedup_patterns(r["patterns"]))

    print(f"Generated {count} filter entries → {OUT_XML}")
    for t in sorted(tier_rules):
        fwd = "  (+ forwardTo)" if (t == 1 and forward_to) else ""
        print(f"  Tier {t}: {tier_rules[t]} rules{fwd}")
    if count > 1000:
        print(f"\n  ⚠  Gmail limit is 1,000 filters — you have {count}.")
    print()
    print("Next steps:")
    print("  1. Create labels (e.g. apply_filters.py or apply_labels.py)")
    print("  2. Gmail Settings → Filters and Blocked Addresses → Import filters")
    print(f"     → Select {OUT_XML}")
    print("  3. Run backfill to label existing emails (if applicable)")


if __name__ == "__main__":
    main()
