# gws-cli — Gmail Mailbox Organiser

A command-line toolkit that analyses your Gmail mailbox, builds tiered filter rules, and retroactively labels everything — without touching a single email body.

Useful if you:
- Have a large, messy Gmail inbox and want to auto-sort it into labelled folders
- Are migrating to a new email address and need to audit every service you have an account with
- Want to bulk-label years of existing email in minutes using the Gmail API

---

## Script reference

| Script | What it does | Flags |
|---|---|---|
| `auth_setup.py` | OAuth2 setup — run once | — |
| `fetch.py` | Download all message headers | `--reset` wipe saved data and start fresh |
| `discover_senders.py` | Show senders not covered by any rule | `--min N` min message count (default 3) · `--limit N` max results (default 100) |
| `analyze.py` | Classify messages → `output/filter_rules.json` | — |
| `apply_filters.py` | Create Gmail labels + filter rules via gws CLI | `--dry-run` preview only · `--labels-only` skip filter creation · `--forward-to ADDRESS` forward all Tier 1 matches to `ADDRESS` · `--reset-filters` delete all existing filters first · logs to `output/apply_filters.log` |
| `export_filters_xml.py` | Export rules as Gmail-importable XML | `--forward-to ADDRESS` include a forward action on all Tier 1 filters · run `apply_filters.py --labels-only` first or imported filters will have no labels to apply |
| `backfill.py` | Retroactively label existing messages | `--dry-run` preview only · `--tier 1,2,3,4` restrict to specific tiers |
| `mark_read.py` | Bulk mark messages as read | `--dry-run` count only · `--inbox-only` inbox only · `--label LABEL` restrict to one label |
| `accounts.py` | Build account inventory from mailbox | `--min-count N` min emails per sender (default 1) · `--discovered-only` exclude already-ruled senders |
| `email_update.py` | Generate email-change checklist | `--old ADDRESS` *(required)* · `--new ADDRESS` *(required)* |

---

## How it works

```
auth_setup.py       → OAuth2 token
       ↓
fetch.py            → download all message headers (From, Subject) into data/headers.jsonl
       ↓
discover_senders.py → see which senders aren't yet covered by a rule
       ↓
analyze.py          → classify senders using config/rules.yaml → output/filter_rules.json
       ↓
apply_filters.py    → create Gmail labels + filter rules via gws CLI
       ↓
backfill.py         → retroactively label existing messages via batchModify API
       ↓
accounts.py         → build account inventory from mailbox signals
       ↓
email_update.py     → generate email-change checklist for each service
```

---

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up Google API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services → Credentials**
2. Create an **OAuth 2.0 Client ID** (Desktop app type)
3. Enable the **Gmail API** for your project
4. Download the client secret JSON
5. Save it as `config/client_secret.json`

### 3. Authenticate

```bash
python auth_setup.py
```

This opens a browser, asks you to approve Gmail access, and saves a token to `data/token.json`.

### 4. Fetch your mailbox headers

```bash
python fetch.py
```

Downloads the `From`, `Subject`, and `List-Unsubscribe` headers for every message. Safe to interrupt and resume — progress is saved every 500 messages.

### 5. Configure your rules

```bash
cp config/rules.example.yaml    config/rules.yaml
cp config/services.example.yaml config/services.yaml
cp config/contacts.example.yaml config/contacts.yaml
```

Edit `config/rules.yaml` — this is the core of the tool. Each rule maps a sender pattern to a label and a tier.

> **Using Claude Code instead?** Now that your mailbox data is fetched, you can have Claude draft rules from your actual senders rather than editing by hand — see [Using Claude Code to populate rules.yaml](#using-claude-code-to-populate-rulesyaml) below.

### 6. Find gaps in your rules

```bash
python discover_senders.py
```

Shows high-volume senders not yet covered by any rule. Add them to `config/rules.yaml`.

### 7. Analyse

```bash
python analyze.py
```

Classifies every message and writes `output/filter_rules.json` + a human-readable report.

### 8. Apply filters

```bash
python apply_filters.py --dry-run                         # preview first
python apply_filters.py                                   # create labels + Gmail filter rules
python apply_filters.py --forward-to you@newdomain.com    # forward all Tier 1 matches
python apply_filters.py --reset-filters                   # delete all existing filters, then recreate
```

Output is also written to `output/apply_filters.log`.

Alternatively, export as XML for manual import via the Gmail web UI:

```bash
python apply_filters.py --labels-only                        # create labels first (required before XML import)
python export_filters_xml.py                                 # all tiers
python export_filters_xml.py --forward-to you@newdomain.com  # include forward action on Tier 1 filters
```

Then: Gmail Settings → Filters and Blocked Addresses → **Import filters** → select `output/gmail_filters.xml`.

> **Note:** Labels must exist in Gmail before you import the XML — the XML format carries filter logic only, not label definitions. Run `--labels-only` first or the imported filters will have nowhere to apply labels. Gmail enforces a hard limit of 1,000 filters — the export script will warn you if you exceed it.

Filters apply to **new incoming** messages. To label existing mail, run backfill.

### 9. Backfill existing messages

```bash
python backfill.py --dry-run   # preview
python backfill.py             # apply — labels 50k+ messages in ~2 min
```

### 10. (Optional) Bulk mark unread as read

```bash
python mark_read.py --dry-run          # count unread, no changes
python mark_read.py                    # mark everything as read
python mark_read.py --inbox-only       # only inbox
python mark_read.py --label "Finance"  # only a specific label
```

### 11. (Optional) Audit your accounts

```bash
python accounts.py
python email_update.py --old you@old.com --new you@new.com
```

---

## Using Claude Code to populate rules.yaml

Instead of writing `config/rules.yaml` by hand, you can point Claude Code at your mailbox data and have it draft rules for you.

### 1. Install Claude Code

```bash
npm install -g @anthropic-ai/claude-code
```

Requires Node.js 18+. Log in with your Anthropic account:

```bash
claude
```

Follow the one-time OAuth prompt in your browser.

### 2. Run fetch.py first

Claude needs real data to work from. Complete steps 1–5 of the Quick start above so that `data/headers.jsonl` exists and `discover_senders.py` has output to read.

```bash
python fetch.py
python discover_senders.py > data/uncovered_senders.txt
```

### 3. Open Claude Code in the project directory

```bash
cd path/to/gws-cli
claude
```

Claude Code automatically reads the files in your working directory, so it will have access to `data/headers.jsonl`, `data/uncovered_senders.txt`, and the existing `config/rules.example.yaml` as context.

### 4. Give Claude the rules schema

Paste this into the Claude Code terminal at the start of your session to orient it:

```
I'm using gws-cli to organise my Gmail mailbox.
The file config/rules.yaml classifies senders into four tiers:
  1 — IMPORTANT   : billing, banking, security alerts — keep in inbox
  2 — USEFUL      : social, gaming, jobs — skip inbox
  3 — LOW NOISE   : newsletters, promos — skip inbox, leave unread
  4 — UNSUBSCRIBE : pure noise to review and remove — skip inbox, mark read

Each rule looks like:
  - pattern: some-domain.com   # substring match on From address
    label: Category/Subcategory
    tier: 1
    name: Friendly Name

Read config/rules.example.yaml so you understand the format, then read
data/uncovered_senders.txt. For every sender listed, propose a new rule
with an appropriate tier and label hierarchy. Group related senders under
the same label where it makes sense. Write the result to config/rules.yaml,
appending after any existing rules.
```

### 5. Review and iterate

Claude will draft rules for every uncovered sender and write them into `config/rules.yaml`. Review the output, then iterate conversationally:

```
Move all the Shopify receipts to Finance/Shopify at tier 1.
Any sender with "newsletter" in the address should be tier 3.
I don't recognise "noreply@obscure-site.com" — set it to tier 4.
```

Changes take effect immediately — no reload needed. When you're happy, run `analyze.py` and `apply_filters.py` as normal.

### Tips

- Run `discover_senders.py` again after each `fetch.py` run and feed the new output to Claude to keep rules up to date.
- You can share the full `data/headers.jsonl` with Claude if you want it to infer tiers from subject-line patterns rather than just domain names, but sender domains alone are usually sufficient.
- If Claude proposes a label hierarchy you don't like, tell it your preferred structure (e.g. "use `Shopping/` not `Retail/`") and ask it to rewrite the relevant block.

---

## Tier system

| Tier | Name | Behaviour | Use for |
|------|------|-----------|---------|
| 1 | IMPORTANT | Apply label, **keep in inbox** | Banking, billing, security alerts |
| 2 | USEFUL | Apply label, skip inbox | Gaming, social, jobs, entertainment |
| 3 | LOW NOISE | Apply label, skip inbox, **leave unread** | Light newsletters, occasional promos |
| 4 | UNSUBSCRIBE | Apply label, skip inbox, mark read → Unsubscribe Queue | Pure marketing noise to review and unsubscribe from |

---

## Configuration files

All three config files are YAML and live in `config/`. They are **gitignored** — you populate them from the `*.example.yaml` templates.

### `config/rules.yaml`

The classification rules. Each entry:

```yaml
rules:
  - pattern: accounts.google.com    # matched as substring of From address
    label: Accounts/Google          # Gmail label (use / for nested)
    tier: 1                         # 1–4 (see tier table above)
    name: Google Account            # friendly name in reports
```

After running `fetch.py`, use `discover_senders.py` to find senders to add.

### `config/services.yaml`

Maps email domains to service names and login URLs, used by `accounts.py`:

```yaml
services:
  github.com:
    name: GitHub
    url: https://github.com/login
```

### `config/contacts.yaml`

Tells `email_update.py` how to update each service when changing email address:

```yaml
contacts:
  GitHub:
    method: settings                      # settings | portal | email | phone
    login_url: https://github.com/settings/emails
    notes: Add new address, verify, set as primary
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GMAIL_CLIENT_SECRET` | `config/client_secret.json` | Path to your OAuth client secret |

---

## gws CLI

`fetch.py` can fall back to the [gws CLI](https://github.com/nicholasgasior/gws) if no `token.json` is present. `apply_filters.py` now uses the direct Gmail API and no longer requires gws.

If `gws` is on your PATH it will be found automatically. Otherwise set the `GWS_CMD` environment variable:

```bash
export GWS_CMD=/path/to/gws
```

---

## Rate limits & performance

| Operation | Throughput | Time for 50k messages |
|---|---|---|
| `fetch.py` (direct API) | ~100 msg/sec | ~8 min |
| `fetch.py` (gws fallback) | ~10 msg/sec | ~90 min |
| `backfill.py` | 1000 msg/batch | ~2 min |

`fetch.py` uses 50 parallel worker threads. Both `fetch.py` and `backfill.py` implement exponential backoff on 429/5xx responses.

---

## File structure

```
gws-cli/
├── auth_setup.py          # OAuth2 setup — run once
├── fetch.py               # Download message headers
├── discover_senders.py    # Find unclassified senders
├── analyze.py             # Classify and produce filter_rules.json
├── apply_filters.py       # Create Gmail labels + filter rules
├── export_filters_xml.py  # Export rules as Gmail-importable XML
├── backfill.py            # Retroactively label existing messages
├── mark_read.py           # Bulk mark unread messages as read
├── accounts.py            # Build account inventory
├── email_update.py        # Generate email-change checklist
├── requirements.txt
├── config/
│   ├── rules.example.yaml      # Starter classification rules → copy to rules.yaml
│   ├── services.example.yaml   # Starter service map        → copy to services.yaml
│   └── contacts.example.yaml   # Starter contact directory  → copy to contacts.yaml
├── data/                  # Created at runtime — gitignored
│   ├── token.json         # OAuth2 token
│   ├── message_ids.txt    # All Gmail message IDs
│   └── headers.jsonl      # Fetched message headers
└── output/                # Created at runtime — gitignored
    ├── filter_rules.json
    ├── analysis_report.txt
    ├── accounts.json / .csv / .txt
    └── email_update_list.txt / .csv
```

---

## Privacy & security

- Only message **headers** are downloaded (From, Subject, List-Unsubscribe). Email bodies are never read.
- `data/token.json` contains your OAuth2 token — keep it private. It is gitignored by default.
- `config/client_secret.json` is also gitignored. Never commit it.
- The OAuth scope requested is `gmail.modify` (read + label/filter management, no delete or send).

---

## License

MIT
