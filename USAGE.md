# Using gmail-organizer

Step-by-step guide to run the full pipeline: fetch → classify → create labels → backfill → export filters.

---

## Prerequisites

1. **Python 3.10+** with dependencies:
   ```bash
   cd gmail_organizer
   pip install -r requirements.txt
   ```

2. **Gmail API credentials**
   A Google Cloud project with Gmail API enabled, and an OAuth 2.0 "Desktop app" client secret JSON.
   - Put the file in `config/client_secret.json`, or
   - Set `GMAIL_CLIENT_SECRET` to its path.

3. **AI provider keys** (for classification)
   At least one of:
   - `OPENAI_API_KEY`
   - `ANTHROPIC_API_KEY`
   You can also paste them when prompted; they're stored in `data/api_keys.json`.

---

## 1. One-time auth

Get a token so scripts can call the Gmail API:

```bash
python auth_setup.py
```

- Browser opens for Google sign-in and consent.
- Token is saved to `data/token.json` (refresh is automatic later).

If you use a custom client secret path:

```bash
python auth_setup.py --client-secret /path/to/client_secret.json
```

---

## 2. (Optional) Full reset — start from zero

Only if you want to **remove all existing labels and filters** and re-run the pipeline from scratch.

### 2a. Reset Gmail labels and messages

- **Preview:**
  `python reset_labels.py --dry-run`
- **Run:**
  `python reset_labels.py`
  - Prompts for typing `yes`.
  - Puts every message back in INBOX and removes all user labels, then deletes all user-created labels.

### 2b. Remove Gmail filters by hand

- Gmail → **Settings** → **See all settings** → **Filters and Blocked Addresses**
- Delete any filters you previously imported or created for this system.

### 2c. Wipe local fetch data (optional)

To re-fetch all headers from Gmail:

```bash
python fetch.py --reset
```

This deletes `data/message_ids.txt` and `data/headers.jsonl`.
To also clear **classifications** and start classify from zero, delete:

- `data/auto_classifications.json`
- `output/filter_rules.json`
- `output/auto_classify_report.txt`

(And optionally `output/gmail_filters.xml`.)

---

## 3. Fetch message headers

Download IDs and headers (From, Subject, etc.) for all messages:

```bash
python fetch.py
```

- First run: fetches all message IDs, then all headers (can take a while for large mailboxes).
- Interrupt and re-run is safe: progress is appended to `data/headers.jsonl` every 500 messages.
- Output: `data/headers.jsonl` (one JSON object per line).

**Next:** `python auto_classify.py`

---

## 4. Classify senders (AI)

Builds sender → label + tier from headers (and optional body snippets) and writes rules + report:

```bash
python auto_classify.py --min-msgs 1
```

- Reads `data/headers.jsonl`, groups by sender.
- Calls OpenAI (then Anthropic if needed) per sender; caches in `data/auto_classifications.json`.
- Writes:
  - `output/filter_rules.json` (for apply_labels, backfill, export_filters_xml)
  - `output/auto_classify_report.txt`

**Useful flags:**

| Flag | Purpose |
|------|--------|
| `--reclassify` | Ignore cache and re-classify every sender |
| `--uncertain-only` | Re-classify only senders below confidence threshold |
| `--dry-run` | Print prompts only, no API calls or file writes |
| `--dump-senders` | Print all cached classifications and exit |
| `--review-uncertain` | Print low-confidence entries and exit |
| `--override ADDR LABEL TIER` | Manually set one sender and exit |

Example: re-classify only low-confidence senders:

```bash
python auto_classify.py --uncertain-only
```

**Alternative (rule-based):** If you prefer to write rules manually in YAML instead of using AI,
use `analyze.py` with `config/rules.yaml` (see `config/rules.example.yaml` for the format).

---

## 5. Create Gmail labels

Creates the labels referenced in `output/filter_rules.json` (does **not** create filters):

```bash
python apply_labels.py --dry-run   # preview
python apply_labels.py             # create labels
```

Optional: set colours on existing labels:

```bash
python apply_labels.py --update-colors
```

Filters are added later via XML import (step 7).

---

## 6. Backfill — label existing messages

Apply the rules to all messages in `data/headers.jsonl` (add labels, archive/mark read by tier):

```bash
python backfill.py --dry-run   # preview
python backfill.py             # run
```

**Tier behaviour:**

- **Tier 1:** Add label only (stays in inbox).
- **Tier 2–3:** Add label + remove from inbox (stay unread).
- **Tier 4:** Add label + remove from inbox + mark read.

Optional: only certain tiers, e.g.:

```bash
python backfill.py --tier 2,3
python backfill.py --tier 4
python backfill.py --tier 1
```

Optional: clean up empty orphan labels:

```bash
python backfill.py --cleanup-labels --dry-run   # preview
python backfill.py --cleanup-labels             # delete empty labels
```

---

## 7. Export filters XML and import in Gmail

Generate Gmail-importable filters from `output/filter_rules.json`:

```bash
python export_filters_xml.py
```

Writes `output/gmail_filters.xml`.

**Optional — Tier 1 forwarding**

To add "forward to" for Tier 1 senders, set your forwarding address and use:

```bash
set GMAIL_FORWARD_TO=your-other@email.com
python export_filters_xml.py --forward-tier1
```

(Forwarding must be enabled in Gmail settings.)

**Optional — Author / extra filter**

- `GMAIL_USER` / `GMAIL_NAME`: author in the XML (defaults are placeholders).
- Extra filter (e.g. "to:user+forms@gmail.com" → one label):
  - `GMAIL_EXTRA_FILTER_QUERY` e.g. `to:(you+forms@gmail.com)`
  - `GMAIL_EXTRA_FILTER_LABEL` e.g. `Dev/Netlify Forms`

**Import in Gmail**

1. Gmail → **Settings** → **See all settings** → **Filters and Blocked Addresses**.
2. At the bottom: **Import filters**.
3. Choose `output/gmail_filters.xml`.
4. Confirm.

New mail will then be filtered and labelled automatically.

---

## Quick reference — full pipeline (no reset)

```bash
python auth_setup.py              # once
python fetch.py                   # fetch headers
python auto_classify.py           # classify senders → filter_rules.json
python apply_labels.py            # create labels in Gmail
python backfill.py                # label existing messages
python export_filters_xml.py      # generate gmail_filters.xml
# → Import output/gmail_filters.xml in Gmail
```

---

## Quick reference — full reset then pipeline

```bash
python reset_labels.py            # strip labels, restore inbox, delete user labels
# Delete Gmail filters by hand in Settings
python fetch.py --reset           # optional: wipe local fetch data
# Optional: delete data/auto_classifications.json, output/filter_rules.json, etc.
python fetch.py
python auto_classify.py --reclassify --min-msgs 1
python apply_labels.py
python backfill.py
python export_filters_xml.py      # add --forward-tier1 + GMAIL_FORWARD_TO if needed
# → Import output/gmail_filters.xml in Gmail
```

---

## Where things live

| Path | Purpose |
|------|--------|
| `data/token.json` | OAuth token (from auth_setup.py) |
| `data/message_ids.txt` | All Gmail message IDs |
| `data/headers.jsonl` | One JSON per message (id, from, subject, list, labels) |
| `data/auto_classifications.json` | Sender → label/tier cache |
| `data/api_keys.json` | Optional store for OpenAI/Anthropic keys |
| `output/filter_rules.json` | Rules used by apply_labels, backfill, export |
| `output/auto_classify_report.txt` | Human-readable classification report |
| `output/gmail_filters.xml` | Gmail filter import file |
