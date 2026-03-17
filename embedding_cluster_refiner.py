from __future__ import annotations

"""
Optional embedding-based refinement of token-based clusters.

Pipeline:
  thread_clusterer.py -> embedding_cluster_refiner.py -> cluster_classifier.py

Only clusters with at least `REFINE_MIN_CLUSTER_SIZE` threads are refined.
"""

import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Any

from .config import DATA_PATHS, ensure_data_dir
from .utils import json_read, json_write


REFINE_MIN_CLUSTER_SIZE = 5


@dataclass
class Embedding:
    thread_id: str
    vector: List[float]


def build_embedding_inputs(threads_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    inputs: Dict[str, str] = {}
    for tid, t in threads_by_id.items():
        subjects = t.get("subjects") or []
        bodies = t.get("body_samples") or []
        subject = subjects[0] if subjects else ""
        body = bodies[0] if bodies else ""
        text = subject
        if body:
            text = f"{subject}\n\n{body}"
        inputs[tid] = text.strip()
    return inputs


def get_openai_client():
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "openai package is required for embedding refinement.\n"
            "Install with:\n"
            "  pip install openai"
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY environment variable is required.")
    return OpenAI(api_key=api_key)


def embed_threads(thread_texts: Dict[str, str]) -> Dict[str, List[float]]:
    """
    Generate embeddings for each thread using text-embedding-3-small.
    """
    client = get_openai_client()
    ids = list(thread_texts.keys())
    texts = [thread_texts[i] for i in ids]

    # Chunk requests to stay within API limits
    vectors: Dict[str, List[float]] = {}
    batch_size = 128
    for i in range(0, len(ids), batch_size):
        chunk_ids = ids[i : i + batch_size]
        chunk_texts = texts[i : i + batch_size]
        resp = client.embeddings.create(
            model="text-embedding-3-small", input=chunk_texts
        )
        for tid, emb in zip(chunk_ids, resp.data):
            vectors[tid] = emb.embedding  # type: ignore[attr-defined]
    return vectors


def cosine_dbscan_cluster(thread_vectors: Dict[str, List[float]], eps: float = 0.18, min_samples: int = 3) -> Dict[int, List[str]]:
    """
    Cluster thread embeddings using DBSCAN with cosine metric.
    Returns mapping: cluster_label -> [thread_ids]; noise points have label -1.
    """
    try:
        from sklearn.cluster import DBSCAN  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "scikit-learn is required for embedding refinement.\n"
            "Install with:\n"
            "  pip install scikit-learn"
        ) from exc

    if not thread_vectors:
        return {}

    thread_ids = list(thread_vectors.keys())
    X = [thread_vectors[tid] for tid in thread_ids]

    clustering = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine").fit(X)
    labels = clustering.labels_

    clusters: Dict[int, List[str]] = {}
    for tid, label in zip(thread_ids, labels):
        clusters.setdefault(int(label), []).append(tid)
    return clusters


def refine_clusters(
    clusters: List[Dict[str, Any]],
    threads: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    threads_by_id: Dict[str, Dict[str, Any]] = {t["thread_id"]: t for t in threads}
    refined: List[Dict[str, Any]] = []

    for c in clusters:
        thread_ids = c.get("threads") or []
        if len(thread_ids) < REFINE_MIN_CLUSTER_SIZE:
            # keep cluster as-is
            refined.append(c)
            continue

        # Prepare texts and embeddings
        cluster_threads = {
            tid: threads_by_id[tid]
            for tid in thread_ids
            if tid in threads_by_id
        }
        if len(cluster_threads) < REFINE_MIN_CLUSTER_SIZE:
            refined.append(c)
            continue

        texts = build_embedding_inputs(cluster_threads)
        vectors = embed_threads(texts)
        label_to_tids = cosine_dbscan_cluster(vectors, eps=0.18, min_samples=3)

        # If DBSCAN produced 0 or 1 non-noise cluster, keep original
        non_noise = {lbl: tids for lbl, tids in label_to_tids.items() if lbl != -1}
        if not non_noise or len(non_noise) == 1:
            refined.append(c)
            continue

        # Split into refined clusters; noise (-1) collapses into the largest refined group
        noise_tids = label_to_tids.get(-1, [])
        # Choose largest cluster to absorb noise
        target_label = max(non_noise.items(), key=lambda kv: len(kv[1]))[0]
        non_noise[target_label].extend(noise_tids)

        base_id = c["cluster_id"]
        sender = c.get("sender_domain") or ""
        for idx, tids in non_noise.items():
            if len(tids) < 3:
                # very small; keep in original parent cluster
                continue
            sub_id = f"{base_id}-{idx}"
            # Aggregate subjects and bodies from constituent threads
            subjects: List[str] = []
            bodies: List[str] = []
            for tid in tids:
                t = threads_by_id.get(tid)
                if not t:
                    continue
                for s in t.get("subjects") or []:
                    if s and s not in subjects:
                        subjects.append(s)
                for b in t.get("body_samples") or []:
                    if b and b not in bodies:
                        bodies.append(b)
                        if len(bodies) >= 5:
                            break
            refined.append(
                {
                    "cluster_id": sub_id,
                    "sender_domain": sender,
                    "threads": tids,
                    "subjects": subjects,
                    "body_samples": bodies[:5],
                }
            )

    return refined


def main() -> None:
    ensure_data_dir()
    clusters: List[Dict[str, Any]] = json_read(DATA_PATHS.clusters)
    threads: List[Dict[str, Any]] = json_read(DATA_PATHS.threads)
    new_clusters = refine_clusters(clusters, threads)
    json_write(DATA_PATHS.clusters, new_clusters)

    # Update cluster_stats with refined counts where possible
    stats = {}
    if DATA_PATHS.cluster_stats.exists():
        try:
            stats = json_read(DATA_PATHS.cluster_stats)
        except Exception:
            stats = {}
    stats["refined_cluster_count"] = len(new_clusters)
    json_write(DATA_PATHS.cluster_stats, stats)
    print(f"Refined clusters; new count {len(new_clusters)} written to {DATA_PATHS.clusters}")


if __name__ == "__main__":
    main()

