from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Tuple

from .config import DATA_PATHS, ensure_data_dir
from .utils import json_read, json_write, WHITESPACE_RE


MAX_CLUSTERS_PER_SENDER = 25
MIN_CLUSTER_SIZE = 3


@dataclass
class ThreadCluster:
    cluster_id: str
    sender_domain: str
    threads: List[str]
    subjects: List[str]
    body_samples: List[str]


def subject_key(subject_norm: str, max_tokens: int = 4) -> str:
    tokens = [t for t in WHITESPACE_RE.split(subject_norm.strip()) if len(t) >= 3]
    tokens = sorted(tokens)[:max_tokens]
    if len(tokens) >= 2:
        key_tokens = tokens[:2]
    else:
        key_tokens = tokens
    return " ".join(key_tokens) if key_tokens else "_misc"


def cluster_domain_threads(threads: List[Dict[str, Any]], sender_domain: str) -> List[ThreadCluster]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for t in threads:
        norms = t.get("subject_norms") or []
        if norms:
            key = subject_key(norms[0])
        else:
            key = "_misc"
        buckets.setdefault(key, []).append(t)

    clusters: List[ThreadCluster] = []
    for key, ts in buckets.items():
        thread_ids = [t["thread_id"] for t in ts]
        subjects = sorted(
            {s for t in ts for s in (t.get("subjects") or []) if s}
        )
        body_samples: List[str] = []
        for t in ts:
            for b in t.get("body_samples") or []:
                if b not in body_samples:
                    body_samples.append(b)
                if len(body_samples) >= 5:
                    break
            if len(body_samples) >= 5:
                break
        cluster_raw_id = f"{sender_domain}:{key}"
        cluster_id = hashlib.sha1(cluster_raw_id.encode("utf-8")).hexdigest()[:16]
        clusters.append(
            ThreadCluster(
                cluster_id=cluster_id,
                sender_domain=sender_domain,
                threads=thread_ids,
                subjects=subjects,
                body_samples=body_samples,
            )
        )

    # Safety: enforce max_clusters_per_sender by keeping largest clusters
    clusters.sort(key=lambda c: len(c.threads), reverse=True)
    clusters = clusters[:MAX_CLUSTERS_PER_SENDER]

    # Merge small clusters into a generic one per sender
    big: List[ThreadCluster] = []
    small_threads: List[str] = []
    small_subjects: List[str] = []
    small_bodies: List[str] = []

    for c in clusters:
        if len(c.threads) < MIN_CLUSTER_SIZE:
            small_threads.extend(c.threads)
            small_subjects.extend(c.subjects)
            small_bodies.extend(c.body_samples)
        else:
            big.append(c)

    if small_threads:
        cluster_raw_id = f"{sender_domain}:_generic_small"
        cluster_id = hashlib.sha1(cluster_raw_id.encode("utf-8")).hexdigest()[:16]
        big.append(
            ThreadCluster(
                cluster_id=cluster_id,
                sender_domain=sender_domain,
                threads=small_threads,
                subjects=sorted(set(small_subjects)),
                body_samples=small_bodies[:5],
            )
        )

    return big


def main() -> None:
    ensure_data_dir()
    threads: List[Dict[str, Any]] = json_read(DATA_PATHS.threads)
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for t in threads:
        domain = (t.get("sender_domain") or "").lower()
        if not domain:
            domain = "unknown"
        by_domain.setdefault(domain, []).append(t)

    all_clusters: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {"thread_count": len(threads), "clusters_per_sender": {}}

    for domain, ts in by_domain.items():
        clusters = cluster_domain_threads(ts, domain)
        stats["clusters_per_sender"][domain] = len(clusters)
        for c in clusters:
            all_clusters.append(asdict(c))

    # cluster_stats will be augmented later with more metrics
    json_write(DATA_PATHS.clusters, all_clusters)
    json_write(
        DATA_PATHS.cluster_stats,
        {**stats, "avg_threads_per_cluster": (len(threads) / max(len(all_clusters), 1))},
    )
    print(f"Wrote {len(all_clusters)} clusters to {DATA_PATHS.clusters}")


if __name__ == "__main__":
    main()

