from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Any

from .config import DATA_PATHS, ensure_data_dir
from .utils import json_read, json_write


@dataclass
class ClassifiedCluster:
    cluster_id: str
    sender_domain: str
    threads: List[str]
    subjects: List[str]
    body_samples: List[str]
    label: str
    tier: int
    subject_patterns: List[str]


SYSTEM_PROMPT = """You are an email classification assistant.
You receive emails grouped into clusters by sender and subject similarity.
You must assign each cluster a high-level folder label and tier (1 is highest priority).
Return JSON only, matching this schema:
{{
  "label": "string (like 'Finance/Payments' or 'Dev/Alerts')",
  "tier": 1,
  "subject_patterns": ["short keyword or phrase", "..."]
}}
"""


def build_prompt(cluster: Dict[str, Any]) -> str:
    sender_domain = cluster.get("sender_domain") or ""
    subjects = cluster.get("subjects") or []
    body_samples = cluster.get("body_samples") or []
    lines: List[str] = []
    lines.append(f"Sender domain: {sender_domain}")
    lines.append("")
    lines.append("Thread subjects:")
    for s in subjects[:10]:
        lines.append(f"- {s}")
    lines.append("")
    lines.append("Body samples:")
    for b in body_samples[:5]:
        lines.append(f"- {b[:200]}")
    return "\n".join(lines)


def classify_cluster_openai(cluster: Dict[str, Any]) -> ClassifiedCluster:
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "openai package is required for cluster classification.\n"
            "Install with:\n"
            "  pip install openai"
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY environment variable is required.")

    client = OpenAI(api_key=api_key)
    prompt = build_prompt(cluster)

    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    content = completion.choices[0].message.content or "{}"

    import json

    data = json.loads(content)
    label = str(data.get("label") or "Uncategorized")
    tier_raw = data.get("tier", 3)
    try:
        tier = int(tier_raw)
    except Exception:
        tier = 3
    patterns = [str(p).strip() for p in data.get("subject_patterns") or [] if str(p).strip()]

    return ClassifiedCluster(
        cluster_id=cluster["cluster_id"],
        sender_domain=cluster["sender_domain"],
        threads=cluster.get("threads") or [],
        subjects=cluster.get("subjects") or [],
        body_samples=cluster.get("body_samples") or [],
        label=label,
        tier=tier,
        subject_patterns=patterns,
    )


def main() -> None:
    ensure_data_dir()
    clusters: List[Dict[str, Any]] = json_read(DATA_PATHS.clusters)
    classified: List[Dict[str, Any]] = []
    for c in clusters:
        cc = classify_cluster_openai(c)
        classified.append(asdict(cc))
    json_write(DATA_PATHS.classified_clusters, classified)
    print(f"Wrote {len(classified)} classified clusters to {DATA_PATHS.classified_clusters}")


if __name__ == "__main__":
    main()

