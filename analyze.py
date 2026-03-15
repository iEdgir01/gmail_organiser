"""
analyze.py — Read headers.jsonl and produce Gmail filter recommendations.

Classification rules are loaded from config/rules.yaml.
Copy config/rules.example.yaml to config/rules.yaml and customise it first.

Priority tiers:
  TIER 1 — IMPORTANT:   label + keep in inbox
                         (finance, security alerts, billing)
  TIER 2 — USEFUL:      label + skip inbox
                         (gaming, social, entertainment, jobs)
  TIER 3 — LOW NOISE:   label + skip inbox  (NOT marked read)
                         (light newsletters, occasional promos)
  TIER 4 — UNSUBSCRIBE: label + skip inbox + mark read
                         (pure marketing noise — review and unsubscribe)

Output:
  - Console summary with counts
  - output/filter_rules.json   — machine-readable rules for apply_filters.py
  - output/analysis_report.txt — human-readable report

Usage:
    python analyze.py
"""

import json, re, os, sys
from pathlib import Path
from collections import Counter, defaultdict

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required.  Run:  pip install pyyaml")
    sys.exit(1)

BASE        = Path(__file__).parent
HDR_FILE    = BASE / "data"   / "headers.jsonl"
RULES_YAML  = BASE / "config" / "rules.yaml"
OUT_DIR     = BASE / "output"
OUT_JSON    = OUT_DIR / "filter_rules.json"
OUT_TXT     = OUT_DIR / "analysis_report.txt"


# ─────────────────────────────────────────────────────────────────────────────
# Rules loading
# ─────────────────────────────────────────────────────────────────────────────
def _load_rules(path: Path) -> list[tuple]:
    """
    Load rules from a YAML file and return a list of
    (pattern, label, tier, name) tuples — same format as the original
    hardcoded RULES list for full backward compatibility.
    """
    if not path.exists():
        example = path.parent / "rules.example.yaml"
        print(f"WARNING: {path} not found.")
        if example.exists():
            print(f"  Copy the example to get started:")
            print(f"    cp {example} {path}")
        return []

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    raw = (data or {}).get("rules", []) or []
    rules = []
    for entry in raw:
        pattern = str(entry.get("pattern", "")).strip()
        label   = str(entry.get("label",   "")).strip()
        tier    = int(entry.get("tier",    2))
        name    = str(entry.get("name",    pattern)).strip()
        if pattern and label:
            rules.append((pattern, label, tier, name))
    return rules


# Module-level RULES — loaded once at import time.
# discover_senders.py and accounts.py import this directly.
RULES: list[tuple] = _load_rules(RULES_YAML)


# ─────────────────────────────────────────────────────────────────────────────
def extract_addr(frm: str) -> str:
    m = re.search(r"<([^>]+)>", frm)
    addr = m.group(1) if m else frm
    return addr.lower().strip()


def classify(addr: str) -> tuple | None:
    for pattern, label, tier, name in RULES:
        if pattern.lower() in addr:
            return label, tier, name
    return None


def load_data() -> list[dict]:
    data = []
    with open(HDR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    data.append(json.loads(line))
                except Exception:
                    pass
    return data


def analyze(data: list[dict]):
    total = len(data)

    label_counts:  Counter = Counter()
    tier_counts:   Counter = Counter()
    unclassified:  Counter = Counter()
    label_to_tier: dict    = {}
    label_senders: dict    = defaultdict(Counter)

    for msg in data:
        addr = extract_addr(msg.get("from", ""))
        if not addr:
            continue
        result = classify(addr)
        if result:
            lbl, tier, name = result
            label_counts[lbl] += 1
            tier_counts[tier]  += 1
            label_to_tier[lbl]  = tier
            label_senders[lbl][addr] += 1
        else:
            unclassified[addr] += 1

    categorised = sum(label_counts.values())
    return {
        "total":         total,
        "categorised":   categorised,
        "label_counts":  label_counts,
        "tier_counts":   tier_counts,
        "label_to_tier": label_to_tier,
        "label_senders": label_senders,
        "unclassified":  unclassified,
    }


def build_filter_rules(label_senders: dict, label_to_tier: dict) -> list[dict]:
    """
    Collapse per-address observations back to rule patterns (from RULES).
    Returns a list of dicts ready for apply_filters.py.
    """
    pattern_map = {pat.lower(): (lbl, tier, name) for pat, lbl, tier, name in RULES}

    group_patterns: dict = defaultdict(set)
    group_meta:     dict = {}

    for pattern, (lbl, tier, name) in pattern_map.items():
        key = (lbl, tier)
        group_patterns[key].add(pattern)
        group_meta[key] = {"label": lbl, "tier": tier}

    rules = []
    for (lbl, tier), patterns in sorted(group_patterns.items()):
        rules.append({
            "label":      lbl,
            "tier":       tier,
            "patterns":   sorted(patterns),
            "keep_inbox": tier == 1,
            "mark_read":  tier == 4,
        })
    return rules


def format_report(stats: dict, rules: list[dict]) -> str:
    lines = []
    a = lines.append

    a("=" * 72)
    a("  GMAIL MAILBOX ANALYSIS — FILTER RECOMMENDATIONS")
    a("=" * 72)
    a(f"\n  Total messages analysed : {stats['total']:,}")
    a(f"  Categorised             : {stats['categorised']:,}  "
      f"({stats['categorised']/stats['total']*100:.1f}%)" if stats['total'] else "")
    a(f"  Uncategorised           : {stats['total']-stats['categorised']:,}")
    a(f"\n  Tier 1 (keep inbox)      : {stats['tier_counts'][1]:,}")
    a(f"  Tier 2 (skip inbox)      : {stats['tier_counts'][2]:,}")
    a(f"  Tier 3 (skip inbox)      : {stats['tier_counts'][3]:,}")
    a(f"  Tier 4 (unsubscribe)     : {stats['tier_counts'][4]:,}")

    TIER_LABEL = {
        1: "TIER 1 — IMPORTANT  (label + keep in inbox)",
        2: "TIER 2 — USEFUL     (label + move out of inbox)",
        3: "TIER 3 — LOW NOISE  (label + move out, not marked read)",
        4: "TIER 4 — UNSUBSCRIBE (label + move out + mark read)",
    }

    for tier in [1, 2, 3, 4]:
        a(f"\n{'─'*72}")
        a(f"  {TIER_LABEL[tier]}")
        a(f"{'─'*72}")
        a(f"  {'LABEL':<40} {'COUNT':>7}  {'TOP SENDER'}")
        a(f"  {'-'*40} {'-'*7}  {'-'*30}")
        tier_labels = [(lbl, cnt)
                       for lbl, cnt in stats['label_counts'].most_common()
                       if stats['label_to_tier'].get(lbl) == tier]
        for lbl, cnt in tier_labels:
            top = stats['label_senders'][lbl].most_common(1)
            top_addr = top[0][0] if top else ""
            if len(top_addr) > 38:
                top_addr = top_addr[:35] + "..."
            a(f"  {lbl:<40} {cnt:>7,}  {top_addr}")

    a(f"\n{'─'*72}")
    a("  TOP 30 UNCLASSIFIED SENDERS")
    a(f"{'─'*72}")
    for addr, cnt in stats['unclassified'].most_common(30):
        a(f"  {cnt:>6,}  {addr}")

    a(f"\n{'─'*72}")
    a("  READY TO APPLY?")
    a("  Run:  python apply_filters.py --dry-run   # preview")
    a("        python apply_filters.py             # create labels + filters")
    a("=" * 72)
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not RULES:
        print("ERROR: No rules loaded. Cannot analyse.")
        print(f"  Copy config/rules.example.yaml to config/rules.yaml and customise it.")
        return
    if not HDR_FILE.exists():
        print(f"ERROR: {HDR_FILE} not found. Run  python fetch.py  first.")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading data...")
    data = load_data()
    print(f"Loaded {len(data):,} messages.")

    print("Analysing...")
    stats = analyze(data)
    rules = build_filter_rules(stats["label_senders"], stats["label_to_tier"])

    report = format_report(stats, rules)
    print(report)

    OUT_TXT.write_text(report, encoding="utf-8")
    print(f"\nReport saved to {OUT_TXT}")

    rules_out = {
        "generated_from": str(HDR_FILE),
        "total_messages": stats["total"],
        "rules":          rules,
    }
    OUT_JSON.write_text(json.dumps(rules_out, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"Filter rules saved to {OUT_JSON}")


if __name__ == "__main__":
    main()
