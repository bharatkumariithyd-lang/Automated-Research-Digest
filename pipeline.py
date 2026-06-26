"""
pipeline.py
===========
Literature Mining Pipeline — Phase 1
Searches Semantic Scholar + arXiv using taxonomy.yaml,
extracts structured data using Gemma (Google AI Studio free API),
and sends a formatted HTML email digest.

SETUP REQUIRED (see README.md):
  - GOOGLE_AI_KEY   : Google AI Studio free API key
  - SENDER_EMAIL    : Gmail address to send from
  - SENDER_PASSWORD : Gmail App Password (not your real password)
  - RECIPIENT_EMAILS: Comma-separated list of recipient emails

Run manually:
  python pipeline.py

Runs automatically:
  Every Monday 8am via GitHub Actions (see .github/workflows/weekly_digest.yml)
"""

import os
import time
import hashlib
import smtplib
import logging
import requests
import yaml
from dotenv import load_dotenv
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from robust_json_parse import parse_llm_json_with_fallback

# Load .env file — reads API keys stored locally
# On GitHub Actions, secrets are loaded automatically instead
load_dotenv()

# ── Logging setup ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── File paths ───────────────────────────────────────────────
TAXONOMY_FILE        = "taxonomy.yaml"
PATENT_TAXONOMY_FILE = "patent_taxonomy.yaml"
SEEN_DOIS_FILE       = "seen_dois.txt"    # Tracks papers already sent
SEEN_PATENTS_FILE    = "seen_patents.txt"  # Tracks patents already sent

# OpenAlex field IDs — map the human-readable field names used in taxonomy.yaml
# (fields_of_study) to OpenAlex's numeric field identifiers.
# Full list: https://api.openalex.org/fields
OPENALEX_FIELD_IDS = {
    "Materials Science":            "25",
    "Engineering":                  "22",
    "Chemistry":                    "16",
    "Chemical Engineering":         "15",
    "Physics and Astronomy":        "31",
    "Environmental Science":        "23",
    "Earth and Planetary Sciences": "19",
}


# ═══════════════════════════════════════════════════════════
# STEP 1 — Load taxonomy
# ═══════════════════════════════════════════════════════════

def load_taxonomy() -> dict:
    """Read taxonomy.yaml and return as a dictionary."""
    with open(TAXONOMY_FILE, "r") as f:
        taxonomy = yaml.safe_load(f)
    log.info(f"Taxonomy loaded — {len(taxonomy['topics'])} topic groups")
    return taxonomy


def load_patent_taxonomy() -> dict:
    """
    Load patent_taxonomy.yaml — separate from paper taxonomy.
    Returns disabled config if file not found so pipeline still runs.
    """
    patent_file = Path(PATENT_TAXONOMY_FILE)

    if not patent_file.exists():
        log.warning(f"{PATENT_TAXONOMY_FILE} not found — patent search disabled")
        return {"enabled": False}

    with open(PATENT_TAXONOMY_FILE, "r") as f:
        pt = yaml.safe_load(f)

    assignee_count = len(pt.get("competitor_assignees", []))
    cpc_count      = len(pt.get("cpc_codes", []))
    log.info(f"Patent taxonomy loaded — {assignee_count} assignees · {cpc_count} CPC codes")
    return pt


def get_all_keywords(taxonomy: dict) -> list[str]:
    """Flatten all keywords from all topic groups into one list."""
    keywords = []
    for group, terms in taxonomy["topics"].items():
        keywords.extend(terms)
    return list(set(keywords))  # Remove duplicates


# ═══════════════════════════════════════════════════════════
# STEP 2 — Search APIs
# ═══════════════════════════════════════════════════════════

def search_semantic_scholar(query: str, days_back: int, max_results: int,
                            fields_of_study: list[str] = None) -> list[dict]:
    """
    Search Semantic Scholar API for papers matching the query.
    Returns a list of paper dicts with title, abstract, authors, year, doi, url.
    API docs: https://api.semanticscholar.org/graph/v1
    Includes automatic retry on 429 rate limit errors.
    """
    base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    
    # Calculate date range
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    
    params = {
        "query": query,
        "fields": "title,abstract,authors,year,publicationDate,externalIds,url,venue",
        "publicationDateOrYear": f"{start_date}:{end_date}",
        "limit": min(max_results, 100),
    }
    # Restrict to chosen academic fields (drops biology/medicine/etc.)
    if fields_of_study:
        params["fieldsOfStudy"] = ",".join(fields_of_study)
    
    # Retry up to 3 times on rate limit errors
    max_retries = 3
    for attempt in range(max_retries):
        try:
            api_key = os.environ.get("SEMANTIC_SCHOLAR_KEY", "")
            headers = {"x-api-key": api_key} if api_key else {}
            response = requests.get(base_url, headers=headers, params=params, timeout=30)
            
            # If rate limited — wait and retry
            # log.info(f"Semantic Scholar response: {response.status_code} — {response.text[:200]}")
            if response.status_code == 429:
                wait_time = 10 * (attempt + 1)  # 10s, 20s, 30s
                log.warning(f"  Rate limited by Semantic Scholar — waiting {wait_time}s before retry {attempt+1}/{max_retries}")
                time.sleep(wait_time)
                continue
            
            response.raise_for_status()
            data = response.json()
            
            papers = []
            for item in data.get("data", []):
                if not item.get("abstract"):
                    continue
                
                doi      = item.get("externalIds", {}).get("DOI", "")
                paper_id = item.get("paperId", "")
                
                papers.append({
                    "title":    item.get("title", "No title"),
                    "abstract": item.get("abstract", ""),
                    "authors":  [a.get("name", "") for a in item.get("authors", [])[:4]],
                    "year":     item.get("year", ""),
                    "venue":    item.get("venue", ""),
                    "doi":      doi,
                    "url":      item.get("url", f"https://www.semanticscholar.org/paper/{paper_id}"),
                    "source":   "Semantic Scholar",
                    "uid":      doi or paper_id or hashlib.md5(item.get("title","").encode()).hexdigest(),
                })
            
            log.info(f"  Semantic Scholar '{query}': {len(papers)} papers with abstracts")
            return papers
        
        except requests.RequestException as e:
            log.warning(f"  Semantic Scholar error for '{query}' (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
    
    log.warning(f"  Semantic Scholar '{query}': failed after {max_retries} attempts — skipping")
    return []


def search_arxiv(query: str, days_back: int, max_results: int) -> list[dict]:
    """
    Search arXiv API for preprints matching the query.
    Returns a list of paper dicts.
    API docs: https://arxiv.org/help/api
    Includes automatic retry on 429 rate limit errors.
    """
    base_url = "https://export.arxiv.org/api/query"
    
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": min(max_results, 50),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.get(base_url, params=params, timeout=30)
            
            # If rate limited — wait and retry
            if response.status_code == 429:
                wait_time = 15 * (attempt + 1)  # 15s, 30s, 45s
                log.warning(f"  Rate limited by arXiv — waiting {wait_time}s before retry {attempt+1}/{max_retries}")
                time.sleep(wait_time)
                continue
            
            response.raise_for_status()
            
            # arXiv returns XML — parse it simply
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            
            cutoff_date = datetime.now() - timedelta(days=days_back)
            papers = []
            
            for entry in root.findall("atom:entry", ns):
                published_str = entry.findtext("atom:published", "", ns)
                if published_str:
                    pub_date = datetime.fromisoformat(published_str[:10])
                    if pub_date < cutoff_date:
                        continue
                
                title    = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")
                abstract = entry.findtext("atom:summary", "", ns).strip().replace("\n", " ")
                arxiv_id = entry.findtext("atom:id", "", ns).split("/abs/")[-1]
                
                if not abstract:
                    continue
                
                authors = [
                    a.findtext("atom:name", "", ns)
                    for a in entry.findall("atom:author", ns)
                ][:4]
                
                papers.append({
                    "title":    title,
                    "abstract": abstract,
                    "authors":  authors,
                    "year":     pub_date.year if published_str else "",
                    "venue":    "arXiv",
                    "doi":      "",
                    "url":      f"https://arxiv.org/abs/{arxiv_id}",
                    "source":   "arXiv",
                    "uid":      arxiv_id,
                })
            
            log.info(f"  arXiv '{query}': {len(papers)} papers")
            return papers
        
        except Exception as e:
            log.warning(f"  arXiv error for '{query}' (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(10)
    
    log.warning(f"  arXiv '{query}': failed after {max_retries} attempts — skipping")
    return []


def search_openalex(query: str, days_back: int, max_results: int,
                    fields_of_study: list[str] = None) -> list[dict]:
    """
    Search OpenAlex for recent papers matching the query.

    OpenAlex is the most comprehensive free academic database — 250M+ works,
    strong coverage of metallurgy journals (Elsevier, Springer, Wiley) that
    arXiv and Semantic Scholar miss. No API key required.

    Polite pool: sending an email in the User-Agent header gives 10 req/s
    instead of the anonymous 5 req/s. Uses SENDER_EMAIL from .env.

    API docs: https://docs.openalex.org/api-entities/works/search-works
    """
    base_url    = "https://api.openalex.org/works"
    cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Polite pool — include contact email in User-Agent
    contact_email = os.environ.get("SENDER_EMAIL", "research-digest@example.com")
    headers = {
        "User-Agent": f"research-digest-pipeline/1.0 (mailto:{contact_email})"
    }

    # Build the filter — date window plus optional academic-field restriction
    filter_parts = [f"from_publication_date:{cutoff_date}"]
    if fields_of_study:
        ids = [OPENALEX_FIELD_IDS[f] for f in fields_of_study if f in OPENALEX_FIELD_IDS]
        if ids:
            filter_parts.append("primary_topic.field.id:" + "|".join(f"fields/{i}" for i in ids))

    params = {
        "search":    query,
        "filter":    ",".join(filter_parts),
        "per-page":  min(max_results, 50),   # OpenAlex max per page is 200; 50 is plenty
        "select":    "id,doi,title,abstract_inverted_index,authorships,publication_year,primary_location",
        "sort":      "publication_date:desc",
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.get(base_url, headers=headers, params=params, timeout=30)

            if response.status_code == 429:
                wait_time = 15 * (attempt + 1)
                log.warning(f"  Rate limited by OpenAlex — waiting {wait_time}s before retry {attempt+1}/{max_retries}")
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            data    = response.json()
            results = data.get("results", [])

            papers = []
            for item in results:
                title = (item.get("title") or "").strip()
                if not title:
                    continue

                # Abstract is stored as an inverted index — reconstruct it
                # Format: {"word": [position1, position2], ...}
                inverted = item.get("abstract_inverted_index") or {}
                if inverted:
                    positions = {}
                    for word, pos_list in inverted.items():
                        for pos in pos_list:
                            positions[pos] = word
                    abstract = " ".join(positions[i] for i in sorted(positions))
                else:
                    abstract = ""

                if not abstract:
                    continue   # Skip papers with no abstract — same policy as other sources

                # DOI — strip the URL prefix if present
                doi = (item.get("doi") or "").replace("https://doi.org/", "").strip()

                # Authors — first 4 only
                authors = []
                for a in item.get("authorships", [])[:4]:
                    name = (a.get("author") or {}).get("display_name", "")
                    if name:
                        authors.append(name)

                # Journal / venue name
                location = item.get("primary_location") or {}
                source   = location.get("source") or {}
                venue    = source.get("display_name", "")

                # Canonical URL — prefer DOI, fall back to OpenAlex page
                openalex_id = item.get("id", "")   # e.g. https://openalex.org/W12345
                url = (
                    f"https://doi.org/{doi}" if doi
                    else openalex_id
                )

                # UID — prefer DOI, then OpenAlex ID (always unique)
                uid = doi or openalex_id.split("/")[-1] or hashlib.md5(title.encode()).hexdigest()

                papers.append({
                    "title":    title,
                    "abstract": abstract,
                    "authors":  authors,
                    "year":     item.get("publication_year", ""),
                    "venue":    venue,
                    "doi":      doi,
                    "url":      url,
                    "source":   "OpenAlex",
                    "uid":      uid,
                })

            log.info(f"  OpenAlex '{query}': {len(papers)} papers with abstracts")
            return papers

        except requests.RequestException as e:
            log.warning(f"  OpenAlex error for '{query}' (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)

    log.warning(f"  OpenAlex '{query}': failed after {max_retries} attempts — skipping")
    return []


# ═══════════════════════════════════════════════════════════
# STEP 2b — Search patents via SerpAPI Google Patents
# ═══════════════════════════════════════════════════════════

def search_serp_patents(patent_taxonomy: dict, api_key: str) -> list[dict]:
    """
    Search Google Patents via SerpAPI — covers US, EU, WIPO and more.
    Makes two separate queries:
      1. By assignee company names  (competitor monitoring)
      2. By CPC technology codes    (technology area monitoring)
    Results are merged and deduplicated.
    SerpAPI free tier: 250 searches/month — more than sufficient for weekly runs.
    API docs: https://serpapi.com/google-patents-api
    """
    if not patent_taxonomy.get("enabled", False):
        return []

    if not api_key:
        log.warning("SERPAPI_KEY not set — patent search skipped")
        return []

    days_back  = patent_taxonomy.get("lookback_days", 30)
    max_res    = min(patent_taxonomy.get("results_per_query", 25), 100)
    assignees  = patent_taxonomy.get("competitor_assignees", [])
    cpc_codes  = patent_taxonomy.get("cpc_codes", [])
    after_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
    base_url   = "https://serpapi.com/search"

    all_patents = {}  # uid → patent dict, auto-deduplicates across queries

    # ── Query 1: Search by assignee company names ────────────
    # Use assignee:"Company Name" syntax inside the q field
    if assignees:
        for company in assignees:
            params = {
                "engine":  "google_patents",
                "q":       f'assignee:"{company}"',
                "after":   f"publication:{after_date}",   # SerpAPI needs a date-type prefix
                "num":     max_res,
                "api_key": api_key,
            }
            try:
                resp = requests.get(base_url, params=params, timeout=30)
                if resp.status_code == 200:
                    data   = resp.json()
                    error  = data.get("error", "")
                    if error:
                        log.warning(f"  SerpAPI assignee '{company}': {error}")
                        continue
                    results = data.get("organic_results", [])
                    before  = len(all_patents)
                    for item in results:
                        patent = _parse_serp_patent(item)
                        if patent:
                            all_patents[patent["uid"]] = patent
                    log.info(f"  SerpAPI '{company}': {len(all_patents) - before} patents")
                else:
                    log.warning(f"  SerpAPI '{company}' HTTP {resp.status_code}")
            except Exception as e:
                log.warning(f"  SerpAPI '{company}' error: {e}")
            time.sleep(1)

    log.info(f"  SerpAPI assignee total so far: {len(all_patents)} patents")

    # ── Query 2: Search by CPC technology codes ──────────────
    # SerpAPI Google Patents has NO 'cpc' parameter — the CPC code must go
    # inside 'q' with spaces removed (e.g. "C21C7/00"), and the date filter
    # needs a type prefix (publication:YYYYMMDD).
    if cpc_codes:
        for code in cpc_codes:
            params = {
                "engine":  "google_patents",
                "q":       code.replace(" ", ""),
                "after":   f"publication:{after_date}",
                "num":     max_res,
                "api_key": api_key,
            }
            try:
                before_count = len(all_patents)
                resp = requests.get(base_url, params=params, timeout=30)
                if resp.status_code == 200:
                    data    = resp.json()
                    error   = data.get("error", "")
                    if error:
                        log.warning(f"  SerpAPI CPC '{code}' error: {error}")
                        continue
                    results = data.get("organic_results", [])
                    for item in results:
                        patent = _parse_serp_patent(item)
                        if patent:
                            all_patents[patent["uid"]] = patent
                    log.info(f"  SerpAPI CPC '{code}': {len(all_patents) - before_count} new patents")
                else:
                    log.warning(f"  SerpAPI CPC '{code}' HTTP {resp.status_code}")
            except Exception as e:
                log.warning(f"  SerpAPI CPC '{code}' error: {e}")
            time.sleep(1)

    patents = list(all_patents.values())
    log.info(f"SerpAPI total patents found: {len(patents)} (last {days_back} days)")
    return patents


def _parse_serp_patent(item: dict) -> dict | None:
    """
    Parse a single patent result from SerpAPI Google Patents response.
    Returns a standardised patent dict or None if insufficient data.
    """
    title    = item.get("title", "").strip()
    abstract = item.get("snippet", "").strip()
    pat_id   = item.get("publication_number", "") or item.get("patent_id", "")

    # Skip if missing essential fields
    if not title or not pat_id:
        return None

    # Use snippet as abstract — SerpAPI returns Google Patents excerpt
    # If snippet is very short, note it for engineers
    if len(abstract) < 50:
        abstract = f"[Short excerpt] {abstract}" if abstract else "Abstract not available in search results — click link to view full patent."

    return {
        "patent_id":  pat_id,
        "title":      title,
        "abstract":   abstract,
        "assignee":   item.get("assignee", "Unknown assignee"),
        "inventor":   item.get("inventor", "Unknown"),
        "grant_date": item.get("grant_date", item.get("publication_date", "")),
        "app_date":   item.get("filing_date", item.get("priority_date", "")),
        "cpc_code":   "",  # Not returned directly by SerpAPI Google Patents
        "url":        item.get("pdf", "") or f"https://patents.google.com/patent/{pat_id}",
        "source":     "Google Patents via SerpAPI",
        "uid":        f"GP-{pat_id}",
    }


def score_paper(paper: dict, taxonomy: dict) -> tuple[int, list[str]]:
    """
    Score a paper against the taxonomy keywords.
    Returns (score, list of matched topics).
    Score = number of distinct keyword matches in title + abstract.
    """
    text = f"{paper['title']} {paper['abstract']}".lower()
    matched = []

    for group_name, keywords in taxonomy["topics"].items():
        for kw in keywords:
            if kw.lower() in text:
                matched.append(kw)
                break  # One match per group is enough
    
    return len(matched), matched


def filter_and_deduplicate(papers: list[dict], taxonomy: dict) -> list[dict]:
    """
    Filter papers by relevance threshold and remove duplicates.
    Also removes papers already sent in previous weeks.
    """
    threshold = taxonomy.get("relevance_threshold", 1)
    
    # Load previously seen paper IDs
    seen_path = Path(SEEN_DOIS_FILE)
    seen_ids  = set(seen_path.read_text().splitlines()) if seen_path.exists() else set()
    
    filtered  = []
    seen_this_run = set()
    
    for paper in papers:
        uid = paper["uid"]
        
        # Skip if already sent or seen this run (deduplication)
        if uid in seen_ids or uid in seen_this_run:
            continue
        seen_this_run.add(uid)
        
        # Score against taxonomy
        score, matched_topics = score_paper(paper, taxonomy)
        
        if score >= threshold:
            paper["relevance_score"]  = score
            paper["matched_topics"]   = matched_topics
            filtered.append(paper)
    
    # Save newly seen IDs so we never send them again
    all_seen = seen_ids | seen_this_run
    seen_path.write_text("\n".join(sorted(all_seen)))
    
    log.info(f"Filter: {len(papers)} total → {len(filtered)} relevant and new")
    return filtered


def score_patent(patent: dict, patent_taxonomy: dict) -> tuple[int, list[str]]:
    """Score a patent against the patent taxonomy keywords."""
    text    = f"{patent['title']} {patent['abstract']}".lower()
    matched = []

    for group_name, keywords in patent_taxonomy.get("keywords", {}).items():
        for kw in keywords:
            if kw.lower() in text:
                matched.append(kw)
                break  # One match per group is enough

    return len(matched), matched


def filter_patents(patents: list[dict], patent_taxonomy: dict) -> list[dict]:
    """
    Remove patents already seen in previous runs.
    Also scores against patent taxonomy keywords.
    Uses seen_patents.txt — separate from seen_dois.txt for papers.
    """
    threshold = patent_taxonomy.get("relevance_threshold", 1)
    seen_path = Path(SEEN_PATENTS_FILE)
    seen_ids  = set(seen_path.read_text(encoding="utf-8").splitlines()) if seen_path.exists() else set()

    filtered      = []
    seen_this_run = set()

    for patent in patents:
        uid = patent["uid"]
        if uid in seen_ids or uid in seen_this_run:
            continue
        seen_this_run.add(uid)

        score, matched = score_patent(patent, patent_taxonomy)

        # If no keywords defined, pass all patents through
        kw_defined = bool(patent_taxonomy.get("keywords", {}))
        if not kw_defined or score >= threshold:
            patent["relevance_score"]  = score
            patent["matched_keywords"] = matched
            filtered.append(patent)

    all_seen = seen_ids | seen_this_run
    seen_path.write_text("\n".join(sorted(all_seen)), encoding="utf-8")

    log.info(f"Patent filter: {len(patents)} total → {len(filtered)} new")
    return filtered

EXTRACTION_PROMPT = """You are a research analyst assistant. Read the paper details below and return a structured JSON summary.

PAPER TITLE: {title}
AUTHORS: {authors}
ABSTRACT: {abstract}

Return ONLY a valid JSON object with exactly these fields — no extra text, no markdown backticks:
{{
  "plain_summary": "2-3 sentence plain English explanation of what this paper found and why it matters. Avoid jargon.",
  "key_contribution": "The single most important new finding or method in one sentence.",
  "methods_used": ["list", "of", "main", "methods", "or", "techniques"],
  "relevance_note": "One sentence on why this is relevant to an R&D team working on: {topics}",
  "importance_rating": 3
}}

importance_rating must be an integer 1-5 where:
1 = marginally relevant, 5 = highly significant finding"""


def call_llm_api(prompt: str, api_key: str, model: str, max_tokens: int,
                 provider: str = "groq", fallback_provider: str = None,
                 fallback_api_key: str = None, fallback_model: str = None) -> str:
    """
    Unified LLM API caller with automatic fallback.
    Tries primary provider first. If it fails, automatically
    switches to fallback provider — no manual intervention needed.

    Provider options:
      groq   — free, fast, api.groq.com (set GROQ_API_KEY in .env)
      google — free, aistudio.google.com (set GOOGLE_AI_KEY in .env)
    """
    providers_to_try = [(provider, api_key, model)]

    if fallback_provider and fallback_api_key and fallback_model:
        providers_to_try.append((fallback_provider, fallback_api_key, fallback_model))

    last_error = None

    for current_provider, current_key, current_model in providers_to_try:

        if not current_key:
            log.warning(f"  LLM skipping {current_provider} — no API key set")
            continue

        log.info(f"  LLM using {current_provider.upper()} ({current_model})")
        max_retries = 2

        for attempt in range(max_retries):
            try:
                if current_provider == "groq":
                    # ── Groq API (OpenAI-compatible) ──────────────
                    url     = "https://api.groq.com/openai/v1/chat/completions"
                    headers = {
                        "Authorization": f"Bearer {current_key}",
                        "Content-Type":  "application/json"
                    }
                    payload = {
                        "model":       current_model,
                        "messages":    [{"role": "user", "content": prompt}],
                        "max_tokens":  max_tokens,
                        "temperature": 0.1,
                        # Groq native JSON mode — forces syntactically valid JSON
                        # at the source. Requires the word "json" in the prompt;
                        # our extraction prompts already ask for a JSON object.
                        "response_format": {"type": "json_object"},
                    }
                    response = requests.post(url, headers=headers, json=payload, timeout=60)
                    response.raise_for_status()
                    return response.json()["choices"][0]["message"]["content"].strip()

                else:
                    # ── Google AI Studio (Gemma / Gemini) ─────────
                    url     = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent"
                    headers = {"Content-Type": "application/json"}
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "maxOutputTokens": max_tokens,
                            "temperature":     0.1,
                        }
                    }
                    response = requests.post(url, headers=headers,
                                             params={"key": current_key},
                                             json=payload, timeout=60)
                    response.raise_for_status()
                    return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                last_error = e
                wait = 10 * (attempt + 1)
                log.warning(f"  {current_provider.upper()} attempt {attempt+1}/{max_retries} failed — waiting {wait}s")
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    log.warning(f"  {current_provider.upper()} failed — trying fallback if available")

            except Exception as e:
                last_error = e
                log.warning(f"  {current_provider.upper()} error: {e} — trying fallback if available")
                break  # Non-network error — skip retries, try fallback immediately

    # All providers failed
    raise last_error or Exception("All LLM providers failed")


def extract_paper_data(paper: dict, taxonomy: dict, api_key: str, llm_config: dict) -> dict:
    """
    Send paper abstract to Gemma and get structured extraction back.
    Falls back to a basic summary if the API call fails.
    """
    topics_str = ", ".join([
        kw for group in taxonomy["topics"].values() for kw in group[:3]
    ])
    
    prompt = EXTRACTION_PROMPT.format(
        title    = paper["title"],
        authors  = ", ".join(paper["authors"]) or "Unknown",
        abstract = paper["abstract"][:2000],  # Trim very long abstracts
        topics   = topics_str,
    )
    
    model              = llm_config.get("model", "llama-3.3-70b-versatile")
    max_tokens         = llm_config.get("max_tokens", 500)
    provider           = llm_config.get("provider", "groq")
    fallback_provider  = llm_config.get("fallback_provider", None)
    fallback_model     = llm_config.get("fallback_model", None)
    fallback_key       = llm_config.get("_fallback_key", None)

    try:
        raw_response = call_llm_api(
            prompt, api_key, model, max_tokens, provider,
            fallback_provider, fallback_key, fallback_model
        )
        
        # Parse JSON tolerantly — handles markdown fences, prose wrappers and
        # trailing commas (see robust_json_parse.py). The fallback keeps the
        # raw text as the summary but fills every field the template expects.
        parse_fallback = {
            "plain_summary":    raw_response[:400],
            "key_contribution": "See abstract",
            "methods_used":     [],
            "relevance_note":   "Manual review recommended — could not parse AI summary",
            "importance_rating": 2,
        }
        extracted, ok = parse_llm_json_with_fallback(raw_response, parse_fallback)
        paper["extraction"] = extracted
        if ok:
            log.info(f"  Extracted: {paper['title'][:60]}...")
        else:
            log.warning(f"  JSON parse failed for: {paper['title'][:50]} — using raw text")
        
    except Exception as e:
        # API call failed — fall back to showing abstract directly
        log.warning(f"  LLM API error for '{paper['title'][:50]}': {e}")
        paper["extraction"] = {
            "plain_summary":    paper["abstract"][:400],
            "key_contribution": "See abstract",
            "methods_used":     [],
            "relevance_note":   "LLM unavailable — showing raw abstract",
            "importance_rating": 2,
        }
    
    # Respect API rate limits — 1 second between calls
    time.sleep(1)
    return paper


PATENT_PROMPT = """You are a patent intelligence analyst for an AI and machine learning R&D team.
Read the patent details below and return a structured JSON summary.

PATENT TITLE: {title}
ASSIGNEE (company that owns this): {assignee}
GRANT DATE: {grant_date}
CPC CODE: {cpc_code}
ABSTRACT: {abstract}

Return ONLY a valid JSON object — no extra text, no markdown backticks:
{{
  "plain_summary": "2-3 sentence plain English explanation of what this patent protects and how it works. No legal jargon.",
  "technology_area": "One short phrase describing the technology e.g. transformer attention mechanism",
  "key_protection": "One sentence on exactly what this patent claims ownership of",
  "competitive_concern": "One sentence on why an AI R&D team should be aware of this patent",
  "importance_rating": 3
}}

importance_rating must be an integer 1-5 where:
1 = minor incremental patent, 5 = broad foundational claim on core AI technology"""


def extract_patent_data(patent: dict, api_key: str, llm_config: dict) -> dict:
    """
    Send patent abstract to Gemma and get structured extraction back.
    Uses a different prompt from papers — focused on legal claims and competitive risk.
    """
    prompt = PATENT_PROMPT.format(
        title      = patent["title"],
        assignee   = patent["assignee"],
        grant_date = patent["grant_date"],
        cpc_code   = patent["cpc_code"],
        abstract   = patent["abstract"][:2000],
    )

    model             = llm_config.get("model", "llama-3.3-70b-versatile")
    max_tokens        = llm_config.get("max_tokens", 400)
    provider          = llm_config.get("provider", "groq")
    fallback_provider = llm_config.get("fallback_provider", None)
    fallback_model    = llm_config.get("fallback_model", None)
    fallback_key      = llm_config.get("_fallback_key", None)

    try:
        raw_response = call_llm_api(
            prompt, api_key, model, max_tokens, provider,
            fallback_provider, fallback_key, fallback_model
        )
        # Parse JSON tolerantly — see robust_json_parse.py. Fallback keeps the
        # raw text as the summary but fills the fields the template expects.
        parse_fallback = {
            "plain_summary":      raw_response[:400],
            "technology_area":    "See abstract",
            "key_protection":     "See abstract",
            "competitive_concern": "Manual review recommended — could not parse AI summary",
            "importance_rating":  2,
        }
        patent["extraction"], ok = parse_llm_json_with_fallback(raw_response, parse_fallback)
        if ok:
            log.info(f"  Patent extracted: {patent['title'][:55]}...")
        else:
            log.warning(f"  Patent JSON parse failed: {patent['title'][:45]} — using raw")

    except Exception as e:
        log.warning(f"  Patent LLM error '{patent['title'][:45]}': {e}")
        patent["extraction"] = {
            "plain_summary":      patent["abstract"][:400],
            "technology_area":    "See abstract",
            "key_protection":     "See abstract",
            "competitive_concern": "LLM unavailable — showing raw abstract",
            "importance_rating":  2,
        }

    time.sleep(1)
    return patent

def importance_stars(rating: int) -> str:
    """Convert 1-5 rating to star string."""
    r = max(1, min(5, int(rating)))
    return "★" * r + "☆" * (5 - r)


def build_html_email(papers: list[dict], taxonomy: dict, patents: list[dict] = None) -> str:
    """
    Build a clean, professional HTML email digest.
    Each paper gets its own card with extracted data.
    """
    today     = datetime.now().strftime("%d %B %Y")
    count     = len(papers)
    topics_shown = ", ".join(list(taxonomy["topics"].keys())[:4])
    
    # Sort by importance rating — most important first
    papers_sorted = sorted(
        papers,
        key=lambda p: p.get("extraction", {}).get("importance_rating", 1),
        reverse=True
    )
    
    # Build one card per paper
    cards_html = ""
    for i, paper in enumerate(papers_sorted, 1):
        ext      = paper.get("extraction", {})
        authors  = ", ".join(paper["authors"]) if paper["authors"] else "Authors not listed"
        venue    = paper.get("venue", "") or paper.get("source", "")
        year     = paper.get("year", "")
        topics   = " · ".join(paper.get("matched_topics", []))
        stars    = importance_stars(ext.get("importance_rating", 2))
        methods  = ", ".join(ext.get("methods_used", [])) or "—"
        
        cards_html += f"""
        <div style="background:#ffffff;border:1px solid #e0ddd6;border-radius:10px;
                    padding:20px 24px;margin-bottom:16px;">
          
          <div style="display:flex;justify-content:space-between;align-items:flex-start;
                      margin-bottom:10px;">
            <span style="font-size:11px;color:#888;font-weight:600;
                         text-transform:uppercase;letter-spacing:0.05em;">
              Paper {i} of {count}
            </span>
            <span style="font-size:13px;color:#854F0B;" title="Importance rating">
              {stars}
            </span>
          </div>
          
          <h2 style="font-size:15px;font-weight:600;color:#1a1a18;
                     margin:0 0 6px;line-height:1.4;">
            <a href="{paper['url']}" style="color:#185FA5;text-decoration:none;">
              {paper['title']}
            </a>
          </h2>
          
          <p style="font-size:12px;color:#888;margin:0 0 14px;">
            {authors} &nbsp;·&nbsp; {venue} &nbsp;·&nbsp; {year}
          </p>
          
          <div style="background:#f5f3ee;border-radius:7px;padding:12px 16px;
                      margin-bottom:12px;">
            <p style="font-size:12px;font-weight:600;color:#444;
                       margin:0 0 5px;text-transform:uppercase;letter-spacing:0.04em;">
              Summary
            </p>
            <p style="font-size:13px;color:#333;margin:0;line-height:1.65;">
              {ext.get('plain_summary', paper['abstract'][:300])}
            </p>
          </div>
          
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;
                      margin-bottom:12px;">
            <div style="background:#e6f1fb;border-radius:7px;padding:10px 14px;">
              <p style="font-size:11px;font-weight:600;color:#0C447C;
                         margin:0 0 3px;text-transform:uppercase;">Key Contribution</p>
              <p style="font-size:12px;color:#1a1a18;margin:0;line-height:1.5;">
                {ext.get('key_contribution', '—')}
              </p>
            </div>
            <div style="background:#eeedfe;border-radius:7px;padding:10px 14px;">
              <p style="font-size:11px;font-weight:600;color:#3C3489;
                         margin:0 0 3px;text-transform:uppercase;">Methods Used</p>
              <p style="font-size:12px;color:#1a1a18;margin:0;line-height:1.5;">
                {methods}
              </p>
            </div>
          </div>
          
          <div style="background:#eaf3de;border-radius:7px;padding:10px 14px;
                      margin-bottom:12px;">
            <p style="font-size:11px;font-weight:600;color:#27500A;
                       margin:0 0 3px;text-transform:uppercase;">Why This Matters</p>
            <p style="font-size:12px;color:#1a1a18;margin:0;line-height:1.5;">
              {ext.get('relevance_note', '—')}
            </p>
          </div>
          
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-size:11px;color:#888;">
              Matched: <strong style="color:#555;">{topics}</strong>
            </span>
            <a href="{paper['url']}"
               style="font-size:12px;font-weight:600;color:#185FA5;
                      text-decoration:none;padding:5px 14px;border:1px solid #85B7EB;
                      border-radius:5px;">
              Read paper →
            </a>
          </div>
        </div>
        """
    
    # ── Build patent cards ────────────────────────────────────
    patents_html = ""
    if patents:
        patent_cards = ""
        for i, patent in enumerate(patents, 1):
            ext        = patent.get("extraction", {})
            stars      = importance_stars(ext.get("importance_rating", 2))
            cpc        = patent.get("cpc_code", "")
            assignee   = patent.get("assignee", "Unknown")
            grant_date = patent.get("grant_date", "")
            tech_area  = ext.get("technology_area", "—")
            key_prot   = ext.get("key_protection", "—")
            concern    = ext.get("competitive_concern", "—")
            summary    = ext.get("plain_summary", patent.get("abstract", "")[:300])

            patent_cards += f"""
        <div style="background:#ffffff;border:1px solid #e0ddd6;border-radius:10px;
                    padding:20px 24px;margin-bottom:16px;">

          <div style="display:flex;justify-content:space-between;align-items:flex-start;
                      margin-bottom:10px;">
            <span style="font-size:11px;color:#534AB7;font-weight:600;
                         text-transform:uppercase;letter-spacing:0.05em;">
              Patent {i} · {assignee}
            </span>
            <span style="font-size:13px;color:#854F0B;" title="Importance rating">
              {stars}
            </span>
          </div>

          <h2 style="font-size:15px;font-weight:600;color:#1a1a18;
                     margin:0 0 6px;line-height:1.4;">
            <a href="{patent['url']}" style="color:#534AB7;text-decoration:none;">
              {patent['title']}
            </a>
          </h2>

          <p style="font-size:12px;color:#888;margin:0 0 14px;">
            Granted: {grant_date} &nbsp;·&nbsp; CPC: {cpc} &nbsp;·&nbsp; {tech_area}
          </p>

          <div style="background:#f5f3ee;border-radius:7px;padding:12px 16px;
                      margin-bottom:12px;">
            <p style="font-size:12px;font-weight:600;color:#444;
                       margin:0 0 5px;text-transform:uppercase;letter-spacing:0.04em;">
              What it protects
            </p>
            <p style="font-size:13px;color:#333;margin:0;line-height:1.65;">
              {summary}
            </p>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;
                      margin-bottom:12px;">
            <div style="background:#eeedfe;border-radius:7px;padding:10px 14px;">
              <p style="font-size:11px;font-weight:600;color:#3C3489;
                         margin:0 0 3px;text-transform:uppercase;">Key claim</p>
              <p style="font-size:12px;color:#1a1a18;margin:0;line-height:1.5;">
                {key_prot}
              </p>
            </div>
            <div style="background:#faeeda;border-radius:7px;padding:10px 14px;">
              <p style="font-size:11px;font-weight:600;color:#633806;
                         margin:0 0 3px;text-transform:uppercase;">Watch out</p>
              <p style="font-size:12px;color:#1a1a18;margin:0;line-height:1.5;">
                {concern}
              </p>
            </div>
          </div>

          <div style="text-align:right;">
            <a href="{patent['url']}"
               style="font-size:12px;font-weight:600;color:#534AB7;
                      text-decoration:none;padding:5px 14px;border:1px solid #AFA9EC;
                      border-radius:5px;">
              View patent →
            </a>
          </div>
        </div>
            """

        patents_html = f"""
    <!-- Patents section header -->
    <div style="background:#534AB7;border-radius:12px 12px 0 0;
                padding:24px 28px;margin-top:32px;margin-bottom:0;">
      <p style="font-size:11px;color:#CCCAF8;margin:0 0 4px;
                 text-transform:uppercase;letter-spacing:0.08em;">
        Patent Alerts
      </p>
      <h2 style="font-size:20px;font-weight:600;color:#ffffff;margin:0 0 6px;">
        {len(patents)} New Patents This Week
      </h2>
    </div>
    <div style="background:#3C3489;padding:10px 28px;margin-bottom:20px;">
      <p style="font-size:12px;color:#CCCAF8;margin:0;">
        Monitored assignees and CPC codes · Summarised by Gemma AI · USPTO source
      </p>
    </div>
    {patent_cards}
        """

    # Full email HTML
    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0ede8;
             font-family:system-ui,-apple-system,sans-serif;">
  
  <div style="max-width:680px;margin:24px auto;padding:0 16px;">
    
    <!-- Header -->
    <div style="background:#1a1a18;border-radius:12px 12px 0 0;
                padding:24px 28px;margin-bottom:0;">
      <p style="font-size:11px;color:#888;margin:0 0 4px;
                 text-transform:uppercase;letter-spacing:0.08em;">
        Weekly Research Digest
      </p>
      <h1 style="font-size:20px;font-weight:600;color:#ffffff;margin:0 0 6px;">
        {count} New Papers This Week
      </h1>
      <p style="font-size:13px;color:#aaa;margin:0;">
        {today} &nbsp;·&nbsp; Topics: {topics_shown}
      </p>
    </div>
    
    <!-- Info bar -->
    <div style="background:#27500A;padding:10px 28px;margin-bottom:20px;
                border-radius:0 0 0 0;">
      <p style="font-size:12px;color:#b8dfa0;margin:0;">
        Automatically collected from Semantic Scholar and arXiv · 
        Summarised by Gemma AI · Sorted by importance
      </p>
    </div>
    
    <!-- Paper cards -->
    {cards_html}

    <!-- Patent cards -->
    {patents_html}
    
    <!-- Footer -->
    <div style="text-align:center;padding:20px;color:#aaa;font-size:11px;">
      <p style="margin:0 0 4px;">
        This digest is automatically generated every Monday.
      </p>
      <p style="margin:0;">
        To update search topics, edit <strong>taxonomy.yaml</strong> in the repository.
      </p>
    </div>
    
  </div>
</body>
</html>
"""
    return html


# ═══════════════════════════════════════════════════════════
# STEP 6 — Send email via Gmail SMTP
# ═══════════════════════════════════════════════════════════

def send_email(html_content: str, taxonomy: dict, config: dict):
    """
    Send the HTML digest email via SMTP.
    Supports both Outlook (office365) and Gmail automatically.
    
    Outlook: use your normal Outlook email + normal password
             (or App Password if your org requires MFA)
    Gmail:   use Gmail address + Gmail App Password
             (NOT your real Gmail password)
    """
    sender     = config["sender_email"]
    password   = config["sender_password"]
    recipients = config["recipient_emails"]
    prefix     = taxonomy["email"]["subject_prefix"]
    today      = datetime.now().strftime("%d %b %Y")
    subject    = f"{prefix} Research Digest — {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    # Auto-detect email provider from sender address
    sender_lower = sender.lower()

    if "gmail.com" in sender_lower:
        smtp_host = "smtp.gmail.com"
        smtp_port = 465
        use_ssl   = True
        log.info("Email provider: Gmail (SSL port 465)")

    elif any(x in sender_lower for x in ["outlook.com", "hotmail.com", "live.com"]):
        smtp_host = "smtp.office365.com"
        smtp_port = 587
        use_ssl   = False
        log.info("Email provider: Outlook personal (STARTTLS port 587)")

    else:
        # Company Outlook / Microsoft 365 — most common for R&D teams
        smtp_host = "smtp.office365.com"
        smtp_port = 587
        use_ssl   = False
        log.info("Email provider: Microsoft 365 / company Outlook (STARTTLS port 587)")

    try:
        if use_ssl:
            # Gmail — direct SSL connection
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                server.login(sender, password)
                server.sendmail(sender, recipients, msg.as_string())
        else:
            # Outlook — start unencrypted then upgrade to TLS
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(sender, password)
                server.sendmail(sender, recipients, msg.as_string())

        log.info(f"Email sent successfully to: {', '.join(recipients)}")

    except smtplib.SMTPAuthenticationError:
        log.error("Authentication failed — check your email and password")
        log.error("Outlook: use your normal Outlook password")
        log.error("Gmail: use App Password from myaccount.google.com, NOT your real password")
        fallback_path = f"digests/digest_{datetime.now().strftime('%Y%m%d')}.html"
        Path(fallback_path).write_text(html_content, encoding="utf-8")
        log.info(f"Digest saved locally as fallback: {fallback_path}")

    except Exception as e:
        log.error(f"Email sending failed: {e}")
        fallback_path = f"digest_{datetime.now().strftime('%Y%m%d')}.html"
        Path(fallback_path).write_text(html_content, encoding="utf-8")
        log.info(f"Digest saved locally as fallback: {fallback_path}")


# ═══════════════════════════════════════════════════════════
# MAIN — Orchestrates all steps
# ═══════════════════════════════════════════════════════════

def main():
    log.info("=" * 55)
    log.info("Research Digest Pipeline — starting")
    log.info("=" * 55)
    
    # ── Load config from environment variables ───────────────
    config = {
        "google_ai_key":    os.environ.get("GOOGLE_AI_KEY", ""),
        "groq_api_key":     os.environ.get("GROQ_API_KEY", ""),
        "serpapi_key":      os.environ.get("SERPAPI_KEY", ""),
        "sender_email":     os.environ.get("SENDER_EMAIL", ""),
        "sender_password":  os.environ.get("SENDER_PASSWORD", ""),
        "recipient_emails": os.environ.get("RECIPIENT_EMAILS", "").split(","),
    }

    # ── Step 1: Load taxonomy ────────────────────────────────
    taxonomy        = load_taxonomy()
    patent_taxonomy = load_patent_taxonomy()
    keywords        = get_all_keywords(taxonomy)
    days_back  = taxonomy.get("lookback_days", 7)
    max_res    = taxonomy.get("results_per_query", 25)
    max_digest = taxonomy["email"].get("max_papers_in_digest", 30)
    llm_config = taxonomy.get("llm", {})

    # Determine primary and fallback LLM provider and keys
    provider          = llm_config.get("provider", "groq")
    fallback_provider = llm_config.get("fallback_provider", None)
    active_llm_key    = config["groq_api_key"] if provider == "groq" else config["google_ai_key"]
    fallback_llm_key  = (
        config["google_ai_key"] if fallback_provider == "google"
        else config["groq_api_key"] if fallback_provider == "groq"
        else None
    )
    # Inject fallback key so extract functions can access it
    llm_config["_fallback_key"] = fallback_llm_key

    if not active_llm_key:
        log.warning(f"Primary LLM key not set for '{provider}' — extraction will be skipped")
    if fallback_provider and not fallback_llm_key:
        log.warning(f"Fallback LLM key not set for '{fallback_provider}' — no fallback available")
    
    log.info(f"Searching {len(keywords)} keywords, last {days_back} days")
    
    # ── Step 2: Search APIs ──────────────────────────────────
    all_papers = []
    
    # Group keywords into batches to avoid too many API calls
    # Use topic group names as search queries — more efficient
    search_queries = list(taxonomy["topics"].keys())
    fields_of_study = taxonomy.get("fields_of_study", [])   # restrict searches to these academic fields
    
    for query in search_queries:
        # Use group name + first keyword as search term
        group_keywords = taxonomy["topics"][query]
        search_term = group_keywords[0] if group_keywords else query
        
        if taxonomy["sources"].get("semantic_scholar", True):
            papers = search_semantic_scholar(search_term, days_back, max_res, fields_of_study)
            all_papers.extend(papers)
            time.sleep(3)  # 3 seconds — avoids Semantic Scholar 429 rate limit
        
        if taxonomy["sources"].get("arxiv", True):
            papers = search_arxiv(search_term, days_back, max_res)
            all_papers.extend(papers)
            time.sleep(5)  # 5 seconds — arXiv needs more breathing room

        if taxonomy["sources"].get("openalex", False):
            papers = search_openalex(search_term, days_back, max_res, fields_of_study)
            all_papers.extend(papers)
            time.sleep(2)  # 2 seconds — polite pool allows 10 req/s, but be courteous
    
    log.info(f"Total papers fetched: {len(all_papers)}")
    
    # ── Step 3: Filter and deduplicate ───────────────────────
    filtered_papers = filter_and_deduplicate(all_papers, taxonomy)
    
    # Limit to max digest size
    filtered_papers = filtered_papers[:max_digest]
    log.info(f"Papers for this digest: {len(filtered_papers)}")
    
    if not filtered_papers:
        log.info("No new relevant papers this week — continuing to check patents")

    # ── Step 4: Extract and summarise with LLM ───────────────
    if active_llm_key:
        log.info(f"Running LLM extraction via {provider.upper()}...")
        for paper in filtered_papers:
            paper = extract_paper_data(paper, taxonomy, active_llm_key, llm_config)
    else:
        log.info(f"Skipping LLM — no API key set for provider '{provider}'")
        for paper in filtered_papers:
            paper["extraction"] = {
                "plain_summary":    paper["abstract"][:400],
                "key_contribution": "See abstract",
                "methods_used":     [],
                "relevance_note":   f"Add {provider.upper()}_API_KEY to enable AI summaries",
                "importance_rating": 2,
            }

    # ── Step 4b: Patent pipeline ─────────────────────────────
    all_patents = []
    if patent_taxonomy.get("enabled", False):
        log.info("Searching patents via SerpAPI Google Patents...")
        raw_patents  = search_serp_patents(patent_taxonomy, config["serpapi_key"])
        new_patents  = filter_patents(raw_patents, patent_taxonomy)
        max_patents  = patent_taxonomy.get("max_patents_in_digest", 20)
        new_patents  = new_patents[:max_patents]
        log.info(f"Patents for this digest: {len(new_patents)}")

        if new_patents and active_llm_key:
            log.info("Running patent LLM extraction...")
            for patent in new_patents:
                patent = extract_patent_data(patent, active_llm_key, llm_config)
        all_patents = new_patents
    else:
        log.info("Patent search disabled in patent_taxonomy.yaml — skipping")

    # ── Step 5: Build HTML email ─────────────────────────────
    if not filtered_papers and not all_patents:
        log.info("No papers and no patents found this week — no email sent.")
        return

    html_digest = build_html_email(filtered_papers, taxonomy, all_patents)
    
    # Always save a local copy for inspection / debugging
    Path("digests").mkdir(exist_ok=True)
    local_file = f"digests/digest_{datetime.now().strftime('%Y%m%d')}.html"
    Path(local_file).write_text(html_digest, encoding="utf-8")
    log.info(f"Digest saved locally: {local_file}")
    
    # ── Step 6: Send email ───────────────────────────────────
    if config["sender_email"] and config["sender_password"] and config["recipient_emails"][0]:
        send_email(html_digest, taxonomy, config)
    else:
        log.info("Email credentials not set — digest saved locally only")
        log.info("Open the HTML file in a browser to preview the digest")
    
    log.info("=" * 55)
    log.info(f"Pipeline complete — {len(filtered_papers)} papers · {len(all_patents)} patents")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
