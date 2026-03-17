"""
auto_classify.py — AI-powered Gmail sender classifier.

Replaces the hardcoded RULES-based classifier in analyze.py with an
AI-driven classifier that:
  - Groups messages by sender from data/headers.jsonl
  - Classifies each sender via OpenAI / Anthropic (with fallback)
  - Caches decisions in data/auto_classifications.json
  - Emits output/filter_rules.json in the same format used by:
        apply_labels.py, backfill.py, export_filters_xml.py
  - Writes a human-readable summary to output/auto_classify_report.txt

It NEVER stores message bodies on disk; bodies are fetched on-the-fly only to
build prompts for the model.

Requires at least one of the following environment variables:
  - OPENAI_API_KEY
  - ANTHROPIC_API_KEY

Recommended Python dependencies (see project docs/requirements):
  openai>=1.30.0
  anthropic>=0.28.0
  tqdm>=4.66.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

from backfill import TokenManager, load_token, API_BASE  # type: ignore

try:
    from openai import AsyncOpenAI  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    AsyncOpenAI = None  # type: ignore

try:
    from anthropic import AsyncAnthropic  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    AsyncAnthropic = None  # type: ignore


BASE = Path(__file__).parent
HDR_FILE = BASE / "data" / "headers.jsonl"
CACHE_FILE = BASE / "data" / "auto_classifications.json"
API_KEYS_FILE = BASE / "data" / "api_keys.json"
OUT_DIR = BASE / "output"
OUT_FILTER_JSON = OUT_DIR / "filter_rules.json"
OUT_REPORT = OUT_DIR / "auto_classify_report.txt"


# ── Constants (must stay in sync with label taxonomy) ───────────────────────────

CONFIDENCE_THRESHOLD = 0.80

VALID_LABELS = {
    "Finance/Banking",
    "Finance/Investing",
    "Finance/Tax",
    "Finance/Payments",
    "Finance/Bills",
    "Finance/Debt",
    "Finance/Loan Offers",
    "Insurance/Personal",
    "Insurance/Quotes",
    "Dev/Hosting",
    "Dev/AI",
    "Dev/Netlify Forms",
    "Dev",
    "Accounts/Google",
    "Accounts/Microsoft",
    "Accounts/Apple",
    "Accounts/Facebook",
    "Accounts/Instagram",
    "Accounts/Twitter",
    "Accounts/LinkedIn",
    "Accounts/Security",
    "Accounts/Amazon",
    "Transport",
    "Transport/Alerts",
    "Transport/Vehicle",
    "Gaming",
    "Social/LinkedIn",
    "Social/Facebook",
    "Social/Twitter",
    "Social/Instagram",
    "Social/Reddit",
    "Social/Discord",
    "Social/Snapchat",
    "Social/TikTok",
    "Social/Dating",
    "Social",
    "Jobs/LinkedIn",
    "Jobs/Indeed",
    "Jobs",
    "Shopping",
    "Entertainment",
    "Lifestyle",
    "Property",
    "Travel",
    "Travel/Booking",
    "Personal",
    "Personal/Family",
    "Personal/Rentals",
    "Personal/Repairs",
    "Personal/Services",
    "Personal/Solar",
    "Personal/Cancellations",
    "Business/Yeha",
    "Business/Umbusobee",
    "Technical/Support",
    "System/Bounces",
    "Promotions",
    "Unsubscribe Queue",
    "Uncategorised",
}

MODEL_NAMES = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "manual": "manual",
    "failed": "none",
}

DEFAULT_PROVIDER_ORDER = ["openai", "anthropic"]
CACHE_CHECKPOINT_INTERVAL = 25
MAX_CONCURRENT_REQUESTS = 5
MAX_BODY_LENGTH_CHARS = 500
MAX_BODIES_PER_SENDER = 3
MAX_SUBJECTS_PER_SENDER = 15


SYSTEM_PROMPT = """
You are an email classification assistant for a personal Gmail inbox.

Your job is to classify each email SENDER into exactly ONE label and ONE tier.
Never assign multiple categories at once – pick the single best label+tier that
matches the typical content the user actually cares about keeping.

You MUST base your decision on ALL of:
  - who the sender is (person vs company, bank, insurer, retailer, etc.)
  - the SUBJECT LINES (strongest signal)
  - any EMAIL BODY EXCERPTS provided

CRITICAL: Do NOT classify only from the domain name when there are subjects or
body text. Subject and body always override vague assumptions from the domain.

TIER DEFINITIONS:
  1 = IMPORTANT  — Keep in inbox.
                   Use for: banks, billing, insurance, account security alerts,
                   support tickets the user raised, personal contacts, business contacts.
  2 = USEFUL     — Archive (skip inbox), leave unread.
                   Use for: jobs, gaming, social media, dev tools, entertainment,
                   shopping alerts, property listings.
  3 = LOW NOISE  — Archive, leave unread.
                   Use for: newsletters, digests, light promotional emails the
                   user occasionally reads.
  4 = UNSUBSCRIBE — Archive, mark as read.
                   Use for: pure marketing spam the user wants to eventually delete.

LABEL OPTIONS (use exactly as written):
  Finance/Banking       Finance/Investing     Finance/Tax
  Finance/Payments      Finance/Bills         Finance/Debt
  Finance/Loan Offers   Insurance/Personal    Insurance/Quotes
  Dev/Hosting           Dev/AI                Dev/Netlify Forms    Dev
  Accounts/Google       Accounts/Microsoft    Accounts/Apple
  Accounts/Facebook     Accounts/Instagram    Accounts/Twitter
  Accounts/LinkedIn     Accounts/Security     Accounts/Amazon
  Transport             Transport/Alerts      Transport/Vehicle
  Gaming                Social/LinkedIn       Social/Facebook
  Social/Twitter        Social/Instagram      Social/Reddit
  Social/Discord        Social/Snapchat       Social/TikTok
  Social/Dating         Social
  Jobs/LinkedIn         Jobs/Indeed           Jobs
  Shopping              Entertainment         Lifestyle
  Property              Travel                Travel/Booking
  Personal              Personal/Family       Personal/Rentals
  Personal/Repairs      Personal/Services     Personal/Solar
  Personal/Cancellations
  Business/Yeha         Business/Umbusobee
  Technical/Support     System/Bounces        Promotions
  Unsubscribe Queue     Uncategorised

IMPORTANT CONTEXT — South African inbox:
  - Discovery Bank, FNB, ABSA, WesBank = South African banks → Finance/Banking Tier 1
  - Vodacom, MTN, Rain, Supersonic, MetroFibre = SA telecoms/ISPs → Finance/Bills Tier 1
  - Takealot = SA e-commerce (like Amazon) → Shopping
  - Bolt = SA ride-hailing (like Uber) → Transport
  - King Price, Pineapple, Naked, OUTsurance, Dotsure = SA insurers → Insurance/Personal Tier 1
  - EasyEquities, 22seven, ClearScore = SA investing → Finance/Investing Tier 1
  - If a sender is a bank, ISP bill, or insurance company: always Tier 1
  - If subjects look like receipts/invoices from any sender: Tier 1

CRITICAL SAFETY RULES:
  - Only use Accounts/Security when subjects clearly indicate security events
    (logins, password/2FA codes, suspicious activity, account protection).
    Do NOT use it for generic marketing or normal conversation replies.

  - FINANCE vs PROMOTIONS vs UNSUBSCRIBE:
      * Finance/Bills, Finance/Payments, Insurance/Personal and similar MUST be
        reserved for real bills, invoices, statements, and policy/contract
        administration (e.g. "Invoice", "Statement", "Payment received",
        "Account overdue", "Policy schedule", "Contract signed").
      * If a message is clearly a promotion, discount, "free storage", "special
        offer", "sale", or marketing campaign – even if it comes from a bank,
        insurer, or a person – it belongs in Promotions or Unsubscribe Queue,
        NOT a Finance/… or Insurance/… label.
      * Example: a subject like "100GB Free cloud storage for all active
        accounts" is a marketing promotion and should be Promotions or
        Unsubscribe Queue, NOT Finance/Bills.
      * Use Unsubscribe Queue ONLY for pure marketing senders the user is likely
        to unsubscribe from (bulk marketing, newsletters, repeated promos with
        unsubscribe links). Never place genuine invoices, bills, contracts, or
        insurance communications in Unsubscribe Queue.

  - PERSONAL vs BUSINESS vs GENERIC:
      * Direct 1:1 human conversations (friends, family, landlords, repair
        people, small contractors) with conversational subjects like "Hi",
        "Quick question", "Thank you", "Photos", etc. should be Personal/…
        unless they are clearly contracts, invoices, or insurance documents.
      * Emails about leases, quotes, repairs, and service bookings that include
        pricing, dates, or terms can go under Personal/Repairs, Personal/Rentals,
        Personal/Services, or Business/… depending on context.

  - CONTRACTS & INSURANCE:
      * Messages that contain contracts, policy wording, schedules, renewal
        notices, or claim decisions belong under Insurance/Personal or an
        appropriate Personal/… or Business/… label with Tier 1.
      * Do NOT downgrade these to Promotions or Unsubscribe Queue even if they
        are formatted like marketing – the contractual content is more important.

  - If subjects are missing or uninformative, lower your confidence and rely
    on body excerpts where provided rather than guessing from sender/domain.

Respond with a JSON object only. No text outside the JSON.
{
  "label": "<label from list above>",
  "tier": <1|2|3|4>,
  "confidence": <0.0–1.0>,
  "reasoning": "<one sentence explaining your choice>"
}
""".strip()


# ── Types ──────────────────────────────────────────────────────────────────────


SenderSummary = Dict[str, Any]
CacheEntry = Dict[str, Any]


@dataclass
class ProviderConfig:
    name: str
    available: bool


# ── Helpers: data loading & grouping ───────────────────────────────────────────


_API_KEYS_CACHE: Dict[str, str] = {}


def _load_api_keys_from_disk() -> None:
    """
    Load API keys from API_KEYS_FILE into the in-memory cache.
    """
    global _API_KEYS_CACHE
    if _API_KEYS_CACHE:
        return
    if not API_KEYS_FILE.exists():
        _API_KEYS_CACHE = {}
        return
    try:
        data = json.loads(API_KEYS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _API_KEYS_CACHE = {str(k): str(v) for k, v in data.items() if v}
        else:
            _API_KEYS_CACHE = {}
    except Exception:
        _API_KEYS_CACHE = {}


def _save_api_keys_to_disk() -> None:
    """
    Persist the in-memory API key cache to API_KEYS_FILE.
    """
    if not _API_KEYS_CACHE:
        return
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(_API_KEYS_CACHE)
    API_KEYS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _get_api_key(name: str, env_var: str) -> Optional[str]:
    """
    Return the API key for a provider, checking (in order):
      1. Process environment variable (highest priority)
      2. Project-local api_keys.json file (API_KEYS_FILE)
    """
    val = os.environ.get(env_var)
    if val:
        return val
    _load_api_keys_from_disk()
    return _API_KEYS_CACHE.get(env_var) or None


def load_headers(path: Path) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    if not path.exists():
        print(f"ERROR: {path} not found. Run  python fetch.py  first.")
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                # Skip malformed lines
                continue
    return msgs


def extract_addr(frm: str) -> str:
    m = re.search(r"<([^>]+)>", frm)
    addr = m.group(1) if m else frm
    return addr.lower().strip()


def group_by_sender(msgs: List[Dict[str, Any]], min_msgs: int) -> Dict[str, SenderSummary]:
    grouped: Dict[str, SenderSummary] = {}
    for msg in msgs:
        addr = extract_addr(msg.get("from", "") or "")
        if not addr:
            continue
        subj = msg.get("subject", "") or ""
        entry = grouped.get(addr)
        if entry is None:
            entry = {
                "addr": addr,
                "domain": addr.split("@", 1)[-1] if "@" in addr else addr,
                "total": 0,
                "subjects": Counter(),
                "sample_ids": [],
            }
            grouped[addr] = entry
        entry["total"] += 1
        if subj:
            entry["subjects"][subj] += 1
        if len(entry["sample_ids"]) < 5:
            msg_id = msg.get("id")
            if msg_id:
                entry["sample_ids"].append(msg_id)

    # Apply min_msgs filter and trim subjects
    result: Dict[str, SenderSummary] = {}
    for addr, info in grouped.items():
        if info["total"] < min_msgs:
            continue
        subjects_counter: Counter = info["subjects"]
        top_subjects = Counter()
        for subject, count in subjects_counter.most_common(MAX_SUBJECTS_PER_SENDER):
            top_subjects[subject] = count
        info["subjects"] = top_subjects
        result[addr] = info
    return result


# ── Cache layer ────────────────────────────────────────────────────────────────


def load_cache(path: Path) -> Dict[str, CacheEntry]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        senders = data.get("senders")
        if isinstance(senders, dict):
            return senders  # type: ignore[return-value]
        return {}
    except Exception:
        return {}


def save_cache(cache: Dict[str, CacheEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"version": 1, "senders": cache}
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def is_cached(addr: str, cache: Dict[str, CacheEntry]) -> bool:
    entry = cache.get(addr)
    if not entry:
        return False
    # Manual overrides are always respected; non-manual entries are also used as-is.
    return True


# ── Prompt builders ────────────────────────────────────────────────────────────


def build_user_message(sender: SenderSummary) -> str:
    addr = sender["addr"]
    domain = sender["domain"]
    total = sender["total"]
    subjects: Counter = sender.get("subjects") or Counter()

    lines: List[str] = []
    a = lines.append
    a(f"Sender: {addr}")
    a(f"Domain: {domain}")
    a(f"Message count: {total}")
    a("")
    a("Top subjects (count — subject):")

    if subjects:
        for subject, count in subjects.most_common(MAX_SUBJECTS_PER_SENDER):
            a(f"  {count:>3} — {subject}")
    else:
        a("  (no readable subjects)")

    return "\n".join(lines)


def build_user_message_with_body(sender: SenderSummary, bodies: List[str]) -> str:
    base = build_user_message(sender)
    if not bodies:
        return base

    lines: List[str] = [base, "", "Email body excerpts (first 300 chars each):"]
    for idx, body in enumerate(bodies[:MAX_BODIES_PER_SENDER], start=1):
        snippet = body[:300]
        lines.append(f"--- Body {idx} ---")
        lines.append(snippet)
        lines.append("")
    return "\n".join(lines).rstrip()


# ── Provider clients ──────────────────────────────────────────────────────────


def validate_classification(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    label = result.get("label")
    tier = result.get("tier")
    confidence = result.get("confidence")
    if not isinstance(label, str) or label not in VALID_LABELS:
        return False
    if tier not in (1, 2, 3, 4):
        return False
    try:
        c_val = float(confidence)
    except Exception:
        return False
    if not (0.0 <= c_val <= 1.0):
        return False
    return True


async def classify_with_openai(user_message: str, system_prompt: str) -> Optional[Dict[str, Any]]:
    api_key = _get_api_key("openai", "OPENAI_API_KEY")
    if not api_key or AsyncOpenAI is None:
        return None
    try:
        client = AsyncOpenAI(api_key=api_key)
        resp = await client.chat.completions.create(
            model=MODEL_NAMES["openai"],
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_tokens=256,
            timeout=15,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        return data
    except Exception:
        return None


async def classify_with_anthropic(user_message: str, system_prompt: str) -> Optional[Dict[str, Any]]:
    api_key = _get_api_key("anthropic", "ANTHROPIC_API_KEY")
    if not api_key or AsyncAnthropic is None:
        return None
    try:
        client = AsyncAnthropic(api_key=api_key)
        tools = [
            {
                "name": "classify_sender",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "tier": {"type": "integer", "enum": [1, 2, 3, 4]},
                        "confidence": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["label", "tier", "confidence", "reasoning"],
                },
            }
        ]
        resp = await client.messages.create(
            model=MODEL_NAMES["anthropic"],
            max_tokens=256,
            tools=tools,
            tool_choice={"type": "tool", "name": "classify_sender"},
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        for item in resp.content:
            if getattr(item, "type", None) == "tool_use" and item.name == "classify_sender":
                return dict(item.input)  # type: ignore[arg-type]
        return None
    except Exception:
        return None


async def classify_with_escalation(
    user_message: str,
    system_prompt: str,
    providers: List[str],
) -> Tuple[Optional[Dict[str, Any]], str]:
    for provider in providers:
        if provider == "openai":
            res = await classify_with_openai(user_message, system_prompt)
        elif provider == "anthropic":
            res = await classify_with_anthropic(user_message, system_prompt)
        else:
            continue

        if res is None:
            continue
        if not validate_classification(res):
            continue
        return res, provider
    return None, "failed"


# ── Gmail body fetching ────────────────────────────────────────────────────────


def _extract_plain_text_from_payload(payload: Dict[str, Any]) -> str:
    # Walk the payload structure to find text/plain or text/html parts.
    def walk(part: Dict[str, Any]) -> List[Tuple[str, str]]:
        results: List[Tuple[str, str]] = []
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if isinstance(data, str) and mime in ("text/plain", "text/html"):
            results.append((mime, data))
        for child in part.get("parts") or []:
            results.extend(walk(child))
        return results

    parts = walk(payload)
    if not parts:
        return ""

    import base64
    import html

    # Prefer plain text
    plain = next((p for p in parts if p[0] == "text/plain"), parts[0])
    mime, b64 = plain
    try:
        raw = base64.urlsafe_b64decode(b64 + "==").decode("utf-8", errors="ignore")
    except Exception:
        raw = ""
    if mime == "text/html":
        # Very simple HTML stripper
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = html.unescape(raw)
    raw = " ".join(raw.split())
    return raw[:MAX_BODY_LENGTH_CHARS]


def fetch_body_text(session: requests.Session, token_mgr: TokenManager, msg_id: str) -> str:
    try:
        url = f"{API_BASE}/messages/{msg_id}"
        resp = session.get(
            url,
            headers=token_mgr.headers,
            params={"format": "full"},
            timeout=30,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        payload = data.get("payload") or {}
        text = _extract_plain_text_from_payload(payload)
        return text
    except Exception:
        return ""


async def fetch_bodies_for_sender(
    session: requests.Session,
    token_mgr: TokenManager,
    sample_ids: List[str],
    n: int = MAX_BODIES_PER_SENDER,
) -> List[str]:
    if not sample_ids:
        return []

    async def _one(msg_id: str) -> str:
        return await asyncio.to_thread(fetch_body_text, session, token_mgr, msg_id)

    tasks = [asyncio.create_task(_one(mid)) for mid in sample_ids[:n]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    bodies: List[str] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        if r:
            bodies.append(r)
    return bodies[:n]


# ── Main classification loop ───────────────────────────────────────────────────


async def classify_all_senders(
    senders: Dict[str, SenderSummary],
    cache: Dict[str, CacheEntry],
    providers: List[str],
    token_mgr: Optional[TokenManager],
    session: Optional[requests.Session],
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    dry_run: bool = False,
    reclassify: bool = False,
    uncertain_only: bool = False,
) -> Dict[str, CacheEntry]:
    addresses = list(senders.keys())

    provider_counts = Counter()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    processed = 0

    async def process_addr(addr: str) -> None:
        nonlocal processed
        sender = senders[addr]
        cached = cache.get(addr)

        if cached and not reclassify:
            if uncertain_only and float(cached.get("confidence", 0.0)) >= confidence_threshold:
                processed += 1
                return
            if not uncertain_only:
                processed += 1
                return

        if dry_run:
            # In dry-run mode, still show the full context we would send, including
            # body excerpts when available.
            method = "subjects"
            user_message = build_user_message(sender)
            if session is not None and token_mgr is not None and sender.get("sample_ids"):
                bodies = await fetch_bodies_for_sender(session, token_mgr, sender["sample_ids"])
                if bodies:
                    method = "body"
                    user_message = build_user_message_with_body(sender, bodies)
            print("-" * 40)
            print(user_message)
            processed += 1
            return

        async with semaphore:
            # Single-pass classification: ALWAYS include body excerpts when possible.
            method = "subjects"
            user_message = build_user_message(sender)
            if session is not None and token_mgr is not None and sender.get("sample_ids"):
                bodies = await fetch_bodies_for_sender(session, token_mgr, sender["sample_ids"])
                if bodies:
                    method = "body"
                    user_message = build_user_message_with_body(sender, bodies)

            result, provider = await classify_with_escalation(user_message, SYSTEM_PROMPT, providers)

            if result is None:
                result = {
                    "label": "Uncategorised",
                    "tier": 2,
                    "confidence": 0.0,
                    "reasoning": "All providers failed",
                }
                provider = "failed"

            provider_counts[provider] += 1
            cache[addr] = {
                "label": result["label"],
                "tier": result["tier"],
                "confidence": float(result.get("confidence", 0.0)),
                "reasoning": result.get("reasoning", ""),
                "method": method,
                "provider": provider,
                "model": MODEL_NAMES.get(provider, "none"),
                "message_count": sender["total"],
                "manual_override": cache.get(addr, {}).get("manual_override", False),
            }

            processed += 1
            if processed % CACHE_CHECKPOINT_INTERVAL == 0:
                save_cache(cache, CACHE_FILE)

    # Run with progress bar
    tasks = [process_addr(addr) for addr in addresses]
    for f in tqdm(
        asyncio.as_completed(tasks),
        total=len(tasks),
        desc="Classifying senders",
        unit="sender",
    ):
        await f

    # Final checkpoint
    if not dry_run:
        save_cache(cache, CACHE_FILE)

    # Summary
    if not dry_run:
        parts = []
        total = sum(provider_counts.values())
        for name in ["openai", "anthropic", "failed"]:
            cnt = provider_counts.get(name, 0)
            if total:
                pct = (cnt / total) * 100
                parts.append(f"{name}:{cnt} ({pct:.1f}%)")
        if parts:
            print("Provider usage:", ", ".join(parts))

    return cache


# ── Output generation ─────────────────────────────────────────────────────────


def build_filter_rules(cache: Dict[str, CacheEntry], senders: Dict[str, SenderSummary]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, int], List[str]] = defaultdict(list)
    for addr, entry in cache.items():
        label = entry.get("label")
        tier = entry.get("tier")
        if not label or label not in VALID_LABELS:
            continue
        if tier not in (1, 2, 3, 4):
            continue
        groups[(label, int(tier))].append(addr)

    rules: List[Dict[str, Any]] = []
    for (label, tier), patterns in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        rules.append(
            {
                "label": label,
                "tier": tier,
                "patterns": sorted(set(patterns)),
                "keep_inbox": tier == 1,
                "mark_read": tier == 4,
                "skip_filter": False,
            }
        )
    return rules


def write_filter_rules_json(rules: List[Dict[str, Any]], total_messages: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_from": "auto_classify.py",
        "total_messages": total_messages,
        "rules": rules,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_report(cache: Dict[str, CacheEntry], senders: Dict[str, SenderSummary], path: Path) -> None:
    total_senders = len(senders)
    entries = {addr: entry for addr, entry in cache.items() if addr in senders}

    provider_counts = Counter(entry.get("provider", "unknown") for entry in entries.values())

    conf_buckets = Counter()
    for entry in entries.values():
        c = float(entry.get("confidence", 0.0))
        if c >= 0.9:
            conf_buckets[">=0.90"] += 1
        elif c >= 0.8:
            conf_buckets["0.80-0.89"] += 1
        elif c >= 0.6:
            conf_buckets["0.60-0.79"] += 1
        else:
            conf_buckets["<0.60"] += 1

    tier_label_counts: Dict[Tuple[int, str], int] = defaultdict(int)
    sender_message_counts: Dict[str, int] = {addr: senders[addr]["total"] for addr in senders}

    for addr, entry in entries.items():
        label = entry.get("label")
        tier = int(entry.get("tier", 0) or 0)
        if label not in VALID_LABELS or tier not in (1, 2, 3, 4):
            continue
        tier_label_counts[(tier, label)] += sender_message_counts.get(addr, 0)

    lines: List[str] = []
    a = lines.append
    a("AUTO-CLASSIFY REPORT")
    a("════════════════════════════════════════")
    a(f"Total senders classified : {len(entries):,} / {total_senders:,}")
    a(
        f"  OpenAI primary         : {provider_counts.get('openai', 0):>5}  "
        f"Anthropic fallback : {provider_counts.get('anthropic', 0):>5}"
    )
    a(f"  Failed / Uncategorised : {provider_counts.get('failed', 0):>5}")
    a("")
    a("Confidence distribution:")
    a(f"  ≥ 0.90   : {conf_buckets['>=0.90']:>5}")
    a(f"  0.80–0.89: {conf_buckets['0.80-0.89']:>5}")
    a(f"  0.60–0.79: {conf_buckets['0.60-0.79']:>5}")
    a(f"  < 0.60   : {conf_buckets['<0.60']:>5}")
    a("")

    tier_titles = {
        1: "TIER 1 — IMPORTANT (keep inbox)",
        2: "TIER 2 — USEFUL (archive, leave unread)",
        3: "TIER 3 — LOW NOISE (archive, leave unread)",
        4: "TIER 4 — UNSUBSCRIBE (archive, mark read)",
    }

    for tier in (1, 2, 3, 4):
        a("────────────────────────────────────────")
        a(tier_titles[tier])
        a("────────────────────────────────────────")
        # Sum by label
        label_counts: List[Tuple[str, int]] = []
        for (t, label), count in tier_label_counts.items():
            if t == tier:
                label_counts.append((label, count))
        for label, count in sorted(label_counts, key=lambda x: (-x[1], x[0])):
            # Find top sender for this label+tier
            top_addr = ""
            top_count = -1
            for addr, entry in entries.items():
                if entry.get("label") == label and int(entry.get("tier", 0) or 0) == tier:
                    mc = sender_message_counts.get(addr, 0)
                    if mc > top_count:
                        top_count = mc
                        top_addr = addr
            a(f"  {label:<28} {count:>7,}  (top: {top_addr})")
        a("")

    # Low confidence section
    a("────────────────────────────────────────")
    a("LOW CONFIDENCE — REVIEW RECOMMENDED")
    a("────────────────────────────────────────")
    low_conf = [
        (addr, entry)
        for addr, entry in entries.items()
        if float(entry.get("confidence", 0.0)) < 0.7
    ]
    for addr, entry in sorted(
        low_conf, key=lambda it: float(it[1].get("confidence", 0.0))
    ):
        c = float(entry.get("confidence", 0.0))
        label = entry.get("label")
        reason = entry.get("reasoning", "")
        a(f"  {c:>4.2f}  {addr:40} → {label}")
        if reason:
            a(f"        {reason}")
    if not low_conf:
        a("  (none)")

    # Failed section
    a("")
    a("────────────────────────────────────────")
    a("FAILED — ALL PROVIDERS FAILED")
    a("────────────────────────────────────────")
    failed = [
        (addr, entry)
        for addr, entry in entries.items()
        if entry.get("provider") == "failed"
    ]
    for addr, entry in failed:
        msgs = sender_message_counts.get(addr, 0)
        a(f"  {addr} ({msgs} msgs)")
    if not failed:
        a("  (none)")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ── CLI / entrypoint ──────────────────────────────────────────────────────────


def _ensure_api_keys_interactive() -> None:
    """
    If no provider API keys are present, prompt the user to enter them
    interactively. Values are stored in a project-local JSON file
    (API_KEYS_FILE) so they can be reused on the next run. Environment
    variables, if set, always take precedence.
    """
    _load_api_keys_from_disk()

    has_oa = bool(_get_api_key("openai", "OPENAI_API_KEY"))
    has_an = bool(_get_api_key("anthropic", "ANTHROPIC_API_KEY"))

    if has_oa or has_an:
        return

    print(
        "No API keys found in environment for OpenAI or Anthropic.\n"
        "You can paste one or more API keys now (leave blank to skip).\n"
    )

    try:
        oa = getpass("OPENAI_API_KEY    (optional): ").strip()
        if oa:
            _API_KEYS_CACHE["OPENAI_API_KEY"] = oa

        an = getpass("ANTHROPIC_API_KEY (optional): ").strip()
        if an:
            _API_KEYS_CACHE["ANTHROPIC_API_KEY"] = an

        if _API_KEYS_CACHE:
            _save_api_keys_to_disk()
    except (EOFError, KeyboardInterrupt):
        # User cancelled input; fall through to normal validation
        print()


def _detect_providers(explicit: Optional[str]) -> List[str]:
    if explicit:
        raw = [p.strip().lower() for p in explicit.split(",") if p.strip()]
        order = [p for p in raw if p in DEFAULT_PROVIDER_ORDER]
    else:
        order = list(DEFAULT_PROVIDER_ORDER)

    available: List[str] = []
    has_oa = bool(_get_api_key("openai", "OPENAI_API_KEY"))
    has_an = bool(_get_api_key("anthropic", "ANTHROPIC_API_KEY"))

    for p in order:
        if p == "openai" and has_oa:
            available.append(p)
        elif p == "anthropic" and has_an:
            available.append(p)

    providers_line = (
        f"Providers : OpenAI {'✓' if has_oa else '✗'}  "
        f"Anthropic {'✓' if has_an else '✗'}"
    )
    print("auto_classify.py — AI-powered Gmail sender classifier")
    print("══════════════════════════════════════════════════════")
    print(providers_line)

    if not (has_oa or has_an):
        print(
            "ERROR: No API keys found. Set at least one of: "
            "OPENAI_API_KEY, ANTHROPIC_API_KEY"
        )
        sys.exit(1)

    if not available:
        # Keys exist but none matched explicit provider selection
        print("ERROR: No providers available after applying --providers filter.")
        sys.exit(1)

    print(f"Order     : " + " → ".join(available))
    return available


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-msgs", type=int, default=1)
    parser.add_argument("--resume", action="store_true", help="Alias for default (cache is always used)")
    parser.add_argument("--reclassify", action="store_true", help="Ignore cache and re-classify all")
    parser.add_argument(
        "--uncertain-only",
        action="store_true",
        help="Re-classify only senders below confidence threshold",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=CONFIDENCE_THRESHOLD,
        help=f"Confidence threshold for body-pass reclassification (default {CONFIDENCE_THRESHOLD})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print prompts, no API calls or writes")
    parser.add_argument(
        "--review-uncertain",
        action="store_true",
        help="Print cached low-confidence entries and exit",
    )
    parser.add_argument(
        "--dump-senders",
        action="store_true",
        help="Print all cached sender classifications (label, tier, confidence) and exit",
    )
    parser.add_argument(
        "--override",
        nargs=3,
        metavar=("ADDR", "LABEL", "TIER"),
        help="Pin manual classification for a sender and exit",
    )
    parser.add_argument(
        "--providers",
        help="Comma-separated provider list to use (subset/order of openai,anthropic)",
    )

    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Give the user a chance to paste keys interactively if none exist.
    _ensure_api_keys_interactive()

    providers = _detect_providers(args.providers)

    # Manual review mode (no API calls)
    cache = load_cache(CACHE_FILE)

    if args.dump_senders:
        print("All cached sender classifications:\n")
        for addr, entry in sorted(cache.items(), key=lambda it: it[0]):
            label = entry.get("label")
            tier = entry.get("tier")
            conf = float(entry.get("confidence", 0.0))
            provider = entry.get("provider")
            print(f"{addr:40} → {label:<24} T{tier}  conf={conf:0.2f}  ({provider})")
        return

    if args.review_uncertain:
        print("Low-confidence cached entries (confidence < 0.70):\n")
        for addr, entry in sorted(
            cache.items(), key=lambda it: float(it[1].get("confidence", 0.0))
        ):
            c = float(entry.get("confidence", 0.0))
            if c >= 0.7:
                continue
            print(
                f"{c:>4.2f}  {addr:40} → {entry.get('label')} "
                f"(tier {entry.get('tier')}, provider {entry.get('provider')})"
            )
            reason = entry.get("reasoning")
            if reason:
                print(f"    {reason}")
        return

    # Manual override mode
    if args.override:
        addr, label, tier_str = args.override
        try:
            tier = int(tier_str)
        except ValueError:
            print("ERROR: TIER must be an integer 1–4")
            sys.exit(1)
        if label not in VALID_LABELS:
            print(f"ERROR: LABEL '{label}' is not in VALID_LABELS")
            sys.exit(1)
        if tier not in (1, 2, 3, 4):
            print("ERROR: TIER must be one of 1,2,3,4")
            sys.exit(1)
        cache[addr] = {
            "label": label,
            "tier": tier,
            "confidence": 1.0,
            "reasoning": "Manual override",
            "method": "manual",
            "provider": "manual",
            "model": MODEL_NAMES["manual"],
            "message_count": cache.get(addr, {}).get("message_count", 0),
            "manual_override": True,
        }
        save_cache(cache, CACHE_FILE)
        print(f"Pinned: {addr} → {label} (Tier {tier})")
        return

    # Normal classification workflow
    msgs = load_headers(HDR_FILE)
    senders = group_by_sender(msgs, args.min_msgs)
    print(
        f"Headers   : {HDR_FILE} ({len(msgs):,} messages)\n"
        f"Cache     : {CACHE_FILE} ({len(cache):,} entries)\n"
        f"Senders   : {len(senders):,} total"
    )

    if args.dry_run:
        print("\nDRY RUN — printing prompts only (no API calls, no writes)\n")
        # run classification loop in dry-run mode without Gmail API
        asyncio.run(
            classify_all_senders(
                senders,
                cache,
                providers,
                token_mgr=None,
                session=None,
                confidence_threshold=args.confidence,
                dry_run=True,
                reclassify=args.reclassify,
                uncertain_only=args.uncertain_only,
            )
        )
        return

    # Gmail API client for body fetching
    token_mgr: Optional[TokenManager] = None
    session: Optional[requests.Session] = None
    try:
        token_mgr = load_token()
        session = requests.Session()
    except SystemExit:
        # load_token already printed an error and exited; propagate
        raise
    except Exception:
        token_mgr = None
        session = None
        print("WARNING: Failed to initialise Gmail API client; body-based classification disabled.")

    asyncio.run(
        classify_all_senders(
            senders,
            cache,
            providers,
            token_mgr=token_mgr,
            session=session,
            confidence_threshold=args.confidence,
            dry_run=False,
            reclassify=args.reclassify,
            uncertain_only=args.uncertain_only,
        )
    )

    # After classification, rebuild rules and report
    updated_cache = load_cache(CACHE_FILE)
    rules = build_filter_rules(updated_cache, senders)
    write_filter_rules_json(rules, len(msgs), OUT_FILTER_JSON)
    write_report(updated_cache, senders, OUT_REPORT)
    print(f"\nFilter rules saved to {OUT_FILTER_JSON}")
    print(f"Report saved to {OUT_REPORT}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")

