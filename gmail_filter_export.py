from __future__ import annotations

"""
Export Gmail filters for the generated rules.

Supports two modes:
- XML export for manual import into Gmail web UI
- Direct filter creation via Gmail API (optional)
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

from .config import DATA_PATHS, ensure_data_dir
from .utils import json_read


XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom" xmlns:apps="http://schemas.google.com/apps/2006">\n'
XML_FOOTER = "</feed>\n"


@dataclass
class ExportFilter:
    query: str
    label: str
    skip_inbox: bool = True


def load_rules() -> List[ExportFilter]:
    rules: List[Dict[str, Any]] = json_read(DATA_PATHS.filter_rules)
    filters: List[ExportFilter] = []
    for r in rules:
        query = r.get("gmail_query") or ""
        label = r.get("label") or ""
        if not query or not label:
            continue
        filters.append(ExportFilter(query=query, label=label, skip_inbox=True))
    return filters


def to_xml(filters: List[ExportFilter]) -> str:
    parts = [XML_HEADER]
    for f in filters:
        parts.append("  <entry>")
        parts.append("    <category term=\"filter\"></category>")
        parts.append("    <title>Mail Filter</title>")
        parts.append("    <content/>")
        parts.append("    <apps:property name=\"hasTheWord\" value=\"{}\"/>".format(f.query.replace('"', "&quot;")))
        if f.skip_inbox:
            parts.append("    <apps:property name=\"shouldArchive\" value=\"true\"/>")
        parts.append("    <apps:property name=\"label\" value=\"{}\"/>".format(f.label.replace('"', "&quot;")))
        parts.append("  </entry>")
    parts.append(XML_FOOTER)
    return "\n".join(parts)


def export_xml(path: Optional[Path] = None) -> Path:
    ensure_data_dir()
    filters = load_rules()
    xml = to_xml(filters)
    if path is None:
        path = DATA_PATHS.base / "gmail_filters.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    return path


def create_filters_via_api(service, user_id: str = "me") -> None:
    """
    Create Gmail filters using the Gmail API.
    """
    filters = load_rules()
    for f in filters:
        body = {
            "criteria": {
                "query": f.query,
            },
            "action": {
                "addLabelIds": [],  # labels managed separately in backfill
                "removeLabelIds": ["INBOX"] if f.skip_inbox else [],
            },
        }
        service.users().settings().filters().create(userId=user_id, body=body).execute()


def main() -> None:
    path = export_xml()
    print(f"Gmail filters XML exported to {path}")


if __name__ == "__main__":
    main()

