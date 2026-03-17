from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Any

from .config import DATA_PATHS, ensure_data_dir
from .utils import json_read, json_write


def group_threads_by_domain(threads: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in threads:
        domain = (t.get("sender_domain") or "").lower()
        if not domain:
            domain = "unknown"
        grouped[domain].append(t)
    return grouped


def main() -> None:
    ensure_data_dir()
    threads: List[Dict[str, Any]] = json_read(DATA_PATHS.threads)
    grouped = group_threads_by_domain(threads)
    # For simplicity, we persist grouped info merged into clusters stage instead
    # of a dedicated file. This function primarily exists as a pipeline stage.
    out = {domain: [t["thread_id"] for t in ts] for domain, ts in grouped.items()}
    json_write(DATA_PATHS.cluster_stats, {"threads_by_domain": out})
    print(f"Grouped {len(threads)} threads across {len(grouped)} domains")


if __name__ == "__main__":
    main()

