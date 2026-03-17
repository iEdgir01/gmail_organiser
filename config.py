import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataPaths:
    base: Path = Path(__file__).resolve().parent / "data"

    @property
    def messages(self) -> Path:
        return self.base / "messages.jsonl"

    @property
    def headers(self) -> Path:
        return self.base / "headers.jsonl"

    @property
    def message_ids(self) -> Path:
        return self.base / "message_ids.txt"

    @property
    def threads(self) -> Path:
        return self.base / "threads.json"

    @property
    def clusters(self) -> Path:
        return self.base / "clusters.json"

    @property
    def classified_clusters(self) -> Path:
        return self.base / "classified_clusters.json"

    @property
    def filter_rules(self) -> Path:
        return self.base / "filter_rules.json"

    @property
    def cluster_stats(self) -> Path:
        return self.base / "cluster_stats.json"

    @property
    def rule_stats(self) -> Path:
        return self.base / "rule_stats.json"


DATA_PATHS = DataPaths()


def ensure_data_dir() -> None:
    DATA_PATHS.base.mkdir(parents=True, exist_ok=True)


def getenv_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}

