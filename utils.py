import base64
import json
import re
from pathlib import Path
from typing import Iterable, List, Dict, Any


SUBJECT_NUMBER_RE = re.compile(r"\b\d{4,}\b")
WHITESPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_subject(subject: str) -> str:
    """
    Lowercase, remove long numbers, strip punctuation, collapse whitespace.
    """
    s = subject.lower().strip()
    s = SUBJECT_NUMBER_RE.sub(" ", s)
    s = PUNCT_RE.sub(" ", s)
    s = WHITESPACE_RE.sub(" ", s)
    tokens = [t for t in s.split(" ") if t]
    # Heuristic: drop common prefixes like "re", "fw"
    if tokens and tokens[0] in {"re", "fw", "fwd"}:
        tokens = tokens[1:]
    return " ".join(tokens)


def extract_domain(from_header: str) -> str:
    """
    Extract sender domain from a From header like:
    "Amazon Billing <billing@amazon.com>" -> "amazon.com"
    """
    m = re.search(r"<([^>]+)>", from_header)
    email = m.group(1) if m else from_header
    at_idx = email.rfind("@")
    if at_idx == -1:
        return ""
    domain = email[at_idx + 1 :].strip().lower()
    return domain


def decode_base64url(data: str) -> bytes:
    """Decode Gmail API base64url encoded strings."""
    # Gmail uses URL-safe base64 without padding
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def jsonl_write(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def jsonl_read(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def json_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def json_read(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def collect_body_text_from_parts(parts: List[Dict[str, Any]]) -> str:
    """
    Walk Gmail payload parts, preferring text/plain but falling back to html.
    """
    text_plain_candidates: List[str] = []
    html_candidates: List[str] = []

    def walk(part: Dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if part.get("parts"):
            for p in part["parts"]:
                walk(p)
        if not data:
            return
        try:
            decoded = decode_base64url(data).decode("utf-8", errors="ignore")
        except Exception:
            return
        if mime_type.startswith("text/plain"):
            text_plain_candidates.append(decoded)
        elif mime_type.startswith("text/html"):
            html_candidates.append(decoded)

    for p in parts:
        walk(p)

    if text_plain_candidates:
        return "\n".join(text_plain_candidates)
    if html_candidates:
        # simple HTML strip
        html = "\n".join(html_candidates)
        html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
        html = re.sub(r"(?s)<[^>]+>", " ", html)
        html = WHITESPACE_RE.sub(" ", html)
        return html
    return ""

