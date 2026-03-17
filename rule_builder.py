from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Dict, List, Any

from .config import DATA_PATHS, ensure_data_dir
from .utils import json_read, json_write


MAX_SUBJECT_PATTERNS = 15


@dataclass
class FilterRule:
    label: str
    tier: int
    senders: List[str]
    subject_patterns: List[str]


def merge_classified_clusters(
    classified_clusters: List[Dict[str, Any]]
) -> List[FilterRule]:
    buckets: Dict[tuple, Dict[str, Any]] = {}
    for c in classified_clusters:
        key = (c.get("sender_domain") or "", c.get("label") or "Uncategorized")
        bucket = buckets.setdefault(
            key,
            {
                "label": c.get("label") or "Uncategorized",
                "tier": c.get("tier", 3),
                "senders": set(),
                "patterns": set(),
            },
        )
        bucket["senders"].add(c.get("sender_domain") or "")
        for p in c.get("subject_patterns") or []:
            if p:
                bucket["patterns"].add(p)

    rules: List[FilterRule] = []
    for (domain, _), data in buckets.items():
        patterns = list(data["patterns"])
        patterns = patterns[:MAX_SUBJECT_PATTERNS]
        senders = sorted({s for s in data["senders"] if s})
        rules.append(
            FilterRule(
                label=data["label"],
                tier=int(data["tier"]),
                senders=senders,
                subject_patterns=patterns,
            )
        )
    return rules


def build_query_for_rule(rule: FilterRule) -> str:
    # sender_query: from:(a.com OR b.com)
    if not rule.senders:
        sender_query = ""
    elif len(rule.senders) == 1:
        sender_query = f'from:({rule.senders[0]})'
    else:
        sender_query = "from:(" + " OR ".join(rule.senders) + ")"

    subject_parts: List[str] = []
    for p in rule.subject_patterns:
        if " " in p:
            subject_parts.append(f'"{p}"')
        else:
            subject_parts.append(p)
    subject_query = ""
    if subject_parts:
        subject_query = "subject:(" + " OR ".join(subject_parts) + ")"

    if sender_query and subject_query:
        return f"{sender_query} {subject_query}"
    return sender_query or subject_query


def main() -> None:
    ensure_data_dir()
    classified: List[Dict[str, Any]] = json_read(DATA_PATHS.classified_clusters)
    rules = merge_classified_clusters(classified)
    serializable = []
    for r in rules:
        d = asdict(r)
        d["gmail_query"] = build_query_for_rule(r)
        serializable.append(d)

    json_write(DATA_PATHS.filter_rules, serializable)
    json_write(
        DATA_PATHS.rule_stats,
        {
            "rule_count": len(serializable),
            "patterns_per_rule": {
                i: len(r["subject_patterns"]) for i, r in enumerate(serializable)
            },
        },
    )
    print(f"Wrote {len(serializable)} filter rules to {DATA_PATHS.filter_rules}")


if __name__ == "__main__":
    main()

