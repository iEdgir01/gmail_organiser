"""
email_update.py — Generate an email-change checklist for all your accounts.

Reads output/accounts.json (produced by accounts.py) and cross-references
config/contacts.yaml to produce:

  output/email_update_list.txt  — grouped checklist: settings / portal / email
  output/email_update_list.csv  — spreadsheet to track which ones you've done
  output/notify_template.txt    — generic support email template

Run accounts.py first to generate accounts.json, then run this script.

Usage:
    python accounts.py
    python email_update.py --old you@old.com --new you@new.com
"""

import json, csv, sys, argparse
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required.  Run:  pip install pyyaml")
    sys.exit(1)

BASE          = Path(__file__).parent
CONTACTS_YAML = BASE / "config" / "contacts.yaml"
OUT_DIR       = BASE / "output"
ACCOUNTS_FILE = OUT_DIR / "accounts.json"
OUT_TXT       = OUT_DIR / "email_update_list.txt"
OUT_CSV       = OUT_DIR / "email_update_list.csv"
OUT_TPL       = OUT_DIR / "notify_template.txt"


# ── Contacts loading ──────────────────────────────────────────────────────────
def load_contacts(path: Path) -> dict:
    """
    Load contacts.yaml and return the contacts dict.
    Falls back to an empty dict if the file is missing.
    """
    if not path.exists():
        example = path.parent / "contacts.example.yaml"
        print(f"INFO: {path} not found — update methods will default to 'settings'.")
        if example.exists():
            print(f"  For accurate checklist, copy the example:")
            print(f"    cp {example} {path}")
        return {}

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return (data or {}).get("contacts", {}) or {}


CONTACTS: dict = load_contacts(CONTACTS_YAML)

METHOD_LABELS = {
    "settings": "Update via account settings (self-service)",
    "portal":   "Update by logging in to the portal",
    "email":    "Send email to support",
    "phone":    "Must call customer support",
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_accounts() -> list[dict]:
    data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    return data.get("accounts", [])


def build_update_list(accounts: list[dict], old_email: str, new_email: str) -> list[dict]:
    rows = []
    for acc in accounts:
        name    = acc["name"]
        url     = acc["url"]
        contact = CONTACTS.get(name, {})

        method    = contact.get("method", "settings")
        login_url = contact.get("login_url", url)
        email_to  = contact.get("contact", "")
        notes     = contact.get("notes", "")

        rows.append({
            "name":      name,
            "method":    method,
            "login_url": login_url,
            "email_to":  email_to,
            "notes":     notes,
            "done":      "",
        })
    return rows


def write_txt_report(rows: list[dict], old_email: str, new_email: str) -> str:
    by_method: dict = {}
    for r in rows:
        by_method.setdefault(r["method"], []).append(r)

    lines = []
    a = lines.append
    a("=" * 76)
    a("  EMAIL UPDATE CHECKLIST")
    a(f"  Old address: {old_email}")
    a(f"  New address: {new_email}")
    a("=" * 76)
    a(f"\n  Total services to update: {len(rows)}")
    for method, label in METHOD_LABELS.items():
        count = len(by_method.get(method, []))
        if count:
            a(f"  {label}: {count}")

    for method in ["settings", "portal", "email", "phone"]:
        items = by_method.get(method, [])
        if not items:
            continue
        a(f"\n{'─'*76}")
        a(f"  {METHOD_LABELS[method].upper()}  ({len(items)} services)")
        a(f"{'─'*76}")

        if method in ("settings", "portal"):
            a(f"  {'SERVICE':<30} {'URL / ACTION'}")
            a(f"  {'-'*30} {'-'*44}")
            for r in sorted(items, key=lambda x: x["name"].lower()):
                name   = r["name"][:29]
                target = r["login_url"][:60]
                notes  = f"  ↳ {r['notes']}" if r["notes"] else ""
                a(f"  [ ] {name:<30} {target}")
                if notes:
                    a(f"      {notes}")
        else:
            a(f"  {'SERVICE':<30} {'CONTACT':<40} {'NOTES'}")
            a(f"  {'-'*30} {'-'*40} {'-'*20}")
            for r in sorted(items, key=lambda x: x["name"].lower()):
                name   = r["name"][:29]
                target = (r["email_to"] or r.get("phone", "—"))[:39]
                notes  = r["notes"][:40] if r["notes"] else ""
                a(f"  [ ] {name:<30} {target:<40} {notes}")

    a(f"\n{'─'*76}")
    a("  See output/notify_template.txt for the support email template.")
    a("=" * 76)
    return "\n".join(lines)


def write_csv_report(rows: list[dict], old_email: str, new_email: str):
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Service", "Update Method", "URL / Login", "Support Email",
            "Notes", "Done (✓)"
        ])
        for r in sorted(rows, key=lambda x: x["name"].lower()):
            writer.writerow([
                r["name"],
                METHOD_LABELS.get(r["method"], r["method"]),
                r["login_url"],
                r["email_to"],
                r["notes"],
                "",
            ])


def write_template(old_email: str, new_email: str) -> str:
    template = f"""\
Subject: Request to Update Email Address on Account

Dear [Service Name] Support Team,

I am writing to request an update to the email address associated with my account.

Current email address : {old_email}
New email address     : {new_email}

Please update my account so that all future communications, notifications,
and account alerts are sent to the new address above.

Could you please confirm once the change has been made?

If you require any additional verification, please let me know and I will
provide the necessary information promptly.

Thank you for your assistance.

Kind regards,
[Your Name]

---
Note: Replace [Service Name] and [Your Name] before sending.
Services that need this email: see output/email_update_list.txt
"""
    OUT_TPL.write_text(template, encoding="utf-8")
    return template


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", required=True,
                        help="Your current (old) email address")
    parser.add_argument("--new", required=True,
                        help="Your new email address")
    args = parser.parse_args()

    if not ACCOUNTS_FILE.exists():
        print(f"ERROR: {ACCOUNTS_FILE} not found.")
        print("Run  python accounts.py  first.")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading accounts...")
    accounts = load_accounts()
    print(f"  {len(accounts)} accounts loaded.\n")

    print(f"  Old email: {args.old}")
    print(f"  New email: {args.new}\n")

    rows   = build_update_list(accounts, args.old, args.new)
    report = write_txt_report(rows, args.old, args.new)

    print(report)

    OUT_TXT.write_text(report, encoding="utf-8")
    print(f"\nChecklist saved to  {OUT_TXT}")

    write_csv_report(rows, args.old, args.new)
    print(f"Spreadsheet saved   {OUT_CSV}")

    write_template(args.old, args.new)
    print(f"Email template      {OUT_TPL}")
    print("\n── Template preview ──────────────────────────────────────────")
    print(OUT_TPL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
