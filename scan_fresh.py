"""
scan_fresh.py — Fresh classification scan, ignoring existing RULES.

Reads data/headers.jsonl, groups every message by sender address, and scores
each sender's subject lines against keyword categories.  High-confidence
senders are placed into a suggested category.  Low-confidence or mixed-signal
senders go into an uncertain bucket for body review.

Output:
    output/fresh_scan.txt           — full report, all senders
    output/fresh_scan_uncertain.txt — uncertain-only report
    data/uncertain_senders.json     — uncertain sender IDs for fetch_bodies.py

Usage:
    python scan_fresh.py
    python scan_fresh.py --min-msgs 3     # skip senders with fewer than 3 messages
    python scan_fresh.py --uncertain-only # print only the uncertain section
"""

import json, re, sys, argparse
from pathlib import Path
from collections import Counter, defaultdict

BASE           = Path(__file__).parent
HDR_FILE       = BASE / "data" / "headers.jsonl"
OUT_DIR        = BASE / "output"
UNCERTAIN_FILE = BASE / "data" / "uncertain_senders.json"

# ── Category keyword lists ─────────────────────────────────────────────────────
# Longer / more specific phrases checked before single-word substrings.
CATEGORIES: dict[str, list[str]] = {
    "billing": [
        "invoice", "receipt", "statement", "payment confirm", "order confirm",
        "order placed", "order shipped", "order deliver", "order complete",
        "order cancel", "your order", "booking confirm", "subscription renew",
        "renewal", "charge", "your bill", "balance due", "refund", "purchase",
        "transaction", "pro forma", "quote", "dispatched", "packed",
        "recharge", "prepaid", "debit", "debit order",
    ],
    "security": [
        "verify your", "security alert", "sign-in attempt", "new sign-in",
        "new device", "unusual activity", "suspicious", "account access",
        "confirm your email", "confirm your account", "one-time code",
        "two-factor", "two factor", "2fa", " otp ", "reset your password",
        "password reset", "password changed", "account locked",
        "action required", "login attempt",
    ],
    "promo": [
        "% off", " deal", "exclusive offer", "limited time", "flash sale",
        "mega sale", "special offer", "save ", "huge discount", "coupon",
        "shop now", "buy now", "last chance", "don't miss",
        "free delivery", "free shipping", "today only", "ends tonight",
        "ends sunday", "ends monday", "click here", "hurry", "expires",
        "best price", "lowest price", "unbeatable",
    ],
    "job": [
        "job alert", "new job", "job opportunity", "job opening",
        "positions available", "vacancy", "vacancies", "job match",
        "interview invitation", "application received", "application update",
        "career opportunity", "hiring", "recruiter", "apply now",
        "shortlisted", "job offer",
    ],
    "social": [
        "sent you a message", "friend request", "wants to connect",
        "connected with you", "followed you", "mentioned you", "tagged you",
        "liked your", "commented on", "reaction to", "new notification",
        "people you may know", "invitation to connect",
    ],
    "newsletter": [
        "newsletter", "weekly digest", "daily digest", "monthly digest",
        "weekly roundup", "monthly roundup", "this week in",
        "this month in", "issue #", "issue no", "vol.", "edition",
        "weekly update", "monthly update",
    ],
    "account": [
        "your account", "account update", "account summary",
        "welcome to", "getting started", "complete your profile",
        "account created", "account activated", "subscription started",
        "your membership", "membership update",
    ],
}

# Weights for confidence scoring
CATEGORY_WEIGHT = {k: 1.0 for k in CATEGORIES}
CATEGORY_WEIGHT["billing"]    = 2.0   # strong signal
CATEGORY_WEIGHT["security"]   = 2.0   # strong signal
CATEGORY_WEIGHT["promo"]      = 1.5
CATEGORY_WEIGHT["job"]        = 1.5


def extract_addr(frm: str) -> str:
    m = re.search(r"<([^>]+)>", frm)
    addr = m.group(1) if m else frm
    return addr.lower().strip()


def score_subject(subject: str) -> Counter:
    """Return category → keyword-hit count for a subject line."""
    s = subject.lower()
    hits: Counter = Counter()
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in s:
                hits[cat] += 1
                break  # one hit per category per subject
    return hits


def classify_sender(subjects: list[str]) -> tuple[str, float, Counter]:
    """
    Returns (suggested_category, confidence_0_to_1, total_scores).
    confidence = fraction of subjects that hit the winning category.
    """
    total = len(subjects)
    if total == 0:
        return "uncertain", 0.0, Counter()

    totals: Counter = Counter()
    for s in subjects:
        hits = score_subject(s)
        for cat, count in hits.items():
            totals[cat] += CATEGORY_WEIGHT.get(cat, 1.0) * count

    if not totals:
        return "uncertain", 0.0, Counter()

    top_cat, top_score = totals.most_common(1)[0]
    total_score        = sum(totals.values())
    confidence         = top_score / total_score if total_score else 0.0

    # Downgrade to uncertain if score is low or split
    if confidence < 0.55 or top_score < 2:
        return "uncertain", confidence, totals

    return top_cat, confidence, totals


def domain_of(addr: str) -> str:
    return addr.split("@", 1)[1] if "@" in addr else addr


def format_section(title: str, senders: list[dict], top_n: int = 15) -> list[str]:
    lines = [
        "",
        "═" * 80,
        f"  {title}",
        "═" * 80,
    ]
    for s in senders:
        addr    = s["addr"]
        total   = s["total"]
        cat     = s["category"]
        conf    = s["confidence"]
        scores  = s["scores"]
        subj_c  = s["subjects"]

        score_str = "  ".join(f"{k}:{v:.0f}" for k, v in scores.most_common(4)) if scores else "-"
        lines.append(f"\n  ── {addr}  ({total:,} msgs)  conf:{conf:.0%}  [{score_str}]")
        for subj, cnt in subj_c.most_common(top_n):
            lines.append(f"    {cnt:>5}x  {subj}")
    return lines


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-msgs",       type=int, default=1,
                        help="Ignore senders with fewer than N messages (default: 1)")
    parser.add_argument("--uncertain-only", action="store_true",
                        help="Only print the uncertain section")
    parser.add_argument("--top",            type=int, default=15,
                        help="Max subjects to show per sender (default: 15)")
    args = parser.parse_args()

    if not HDR_FILE.exists():
        print(f"ERROR: {HDR_FILE} not found.")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load
    print(f"Loading {HDR_FILE.name}...")
    msgs = []
    with open(HDR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    pass
    print(f"  {len(msgs):,} messages loaded.")

    # Group by sender
    sender_subjects: dict[str, Counter] = defaultdict(Counter)
    sender_ids:      dict[str, list]    = defaultdict(list)

    for msg in msgs:
        addr    = extract_addr(msg.get("from", ""))
        subject = (msg.get("subject", "") or "").strip() or "(no subject)"
        mid     = msg.get("id", "")
        if not addr:
            continue
        sender_subjects[addr][subject] += 1
        if len(sender_ids[addr]) < 5:  # keep up to 5 sample IDs per sender
            sender_ids[addr].append(mid)

    print(f"  {len(sender_subjects):,} unique senders.")

    # Classify each sender
    categorised: dict[str, list] = defaultdict(list)  # category → [sender_dict]
    uncertain:   list             = []

    for addr, subj_counter in sender_subjects.items():
        total = sum(subj_counter.values())
        if total < args.min_msgs:
            continue

        subjects = []
        for subj, cnt in subj_counter.items():
            subjects.extend([subj] * cnt)

        cat, conf, scores = classify_sender(subjects)

        rec = {
            "addr":       addr,
            "domain":     domain_of(addr),
            "total":      total,
            "category":   cat,
            "confidence": conf,
            "scores":     scores,
            "subjects":   subj_counter,
            "sample_ids": sender_ids[addr],
        }

        if cat == "uncertain":
            uncertain.append(rec)
        else:
            categorised[cat].append(rec)

    # Sort each bucket by message count
    for cat in categorised:
        categorised[cat].sort(key=lambda r: -r["total"])
    uncertain.sort(key=lambda r: -r["total"])

    total_classified   = sum(sum(r["total"] for r in v) for v in categorised.values())
    total_uncertain    = sum(r["total"] for r in uncertain)
    total_senders      = sum(len(v) for v in categorised.values()) + len(uncertain)

    CATEGORY_TITLES = {
        "billing":    "BILLING / TRANSACTIONAL — invoices, receipts, order confirmations",
        "security":   "SECURITY / ACCOUNT ALERTS — verify, login, password, 2FA",
        "promo":      "PROMOTIONAL / MARKETING — deals, offers, sales",
        "job":        "JOB / CAREER — alerts, applications, recruiters",
        "social":     "SOCIAL — notifications, connections, messages",
        "newsletter": "NEWSLETTER / CONTENT — digests, weekly updates, publications",
        "account":    "ACCOUNT / ONBOARDING — welcome, membership, subscription",
    }

    # Build report
    lines = [
        "FRESH CLASSIFICATION SCAN",
        f"Messages: {len(msgs):,}   Senders: {total_senders:,}   "
        f"Classified: {total_classified:,}   Uncertain: {total_uncertain:,}",
        f"Run  python fetch_bodies.py  to get body context for uncertain senders.",
    ]

    if not args.uncertain_only:
        for cat in ["billing", "security", "account", "job", "social", "newsletter", "promo"]:
            senders = categorised.get(cat, [])
            if not senders:
                continue
            title = CATEGORY_TITLES.get(cat, cat.upper())
            title = f"{title}   [{len(senders)} senders, {sum(r['total'] for r in senders):,} msgs]"
            lines += format_section(title, senders, args.top)

    # Uncertain section
    unc_title = (f"UNCERTAIN — needs body review   "
                 f"[{len(uncertain)} senders, {total_uncertain:,} msgs]")
    lines += format_section(unc_title, uncertain, args.top)

    report = "\n".join(lines)

    # Write reports
    out_full = OUT_DIR / "fresh_scan.txt"
    out_full.write_text(report, encoding="utf-8")
    print(f"\nFull report → {out_full}")

    # Uncertain-only report
    unc_lines = [lines[0], lines[1], lines[2]]
    unc_lines += format_section(unc_title, uncertain, args.top)
    unc_report = "\n".join(unc_lines)
    out_unc = OUT_DIR / "fresh_scan_uncertain.txt"
    out_unc.write_text(unc_report, encoding="utf-8")
    print(f"Uncertain report → {out_unc}")

    # Write uncertain_senders.json for fetch_bodies.py
    uncertain_out = []
    for r in uncertain:
        uncertain_out.append({
            "addr":       r["addr"],
            "domain":     r["domain"],
            "total":      r["total"],
            "confidence": round(r["confidence"], 3),
            "scores":     dict(r["scores"].most_common()),
            "sample_ids": r["sample_ids"],
        })
    UNCERTAIN_FILE.write_text(
        json.dumps(uncertain_out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Uncertain senders → {UNCERTAIN_FILE}  ({len(uncertain_out)} senders)")

    print(f"\nSummary:")
    for cat in ["billing", "security", "account", "job", "social", "newsletter", "promo"]:
        senders = categorised.get(cat, [])
        if senders:
            print(f"  {cat:<12} {len(senders):>4} senders  {sum(r['total'] for r in senders):>7,} msgs")
    print(f"  {'uncertain':<12} {len(uncertain):>4} senders  {total_uncertain:>7,} msgs")


if __name__ == "__main__":
    main()
