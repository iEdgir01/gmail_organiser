from __future__ import annotations

"""
Thread-aware backfill of labels based on generated rules.

Process:
- For each rule, run Gmail search with the rule's query.
- Collect message results and their threadIds.
- Deduplicate threadIds.
- Apply label to entire thread.
"""

from dataclasses import dataclass
from typing import List, Dict, Any

from .config import DATA_PATHS, ensure_data_dir
from .utils import json_read


@dataclass
class BackfillRule:
    label: str
    gmail_query: str


def load_backfill_rules() -> List[BackfillRule]:
    rules: List[Dict[str, Any]] = json_read(DATA_PATHS.filter_rules)
    return [
        BackfillRule(label=r["label"], gmail_query=r.get("gmail_query") or "")
        for r in rules
        if r.get("gmail_query") and r.get("label")
    ]


def ensure_label(service, user_id: str, name: str) -> str:
    existing = service.users().labels().list(userId=user_id).execute()
    for lbl in existing.get("labels", []):
        if lbl.get("name") == name:
            return lbl["id"]
    body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    created = service.users().labels().create(userId=user_id, body=body).execute()
    return created["id"]


def search_messages(service, user_id: str, query: str) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    page_token = None
    while True:
        kwargs: Dict[str, Any] = {"userId": user_id, "q": query, "includeSpamTrash": False}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        batch = resp.get("messages", []) or []
        messages.extend(batch)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def apply_label_to_threads(service, user_id: str, label_id: str, thread_ids: List[str]) -> None:
    body = {"addLabelIds": [label_id], "removeLabelIds": []}
    for tid in thread_ids:
        service.users().threads().modify(userId=user_id, id=tid, body=body).execute()


def backfill(service, user_id: str = "me") -> None:
    ensure_data_dir()
    rules = load_backfill_rules()
    for r in rules:
        label_id = ensure_label(service, user_id, r.label)
        msgs = search_messages(service, user_id, r.gmail_query)
        thread_ids = {m["threadId"] for m in msgs if "threadId" in m}
        apply_label_to_threads(service, user_id, label_id, list(thread_ids))


def main() -> None:
    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "google-api-python-client and google-auth are required "
            "to run gmail_backfill.py. Install them with:\n"
            "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from exc

    scopes = ["https://www.googleapis.com/auth/gmail.modify"]
    creds = None
    if Path("token.json").exists():  # type: ignore[name-defined]
        creds = Credentials.from_authorized_user_file("token.json", scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise SystemExit(
                "OAuth token.json not present or invalid. "
                "Run a Gmail API quickstart to generate it."
            )

    service = build("gmail", "v1", credentials=creds)
    backfill(service)
    print("Thread-aware backfill completed.")


if __name__ == "__main__":
    main()

