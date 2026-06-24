# Automated Research Digest

An automated pipeline that searches academic databases and patent sources for
recent, relevant work, summarises each result with an LLM, builds a formatted
HTML digest, and emails it to a team on a weekly schedule.

It is configured entirely through two YAML files — no code changes are needed to
change what it searches for.

---

## What it does

Each run, the pipeline:

1. **Searches papers** — arXiv, Semantic Scholar, and OpenAlex for recent papers
   matching a keyword taxonomy.
2. **Searches patents** *(optional)* — Google Patents (via SerpAPI) by company
   assignee and CPC technology code.
3. **Filters & deduplicates** — scores results against the taxonomy and skips
   anything already sent in a previous run.
4. **Summarises** — sends each result to an LLM and extracts a structured
   summary (plain-English overview, key contribution, relevance, importance
   rating). Falls back gracefully if the LLM is unavailable.
5. **Builds an HTML digest** — a clean email with a papers section and a patents
   section, sorted by importance.
6. **Emails it** and saves a local copy under `digests/`.

---

## Project structure

```
.
├── pipeline.py            # Main pipeline — all core logic
├── robust_json_parse.py   # Tolerant parser for LLM JSON responses
├── email_sender.py        # SMTP helper (auto-detects Outlook vs Gmail)
├── test_email.py          # Standalone email delivery test
├── taxonomy.yaml          # Paper search config — topics, sources, LLM settings
├── patent_taxonomy.yaml   # Patent search config — assignees, CPC codes
├── requirements.txt       # Python dependencies
├── seen_dois.txt          # Tracks papers already sent (deduplication)
└── seen_patents.txt       # Tracks patents already sent (deduplication)
```

---

## Setup

**Requirements:** Python 3.11+

```bash
# 1. Clone
git clone https://github.com/bharatkumariithyd-lang/Automated-Research-Digest.git
cd Automated-Research-Digest

# 2. Create a virtual environment and install dependencies
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

# 3. Add your secrets (see below), then run
python pipeline.py
```

### Environment variables

Create a `.env` file in the project root (it is gitignored — never commit it):

```env
GROQ_API_KEY=...            # primary LLM (api.groq.com)
GOOGLE_AI_KEY=...           # fallback LLM (aistudio.google.com)
SERPAPI_KEY=...             # patent search (serpapi.com) — optional
SEMANTIC_SCHOLAR_KEY=...    # optional — raises rate limits
SENDER_EMAIL=...            # email account to send from
SENDER_PASSWORD=...         # email password / app password
RECIPIENT_EMAILS=a@x.com,b@y.com   # comma-separated recipients
```

> OpenAlex needs no key — it uses the polite pool with `SENDER_EMAIL` in the
> request header.

### Email provider — Outlook / Microsoft 365 or Gmail

The pipeline **auto-detects the SMTP settings from your `SENDER_EMAIL`**, so
switching providers needs **no code changes** — only different env vars:

| Provider | `SENDER_EMAIL` looks like | SMTP used (automatic) | `SENDER_PASSWORD` to use |
|---|---|---|---|
| Outlook / Microsoft 365 *(default)* | `you@company.com`, `you@outlook.com` | `smtp.office365.com:587` (STARTTLS) | account password (App Password if MFA is enforced) |
| Gmail | `you@gmail.com` | `smtp.gmail.com:465` (SSL) | **Gmail App Password** (16 characters) |

**To use Gmail instead of Outlook:**

1. Put a Gmail address in `SENDER_EMAIL`.
2. Enable **2-Step Verification** on that Google account (required for the next step).
3. Create an **App Password**: Google Account → *Security* → *2-Step Verification* → *App passwords* → generate one for "Mail".
4. Put that 16-character App Password in `SENDER_PASSWORD` — **not** your normal Gmail password (Gmail rejects the real password for SMTP).

The pipeline sees the `gmail.com` address and switches to Gmail's SMTP server
automatically; nothing in the code changes.

---

## Configuration

All search behaviour lives in the two YAML files — edit them, save, and the next
run picks up the changes.

- **`taxonomy.yaml`** — paper topics & keywords, which sources to use, lookback
  window, results per query, LLM provider/model, and email settings.
- **`patent_taxonomy.yaml`** — set `enabled: true`, then list the company
  assignees and CPC codes to monitor. Patent search is **disabled by default**.

Both files ship with a generic AI/ML example taxonomy. Replace the topics,
assignees, and CPC codes with whatever fits your domain.

---

## LLM summarisation

Summaries use a three-tier fallback so a single outage never breaks a run:

1. **Groq** (primary) — fast and free.
2. **Google AI Studio** (automatic fallback).
3. **Raw abstract** (last resort, if both LLMs are unavailable).

LLM responses are parsed with `robust_json_parse.py`, which tolerates markdown
code fences, surrounding prose, and trailing commas before falling back.

---

## Scheduling

The pipeline is designed to run on a weekly schedule (e.g. via GitHub Actions
or any cron runner). Because hosted runners have ephemeral filesystems, persist
`seen_dois.txt` / `seen_patents.txt` between runs so the deduplication state is
not lost.

---

## Tech & data sources

- **Language:** Python 3.11
- **Papers:** arXiv · Semantic Scholar · OpenAlex
- **Patents:** Google Patents (via SerpAPI)
- **LLMs:** Groq · Google AI Studio
- **Email:** SMTP (Outlook / Microsoft 365 or Gmail)
