"""
accounts.py — Build a password-management inventory from your mailbox.

Identifies services where you have a registered account by two methods:
  1. KNOWN services — every sender already in config/rules.yaml
  2. DISCOVERED services — senders whose email subjects contain account-activity
     signals (welcome, verify, password reset, receipt, order, invoice…)

A curated service map (config/services.yaml) maps email domains to friendly
names and login URLs. Copy config/services.example.yaml to get started.

Output:
  output/accounts.json  — full record per service (name, url, email, count)
  output/accounts.csv   — importable into most password managers
  output/accounts.txt   — human-readable checklist

Usage:
    python accounts.py                   # full scan
    python accounts.py --min-count 2     # skip one-off senders (fewer false-positives)
    python accounts.py --discovered-only # show only newly discovered (not in rules)
"""

import json, re, csv, sys, argparse
from pathlib import Path
from collections import defaultdict

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required.  Run:  pip install pyyaml")
    sys.exit(1)

from analyze import RULES

BASE          = Path(__file__).parent
HDR_FILE      = BASE / "data"   / "headers.jsonl"
SERVICES_YAML = BASE / "config" / "services.yaml"
OUT_DIR       = BASE / "output"
OUT_JSON      = OUT_DIR / "accounts.json"
OUT_CSV       = OUT_DIR / "accounts.csv"
OUT_TXT       = OUT_DIR / "accounts.txt"


# ── Subject keywords that signal a real account relationship ─────────────────
ACCOUNT_SIGNALS = [
    r"\bwelcome\b",
    r"\bverif(y|ied|ication)\b",
    r"\bconfirm\b",
    r"\bactivat(e|ion)\b",
    r"\bpassword\b",
    r"\bsign[- ]?(in|up|ed)\b",
    r"\blog[- ]?in\b",
    r"\bregist(er|ered|ration)\b",
    r"\breset\b",
    r"\btwo[- ]factor\b",
    r"\b2fa\b",
    r"\bone[- ]time\b",
    r"\botp\b",
    r"\bsecurit(y|ies)\b",
    r"\breceipt\b",
    r"\border\b",
    r"\binvoice\b",
    r"\bsubscription\b",
    r"\bbilling\b",
    r"\bpayment\b",
    r"\brenewal\b",
    r"\baccount\b",
    r"\bonboard(ing)?\b",
    r"\bget started\b",
    r"you'?re (in|all set)\b",
    r"\bpurchase\b",
    r"\bdownload\b",
    r"\blicen[sc]e\b",
]

_SIGNAL_RE = re.compile("|".join(ACCOUNT_SIGNALS), re.IGNORECASE)


# ── Mail subdomains to strip when guessing the root website ──────────────────
_STRIP_PREFIXES = re.compile(
    r"^(mail|email|e|em|mailer|newsletter|newsletters|noreply|no-reply|"
    r"notify|notifications|notification|alert|alerts|updates|news|info|"
    r"accounts|account|billing|secure|security|system|messages|msg|"
    r"support|help|reply|promo|promotions|marketing|send|sender|"
    r"mg\d?|sg|mailgun|sendgrid|post|postmaster|bounce|bounces|"
    r"donotreply|do-not-reply|team|hello|hi|hey)\.",
    re.IGNORECASE
)


# ── Service map loading ───────────────────────────────────────────────────────
def load_service_map(path: Path) -> dict[str, tuple[str, str]]:
    """
    Load services.yaml and return {domain: (name, url)} dict.
    Falls back to an empty dict if the file is missing.
    """
    if not path.exists():
        example = path.parent / "services.example.yaml"
        print(f"INFO: {path} not found — auto-deriving service names from domains.")
        if example.exists():
            print(f"  For better results, copy the example:")
            print(f"    cp {example} {path}")
        return {}

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    raw = (data or {}).get("services", {}) or {}
    return {
        domain: (str(v.get("name", domain)), str(v.get("url", f"https://www.{domain}")))
        for domain, v in raw.items()
        if isinstance(v, dict)
    }


SERVICE_MAP: dict[str, tuple[str, str]] = load_service_map(SERVICES_YAML)


# ── Helpers ───────────────────────────────────────────────────────────────────
def extract_addr(frm: str) -> str:
    m = re.search(r"<([^>]+)>", frm)
    addr = m.group(1) if m else frm
    return addr.lower().strip()


def addr_to_domain(addr: str) -> str:
    return addr.split("@")[-1] if "@" in addr else addr


def domain_to_website(domain: str) -> str:
    root = _STRIP_PREFIXES.sub("", domain)
    return f"https://www.{root}"


def lookup_service(domain: str) -> tuple[str, str] | None:
    """
    Try increasingly broad lookups in SERVICE_MAP:
      1. Exact domain match
      2. Suffix match (e.g. 'foo.bar.com' → 'bar.com')
    Returns (name, url) or None.
    """
    if domain in SERVICE_MAP:
        return SERVICE_MAP[domain]
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in SERVICE_MAP:
            return SERVICE_MAP[parent]
    return None


def has_account_signal(subject: str) -> bool:
    return bool(_SIGNAL_RE.search(subject))


# ── Main logic ────────────────────────────────────────────────────────────────
def load_messages() -> list[dict]:
    records = []
    with open(HDR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def build_known_domains() -> set[str]:
    """Collect all domains that appear in RULES (always treated as known accounts)."""
    known = set()
    for pattern, label, tier, name in RULES:
        if "@" in pattern:
            known.add(pattern.split("@")[-1])
        else:
            known.add(pattern.lstrip("@"))
    return known


def scan(messages: list[dict], min_count: int) -> list[dict]:
    """
    Returns a list of account records sorted by service name.
    Each record: {name, url, domain, email_addresses, total_count, source, signal_count}
    """
    domain_stats: dict = defaultdict(lambda: {
        "emails": defaultdict(int),
        "total": 0,
        "signal": 0,
    })

    known_domains = build_known_domains()

    for msg in messages:
        addr    = extract_addr(msg.get("from", ""))
        subject = msg.get("subject", "")
        if not addr:
            continue
        domain = addr_to_domain(addr)
        domain_stats[domain]["emails"][addr] += 1
        domain_stats[domain]["total"] += 1
        if has_account_signal(subject):
            domain_stats[domain]["signal"] += 1

    accounts = []
    seen_services: dict[str, dict] = {}

    for domain, stats in domain_stats.items():
        if stats["total"] < min_count:
            continue

        is_known = any(
            kd in domain or domain in kd
            for kd in known_domains
        )
        has_signal = stats["signal"] > 0

        if not is_known and not has_signal:
            continue

        svc = lookup_service(domain)
        if svc:
            name, url = svc
        else:
            root = _STRIP_PREFIXES.sub("", domain)
            name = root.split(".")[0].capitalize()
            url  = f"https://www.{root}"

        source = "known" if is_known else "discovered"

        if name in seen_services:
            rec = seen_services[name]
            for email, cnt in stats["emails"].items():
                if email not in rec["email_addresses"]:
                    rec["email_addresses"].append(email)
            rec["total_count"]  += stats["total"]
            rec["signal_count"] += stats["signal"]
            if source == "known":
                rec["source"] = "known"
        else:
            rec = {
                "name":            name,
                "url":             url,
                "domain":          domain,
                "email_addresses": list(stats["emails"].keys()),
                "total_count":     stats["total"],
                "signal_count":    stats["signal"],
                "source":          source,
            }
            seen_services[name] = rec
            accounts.append(rec)

    accounts.sort(key=lambda r: r["name"].lower())
    return accounts


def write_csv(accounts: list[dict]):
    """
    Generic password manager CSV export.
    Columns: label, username, password, url, notes, folder, favourite
    Compatible with Bitwarden, Nextcloud Passwords, and most CSV-import tools.
    """
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "label", "username", "password", "url", "notes", "folder", "favourite"
        ])
        for acc in accounts:
            notes  = f"Email: {', '.join(acc['email_addresses'][:2])}"
            writer.writerow([
                acc["name"],        # label
                "",                 # username — fill in manually
                "",                 # password — fill in manually
                acc["url"],         # url
                notes,              # notes — which email address is on file
                "Password Audit",   # folder
                "0",                # favourite
            ])


def write_txt(accounts: list[dict]) -> str:
    known      = [a for a in accounts if a["source"] == "known"]
    discovered = [a for a in accounts if a["source"] == "discovered"]

    lines = []
    a = lines.append
    a("=" * 72)
    a("  ACCOUNT INVENTORY — PASSWORD MANAGEMENT")
    a("=" * 72)
    a(f"\n  Total accounts found        : {len(accounts)}")
    a(f"  Known services (from rules) : {len(known)}")
    a(f"  Discovered (by email signal): {len(discovered)}")
    a(f"\n  {'SERVICE':<35} {'WEBSITE':<45} {'EMAILS'}")
    a(f"  {'-'*35} {'-'*45} {'-'*6}")

    def section(title: str, items: list[dict]):
        a(f"\n{'─'*72}")
        a(f"  {title}  ({len(items)} services)")
        a(f"{'─'*72}")
        for acc in items:
            emails = ", ".join(acc["email_addresses"][:2])
            if len(acc["email_addresses"]) > 2:
                emails += f" (+{len(acc['email_addresses'])-2})"
            name = acc["name"][:34]
            url  = acc["url"][:44]
            a(f"  {name:<35} {url:<45} {emails}")

    section("KNOWN SERVICES (from filter rules)", known)
    section("DISCOVERED SERVICES (account signals in subjects)", discovered)

    a(f"\n{'─'*72}")
    a("  NEXT STEPS")
    a("  1. Import output/accounts.csv into your password manager")
    a("  2. Review each entry and set a unique strong password")
    a("  3. Enable 2FA wherever offered")
    a("=" * 72)

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-count",       type=int, default=1,
                        help="Minimum email count to include a sender (default: 1)")
    parser.add_argument("--discovered-only", action="store_true",
                        help="Only show newly discovered accounts (not in rules)")
    args = parser.parse_args()

    if not HDR_FILE.exists():
        print(f"ERROR: {HDR_FILE} not found. Run  python fetch.py  first.")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading messages...")
    messages = load_messages()
    print(f"  {len(messages):,} messages loaded.")

    print("Scanning for account signals...")
    accounts = scan(messages, args.min_count)

    if args.discovered_only:
        accounts = [a for a in accounts if a["source"] == "discovered"]

    print(f"  {len(accounts)} accounts identified.\n")

    report = write_txt(accounts)
    print(report)

    OUT_TXT.write_text(report, encoding="utf-8")
    print(f"\nReport saved to  {OUT_TXT}")

    write_csv(accounts)
    print(f"CSV saved to     {OUT_CSV}  (import into your password manager)")

    OUT_JSON.write_text(
        json.dumps({"total": len(accounts), "accounts": accounts},
                   indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"JSON saved to    {OUT_JSON}")


if __name__ == "__main__":
    main()
