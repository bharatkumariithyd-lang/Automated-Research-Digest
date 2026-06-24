"""
robust_json_parse.py
====================
Tolerant JSON parser for LLM responses.

LLMs frequently wrap their JSON in markdown fences (```json ... ```), add a
sentence of prose before or after it, or emit minor syntax slips like trailing
commas. A bare json.loads() throws on all of these and the structured summary
is lost. parse_llm_json_with_fallback() tries a series of increasingly tolerant
strategies and, if every one fails, returns a caller-supplied fallback dict so
the pipeline never crashes.

Used by pipeline.py — extract_paper_data() and extract_patent_data().
"""

import json
import re


def _strip_code_fences(text: str) -> str:
    """Return the contents of the first ```json ... ``` / ``` ... ``` block,
    or the original text (trimmed) if there are no fences."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()


def _extract_object(text: str) -> str | None:
    """Return the substring from the first '{' to its matching '}'.
    Walks the string tracking brace depth (ignoring braces inside strings)
    so nested objects and prose around the JSON are handled correctly."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None  # Unbalanced braces — no complete object found


def _remove_trailing_commas(text: str) -> str:
    """Strip trailing commas before a closing } or ] — invalid in strict JSON
    but a very common LLM slip."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _try_loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def parse_llm_json_with_fallback(raw_text: str, fallback: dict) -> tuple[dict, bool]:
    """
    Parse a JSON object out of an LLM response, tolerating common defects.

    Strategies, in order:
      1. json.loads on the raw text (the happy path)
      2. json.loads after stripping markdown code fences
      3. json.loads on the first balanced {...} object found in the text
      4. json.loads on that object after removing trailing commas

    Args:
        raw_text:  the raw string returned by the LLM.
        fallback:  dict returned (with any successfully parsed fields overlaid)
                   when parsing fails or yields a non-dict. Should contain every
                   field the downstream code / email template expects.

    Returns:
        (data, ok) where:
          - data is always a dict containing every key from `fallback`, with
            parsed values overlaid on top when parsing succeeds, and
          - ok is True only when real JSON was recovered.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        return dict(fallback), False

    stripped = _strip_code_fences(raw_text)

    # Build the list of candidate strings to attempt, cheapest first.
    candidates = [raw_text.strip(), stripped]
    extracted = _extract_object(stripped) or _extract_object(raw_text)
    if extracted:
        candidates.append(extracted)
        candidates.append(_remove_trailing_commas(extracted))

    for candidate in candidates:
        parsed = _try_loads(candidate)
        if isinstance(parsed, dict):
            # Overlay parsed fields onto the fallback so every expected key
            # is guaranteed present even if the model omitted some.
            merged = dict(fallback)
            merged.update(parsed)
            return merged, True

    return dict(fallback), False
