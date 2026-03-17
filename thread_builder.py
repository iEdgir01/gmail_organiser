from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Dict, List, Any

from .config import DATA_PATHS, ensure_data_dir
from .utils import jsonl_read, json_write


@dataclass
class ThreadSummary:
    thread_id: str
    sender_domain: str
    subjects: List[str]
    subject_norms: List[str]
    body_samples: List[str]
    message_count: int


def build_threads(messages_path=DATA_PATHS.messages) -> Dict[str, ThreadSummary]:
    threads: Dict[str, ThreadSummary] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for msg in jsonl_read(messages_path):
        grouped[msg["thread_id"]].append(msg)

    for thread_id, msgs in grouped.items():
        msgs_sorted = sorted(msgs, key=lambda m: m["timestamp"])
        sender_domain = msgs_sorted[0].get("domain") or ""
        subjects = list({m.get("subject") or "" for m in msgs_sorted if m.get("subject")})
        subject_norms = list(
            {m.get("subject_norm") or "" for m in msgs_sorted if m.get("subject_norm")}
        )
        body_excerpts = [m.get("body_excerpt") or "" for m in msgs_sorted]
        body_samples = [b for b in body_excerpts if b][:3]

        threads[thread_id] = ThreadSummary(
            thread_id=thread_id,
            sender_domain=sender_domain,
            subjects=subjects,
            subject_norms=subject_norms,
            body_samples=body_samples,
            message_count=len(msgs_sorted),
        )
    return threads


def main() -> None:
    ensure_data_dir()
    threads = build_threads()
    serializable = [asdict(t) for t in threads.values()]
    json_write(DATA_PATHS.threads, serializable)
    print(f"Wrote {len(serializable)} threads to {DATA_PATHS.threads}")


if __name__ == "__main__":
    main()

