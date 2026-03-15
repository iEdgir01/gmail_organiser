"""
apply_filters.py — Create Gmail labels and filter rules via gws CLI.

Reads output/filter_rules.json produced by analyze.py.

Tier logic:
  Tier 1 — IMPORTANT:   apply label, KEEP in inbox, optionally forward
  Tier 2 — USEFUL:      apply label, REMOVE from inbox
  Tier 3 — LOW NOISE:   apply label, REMOVE from inbox  (NOT marked read)
  Tier 4 — UNSUBSCRIBE: apply label, REMOVE from inbox, mark as read

Selective forwarding (Tier 1 only):
  Use --forward-to <address> to also forward all Tier 1 matches.
  The destination address must already be verified in Gmail Settings →
  Forwarding and POP/IMAP → Add a forwarding address.

Environment variables:
  GWS_CMD — path to gws CLI binary (default: auto-detected from PATH)

Usage:
    python apply_filters.py --dry-run                          # preview
    python apply_filters.py                                    # create labels + filters
    python apply_filters.py --forward-to you@newdomain.com    # + forward Tier 1
    python apply_filters.py --reset-filters --forward-to you@newdomain.com  # delete all filters, recreate
    python apply_filters.py --labels-only                      # only create labels
"""

import json, argparse, time, os, subprocess, shutil
from pathlib import Path

BASE       = Path(__file__).parent
RULES_FILE = BASE / "output" / "filter_rules.json"
BASE_ENV   = {**os.environ, "PYTHONUTF8": "1"}
ME         = "me"

# gws CLI: env var → PATH lookup → bare command fallback
GWS_CMD = os.environ.get("GWS_CMD") or shutil.which("gws") or "gws"


# ── gws CLI wrapper ───────────────────────────────────────────────────────────
def gws(*args, params: dict = None, body: dict = None,
        retry: int = 3) -> dict:
    cmd = [GWS_CMD, *args]
    if params:
        cmd += ["--params", json.dumps(params)]
    if body:
        cmd += ["--json", json.dumps(body)]
    for attempt in range(retry):
        r = subprocess.run(cmd, capture_output=True,
                           encoding="utf-8", errors="replace", env=BASE_ENV)
        text = r.stdout
        start = text.find("{")
        if start != -1:
            try:
                return json.loads(text[start:])
            except Exception:
                pass
        start = text.find("[")
        if start != -1:
            try:
                return {"items": json.loads(text[start:])}
            except Exception:
                pass
        if attempt < retry - 1:
            time.sleep(1 + attempt)
    return {}


# ── Label management ──────────────────────────────────────────────────────────
def get_existing_labels() -> dict[str, str]:
    data = gws("gmail", "users", "labels", "list", params={"userId": ME})
    return {lbl["name"]: lbl["id"] for lbl in data.get("labels", [])}


def ensure_label(name: str, existing: dict[str, str],
                 dry_run: bool) -> str | None:
    """Create label (and any parent labels) if not present."""
    if name in existing:
        return existing[name]

    # Ensure parent exists first for nested labels (e.g. Finance/Bank)
    parts = name.split("/")
    if len(parts) > 1:
        parent = "/".join(parts[:-1])
        ensure_label(parent, existing, dry_run)

    if dry_run:
        print(f"  [DRY RUN] Would create label: {name}")
        return None

    result = gws("gmail", "users", "labels", "create",
                 params={"userId": ME},
                 body={
                     "name": name,
                     "labelListVisibility":   "labelShow",
                     "messageListVisibility": "show",
                 })
    lid = result.get("id")
    if lid:
        existing[name] = lid
        print(f"  Created label: {name}  ({lid})")
    else:
        print(f"  WARNING: failed to create label: {name}  response: {result}")
    return lid


# ── Forwarding rule audit ─────────────────────────────────────────────────────
def get_existing_filters() -> list[dict]:
    data = gws("gmail", "users", "settings", "filters", "list",
               params={"userId": ME})
    return data.get("filter", [])


def delete_all_filters(existing_filters: list[dict], dry_run: bool):
    if not existing_filters:
        print("  No existing filters to delete.")
        return
    print(f"  Deleting {len(existing_filters)} existing filter(s)...")
    deleted = 0
    for f in existing_filters:
        fid = f["id"]
        if dry_run:
            print(f"  [DRY RUN] Would delete filter {fid}")
        else:
            gws("gmail", "users", "settings", "filters", "delete",
                params={"userId": ME, "id": fid})
            deleted += 1
            time.sleep(0.05)
    if not dry_run:
        print(f"  Deleted {deleted}/{len(existing_filters)} filters.")


def audit_forwarding_rules(existing_filters: list[dict],
                           forward_to: str, dry_run: bool):
    """
    Inspect every existing Gmail filter that has a forward action.
    - Filters forwarding to forward_to: confirm they are kept (no action needed).
    - Filters forwarding elsewhere: report them so the user can decide.
    Broad catch-all forwarders (no 'from' criteria) are flagged for removal.
    """
    fwd_filters = [
        f for f in existing_filters
        if f.get("action", {}).get("forward")
    ]

    if not fwd_filters:
        print("  No existing forwarding filters found.")
        return

    keep, redirect, broad = [], [], []
    for f in fwd_filters:
        dest     = f["action"]["forward"]
        criteria = f.get("criteria", {})
        has_from = bool(criteria.get("from") or criteria.get("query"))

        if not has_from:
            broad.append(f)
        elif forward_to and dest == forward_to:
            keep.append(f)
        else:
            redirect.append(f)

    if keep:
        print(f"\n  {len(keep)} filter(s) already forwarding to {forward_to} — kept as-is.")

    if redirect:
        print(f"\n  WARNING: {len(redirect)} filter(s) forwarding to a DIFFERENT address:")
        for f in redirect:
            frm  = f.get("criteria", {}).get("from", "(any)")
            dest = f["action"]["forward"]
            print(f"     id={f['id']}  from:{frm}  → {dest}")
        print("     Review these manually in Gmail Settings → Filters.")

    if broad:
        print(f"\n  WARNING: {len(broad)} CATCH-ALL forwarder(s) detected (no 'from' criteria).")
        print(f"     These forward EVERYTHING and may interfere with selective forwarding.")
        for f in broad:
            dest = f["action"]["forward"]
            fid  = f["id"]
            print(f"     id={fid}  destination: {dest}")
            if not dry_run:
                gws("gmail", "users", "settings", "filters", "delete",
                    params={"userId": ME, "id": fid})
                print(f"     → Deleted filter {fid}")
            else:
                print(f"     → [DRY RUN] Would delete filter {fid}")


# ── Filter creation ───────────────────────────────────────────────────────────
def create_filter(patterns: list[str], label_id: str,
                  tier: int, dry_run: bool, forward_to: str = "") -> bool:
    """
    Tier 1 → add label, keep inbox  (+ forward if forward_to is set)
    Tier 2 → add label, remove from inbox
    Tier 3 → add label, remove from inbox  (NOT marked read)
    Tier 4 → add label, remove from inbox, mark read
    """
    from_value = " OR ".join(patterns)

    add_ids    = [label_id]
    remove_ids = []
    if tier >= 2:
        remove_ids.append("INBOX")
    if tier == 4:
        remove_ids.append("UNREAD")

    fwd_suffix = f" + forward→{forward_to}" if (tier == 1 and forward_to) else ""
    tier_desc = {
        1: "keep inbox",
        2: "skip inbox",
        3: "skip inbox (unread)",
        4: "skip inbox + mark read",
    }[tier] + fwd_suffix

    if dry_run:
        preview = from_value[:70] + ("…" if len(from_value) > 70 else "")
        print(f"  [DRY RUN] from:{preview}  →  {tier_desc}")
        return True

    action: dict = {
        "addLabelIds":    add_ids,
        "removeLabelIds": remove_ids,
    }
    if tier == 1 and forward_to:
        action["forward"] = forward_to

    body = {
        "criteria": {"from": from_value},
        "action":   action,
    }
    short = from_value[:60] + ("…" if len(from_value) > 60 else "")

    for attempt in range(3):
        result = gws("gmail", "users", "settings", "filters", "create",
                     params={"userId": ME}, body=body)
        fid = result.get("id")
        if fid:
            print(f"  Filter {fid}: {short}  →  {tier_desc}")
            time.sleep(0.2)   # gentle rate limit
            return True
        wait = 2 ** attempt
        print(f"  Retry {attempt+1}/3 in {wait}s — {short}")
        time.sleep(wait)

    print(f"  FAILED — could not create filter: {short}")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",       action="store_true",
                        help="Preview changes without applying them")
    parser.add_argument("--labels-only",   action="store_true",
                        help="Only create labels, skip filter creation")
    parser.add_argument("--forward-to",    default="",
                        help="Forward all Tier 1 matches to this address "
                             "(must be verified in Gmail Settings → Forwarding)")
    parser.add_argument("--reset-filters", action="store_true",
                        help="Delete ALL existing Gmail filters before recreating")
    args = parser.parse_args()

    if not RULES_FILE.exists():
        print(f"ERROR: {RULES_FILE} not found. Run  python analyze.py  first.")
        return

    rules_data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    rules      = rules_data["rules"]
    total_msgs = rules_data.get("total_messages", "?")
    forward_to = args.forward_to.strip()

    print(f"\nLoaded {len(rules)} filter rules  (from {total_msgs:,} messages analysed)")
    if forward_to:
        print(f"Tier 1 forwarding → {forward_to}")
        print(f"  Ensure {forward_to} is verified in Gmail Settings → Forwarding")
    if args.dry_run:
        print("DRY RUN — no changes will be made\n")

    print("\nFetching existing labels...")
    existing = get_existing_labels()
    print(f"  {len(existing)} labels already exist.")

    # ── Labels ────────────────────────────────────────────────────────────
    print("\n── Creating labels ──")
    label_ids: dict[str, str] = {}
    for name in sorted(set(r["label"] for r in rules)):
        lid = ensure_label(name, existing, args.dry_run)
        if lid:
            label_ids[name] = lid

    if args.labels_only:
        print("\nDone (--labels-only).")
        return

    # ── Forwarding audit + optional reset ────────────────────────────────
    print("\n── Auditing existing filters ──")
    existing_filters = get_existing_filters()
    print(f"  {len(existing_filters)} existing filter(s) found.")

    if args.reset_filters:
        delete_all_filters(existing_filters, args.dry_run)
        existing_filters = []
    else:
        audit_forwarding_rules(existing_filters, forward_to, args.dry_run)

    # ── Filters ───────────────────────────────────────────────────────────
    print("\n── Creating filters ──")
    TIER_NAMES = {
        1: "TIER 1 — IMPORTANT  (label + keep inbox)",
        2: "TIER 2 — USEFUL     (label + skip inbox)",
        3: "TIER 3 — LOW NOISE  (label + skip inbox, unread stays)",
        4: "TIER 4 — UNSUBSCRIBE (label + skip inbox + mark read)",
    }
    current_tier = None
    failed = []

    for rule in sorted(rules, key=lambda r: r["tier"]):
        tier  = rule["tier"]
        label = rule["label"]
        pats  = rule["patterns"]

        if tier != current_tier:
            print(f"\n  {TIER_NAMES[tier]}")
            current_tier = tier

        lid = label_ids.get(label)
        if not lid and not args.dry_run:
            print(f"  SKIP — no label id for: {label}")
            continue

        ok = create_filter(pats, lid or "DRY", tier, args.dry_run, forward_to)
        if not ok:
            failed.append((label, tier, pats))

    if failed:
        print(f"\n  ⚠  {len(failed)} filter(s) failed — re-run with --reset-filters to retry all.")
        for lbl, t, p in failed:
            print(f"     tier={t}  label={lbl}  patterns={p[:2]}{'…' if len(p)>2 else ''}")

    verdict = "DRY RUN complete." if args.dry_run else "Done!"
    print(f"\n{verdict}")
    if not args.dry_run:
        print("\nFilters apply to NEW incoming messages.")
        print("To label your existing emails, run:")
        print("  python backfill.py --dry-run   # preview")
        print("  python backfill.py             # apply")


if __name__ == "__main__":
    main()
