"""
step1_search_patents.py
───────────────────────
Searches Google Patents for antibody patents against a target antigen,
then validates each candidate using Claude with extended thinking.

Pipeline:
  Phase A — Claude + web_search finds candidate patent numbers
  Phase B — Filter to US patents only, deduplicate
  Phase C — Claude + optional extended thinking validates each candidate

Usage:
    # Full run: search + validate
    python step1_search_patents.py --target "PD-1" --output-dir ./outputs

    # Rerun validation only on existing candidates CSV (after manual inspection)
    python step1_search_patents.py --target "PD-1" --output-dir ./outputs --revalidate

    # Rerun validation with extended thinking (slower, more thorough)
    python step1_search_patents.py --target "PD-1" --output-dir ./outputs --revalidate --thinking

    # Skip search, validate a known list of patent numbers
    python step1_search_patents.py --target "PD-1" --output-dir ./outputs \
        --patents US20220056133A1,US11234567B2

    # Lower confidence threshold to cast a wider net
    python step1_search_patents.py --target "PD-1" --output-dir ./outputs --min-confidence 0.6

Output files:
    {target}_candidates.csv   — all found patents with validation results
    {target}_validated.csv    — validated=True only, ready for Step 2

As module (in main runner):
    from step1_search_patents import run_step1
    validated = run_step1(target="PD-1", output_dir="./outputs")
    # validated is a list of dicts with patent_number, title, assignee, etc.

Requirements:
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY in .env
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import anthropic

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Exponential backoff for 429 rate limits
RETRY_BASE    = 60   # seconds — first wait
RETRY_MAX     = 300  # seconds — cap per wait
RETRY_LIMIT   = 5    # give up after this many consecutive 429s

# US patent number patterns to keep
# Matches: US20220056133A1, US11234567B2, US10987654B1, US9876543B2
US_PATENT_RE = re.compile(r"\bUS\d{7,11}[AB][12]\b")

# Patterns to explicitly reject
REJECT_PREFIXES = ("EP", "WO", "CN", "RU", "JP", "KR", "CA", "AU")

# Provisional applications (US + 11 digits + P)
PROVISIONAL_RE = re.compile(r"\bUS\d+P\b")

# CSV columns — order matters for readability
CSV_FIELDS = [
    "patent_number",
    "target",
    "title",
    "assignee",
    "year",
    "validates",
    "confidence",
    "reason",
    "abstract_snippet",
]


# ─────────────────────────────────────────────
# Phase A — Search with web_search tool
# ─────────────────────────────────────────────

SEARCH_SYSTEM_PROMPT = """You are a patent search expert specializing in biopharmaceuticals,
specifically antibody patents. You know how to search Google Patents effectively and how to
identify patents that genuinely claim antibodies against a specific target antigen.

When searching, use multiple queries to maximize coverage:
- Search by target name and synonyms
- Search by common assignees (pharma companies)
- Search specifically for US patents

Always return patent numbers in standard USPTO format: US followed by digits followed by
A1, A2, B1, or B2 (e.g. US20220056133A1, US11234567B2).
"""

SEARCH_USER_PROMPT = """Search Google Patents for US patents claiming antibodies that 
DIRECTLY BIND and TARGET {target}.

Search strategy — run multiple searches:
1. Search: "antibody {target} patent" site:patents.google.com
2. Search: "{target} monoclonal antibody US patent claims"
3. Search: "anti-{target} antibody USPTO" 
4. If {target} has known synonyms or alternative names, search those too

For each result you find:
- Extract the patent number (US format only: USXXXXXXXB2, US2022XXXXXXXА1, etc.)
- Extract the title if visible
- Extract the assignee/applicant if visible
- Note the year

STRICT FILTERING — only include patents where:
✓ The patent is a US patent (starts with US, ends with A1/A2/B1/B2)
✓ The patent appears to claim an antibody that BINDS {target}
✗ Skip: EP, WO, CN, RU, JP patents
✗ Skip: patents that only mention {target} in background/prior art
✗ Skip: provisional applications

Return your findings as a JSON array. Each element:
{{
  "patent_number": "US20220056133A1",
  "title": "...",
  "assignee": "...",
  "year": 2022,
  "search_confidence": 0.8,
  "notes": "why you think this is an anti-{target} antibody patent"
}}

Return ONLY the JSON array, no other text."""


def search_patents(client: anthropic.Anthropic, target: str) -> list[dict]:
    """
    Phase A: Use Claude with web_search to find candidate patent numbers.

    Claude autonomously runs multiple searches, reads results, and returns
    a structured list of candidates.

    Note on web_search tool: this is a hosted Anthropic tool. Claude calls it,
    Anthropic executes the search, results are returned automatically in the
    response. We do not handle tool results manually — just keep looping until
    stop_reason == "end_turn".
    """
    log.info(f"Phase A: Searching Google Patents for anti-{target} antibody patents...")

    tools = [{"type": "web_search_20250305", "name": "web_search"}]

    messages = [{
        "role": "user",
        "content": SEARCH_USER_PROMPT.format(target=target)
    }]

    # Run the tool loop — Claude may call web_search multiple times
    final_text = _run_tool_loop(client, messages, tools, system=SEARCH_SYSTEM_PROMPT)

    if not final_text:
        log.error("Search returned empty response")
        return []

    candidates = _parse_json_response(final_text, context="search")
    log.info(f"Phase A: Found {len(candidates)} raw candidates")
    return candidates if isinstance(candidates, list) else []


def _run_tool_loop(
    client: anthropic.Anthropic,
    messages: list[dict],
    tools: list[dict],
    system: str = "",
    max_iterations: int = 20,
) -> str:
    """
    Run Claude in a tool-use loop until stop_reason == "end_turn".

    For web_search_20250305 (hosted tool): Anthropic executes the search
    automatically. We just append Claude's response to messages and loop.
    No manual tool_result construction needed.

    Returns the final text output from Claude.
    """
    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        tools=tools,
        messages=messages,
    )
    if system:
        kwargs["system"] = system

    rl_count = 0
    for iteration in range(max_iterations):
        try:
            response = client.messages.create(**kwargs)
            rl_count = 0
        except anthropic.RateLimitError:
            rl_count += 1
            if rl_count > RETRY_LIMIT:
                log.error("Rate limited too many times in a row — giving up")
                raise
            wait = min(RETRY_BASE * (2 ** (rl_count - 1)), RETRY_MAX)
            log.warning(f"Rate limited — waiting {wait}s before retry ({rl_count}/{RETRY_LIMIT})")
            time.sleep(wait)
            continue
        except anthropic.APIError as e:
            log.error(f"API error: {e}")
            raise

        log.info(
            f"  Loop iteration {iteration + 1}: "
            f"stop_reason={response.stop_reason}, "
            f"blocks={[b.type for b in response.content]}"
        )

        if response.stop_reason == "end_turn":
            # Extract all text blocks and join
            return " ".join(
                block.text for block in response.content
                if block.type == "text"
            )

        if response.stop_reason == "tool_use":
            # Append Claude's full response (includes tool_use + tool_result blocks)
            # For hosted tools like web_search, results are already in response.content
            # We just append and loop — Claude will read its own results next iteration
            kwargs["messages"] = kwargs["messages"] + [
                {"role": "assistant", "content": response.content}
            ]
            continue

        # Unexpected stop reason
        log.warning(f"Unexpected stop_reason: {response.stop_reason}")
        break

    log.warning("Tool loop reached max iterations without end_turn")
    return ""


# ─────────────────────────────────────────────
# Phase B — Filter to US patents only
# ─────────────────────────────────────────────

def filter_us_patents(candidates: list[dict]) -> list[dict]:
    """
    Keep only genuine US USPTO patents.
    - Must match US\d+[AB][12] pattern
    - Must not be provisional (US...P)
    - Must not start with EP, WO, CN, etc.
    - Deduplicate by patent_number
    """
    seen    = set()
    kept    = []
    dropped = []

    for c in candidates:
        number = (c.get("patent_number") or "").strip()

        # Extract a clean patent number if embedded in text
        match = US_PATENT_RE.search(number)
        if match:
            number = match.group(0)
            c["patent_number"] = number
        
        # Reject non-US prefixes
        if any(number.startswith(p) for p in REJECT_PREFIXES):
            dropped.append((number, "non-US jurisdiction"))
            continue

        # Reject provisionals
        if PROVISIONAL_RE.match(number):
            dropped.append((number, "provisional application"))
            continue

        # Reject if doesn't match US patent format
        if not US_PATENT_RE.match(number):
            dropped.append((number, f"invalid format: '{number}'"))
            continue

        # Deduplicate
        if number in seen:
            dropped.append((number, "duplicate"))
            continue

        seen.add(number)
        kept.append(c)

    if dropped:
        log.info(f"Phase B: Dropped {len(dropped)} candidates:")
        for num, reason in dropped:
            log.info(f"  {num}: {reason}")

    log.info(f"Phase B: {len(kept)} US patents after filtering")
    return kept


# ─────────────────────────────────────────────
# Phase C — Validate each candidate
# ─────────────────────────────────────────────

VALIDATE_SYSTEM_PROMPT = """You are a patent claims expert specializing in biopharmaceutical 
antibody patents. You can precisely determine whether a patent's primary claims cover an 
antibody that directly binds a specific target antigen.

You are strict and precise:
- "validates=true" only when the patent's independent claims explicitly cover an antibody
  (monoclonal, humanized, bispecific, fragment, etc.) that directly binds the target
- "validates=false" if the target is only in background, prior art, or comparative examples
- "validates=false" if the antibody targets a downstream effector rather than the target itself
- You look at claims first, then abstract, then description"""

VALIDATE_USER_PROMPT = """Fetch the abstract and claims of patent {patent_number} from 
Google Patents: https://patents.google.com/patent/{patent_number}

Then answer: Does this patent claim an antibody that DIRECTLY TARGETS AND BINDS {target}?

Be strict:
- "validates=true" ONLY if the patent's primary independent claim covers an 
  anti-{target} antibody (including fragments: Fab, scFv, nanobody, etc.)
- "validates=false" if {target} is only mentioned in background or prior art
- "validates=false" if the antibody targets something that interacts WITH {target}
  but does not itself bind {target}
- "validates=false" if the patent is about {target} protein/gene but not an antibody against it

Return ONLY this JSON (no other text):
{{
  "validates": true or false,
  "confidence": 0.0 to 1.0,
  "reason": "one sentence explaining your decision",
  "title": "patent title if found",
  "assignee": "assignee/applicant if found",
  "year": year as integer or null,
  "abstract_snippet": "first 200 chars of abstract if found"
}}"""


def validate_patent(
    client: anthropic.Anthropic,
    patent_number: str,
    target: str,
    use_thinking: bool = False,
    thinking_budget: int = 8000,
) -> dict:
    """
    Phase C: Validate a single patent using Claude.

    Claude fetches the patent from Google Patents via web_search,
    reads the claims/abstract, and decides if it's a genuine anti-target antibody.

    use_thinking=True enables extended thinking — slower but more thorough.
    Recommended when revalidating after manual inspection of marginal cases.
    """
    tools = [{"type": "web_search_20250305", "name": "web_search"}]

    messages = [{
        "role": "user",
        "content": VALIDATE_USER_PROMPT.format(
            patent_number=patent_number,
            target=target,
        )
    }]

    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=tools,
        system=VALIDATE_SYSTEM_PROMPT,
        messages=messages,
    )

    if use_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        kwargs["max_tokens"] = thinking_budget + 4096

    log.info(f"  Validating {patent_number} (thinking={'on' if use_thinking else 'off'})")

    try:
        final_text = _run_tool_loop(client, messages, tools, system=VALIDATE_SYSTEM_PROMPT)
    except Exception as e:
        log.error(f"  Validation failed for {patent_number}: {e}")
        return _empty_validation(patent_number, target, error=str(e))

    result = _parse_json_response(final_text, context=f"validate {patent_number}")
    if not result or not isinstance(result, dict):
        log.warning(f"  Could not parse validation response for {patent_number}")
        return _empty_validation(patent_number, target, error="parse failed")

    result["patent_number"] = patent_number
    result["target"]        = target

    log.info(
        f"  {patent_number}: validates={result.get('validates')}, "
        f"confidence={result.get('confidence')}"
    )
    return result


def validate_all(
    client: anthropic.Anthropic,
    candidates: list[dict],
    target: str,
    use_thinking: bool = False,
    thinking_budget: int = 8000,
    delay_between: float = 30.0,
) -> list[dict]:
    """
    Validate all candidates. Returns list of result dicts (all candidates, not just valid ones).
    """
    log.info(
        f"Phase C: Validating {len(candidates)} candidates "
        f"(thinking={'on' if use_thinking else 'off'})"
    )
    results = []

    for i, candidate in enumerate(candidates, 1):
        patent_number = candidate.get("patent_number", "")
        log.info(f"[{i}/{len(candidates)}] {patent_number}")

        result = validate_patent(
            client=client,
            patent_number=patent_number,
            target=target,
            use_thinking=use_thinking,
            thinking_budget=thinking_budget,
        )

        # Merge search-phase metadata with validation result
        merged = {
            "patent_number":   patent_number,
            "target":          target,
            "title":           result.get("title") or candidate.get("title", ""),
            "assignee":        result.get("assignee") or candidate.get("assignee", ""),
            "year":            result.get("year") or candidate.get("year"),
            "validates":       result.get("validates", False),
            "confidence":      result.get("confidence", 0.0),
            "reason":          result.get("reason", ""),
            "abstract_snippet": result.get("abstract_snippet", ""),
        }
        results.append(merged)

        # Polite delay between API calls
        if i < len(candidates):
            time.sleep(delay_between)

    passed = sum(1 for r in results if r.get("validates"))
    log.info(f"Phase C: {passed}/{len(results)} patents validated")
    return results


# ─────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────

def save_csv(records: list[dict], path: Path) -> None:
    """Save records to CSV. Always CSV — easier to inspect than JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    log.info(f"Saved: {path} ({len(records)} rows)")


def load_csv(path: Path) -> list[dict]:
    """Load CSV back to list of dicts."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def print_summary(results: list[dict], min_confidence: float) -> None:
    validated = [r for r in results if r.get("validates") in (True, "True") and
                 float(r.get("confidence", 0)) >= min_confidence]
    rejected  = [r for r in results if r not in validated]

    print(f"\n{'─'*72}")
    print(f"VALIDATED ({len(validated)} patents, confidence ≥ {min_confidence})")
    print(f"{'─'*72}")
    for r in validated:
        print(f"  {r['patent_number']:20}  conf={float(r.get('confidence',0)):.2f}  "
              f"{r.get('assignee',''):<25}  {r.get('title','')[:40]}")

    print(f"\n{'─'*72}")
    print(f"REJECTED ({len(rejected)} patents)")
    print(f"{'─'*72}")
    for r in rejected:
        print(f"  {r['patent_number']:20}  conf={float(r.get('confidence',0)):.2f}  "
              f"{r.get('reason','')[:55]}")
    print()


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _parse_json_response(text: str, context: str = "") -> dict | list | None:
    """Parse JSON from Claude response. Handles markdown fences."""
    text = text.strip()

    # Strip markdown fences
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.startswith("```")
        ).strip()

    # Find first JSON structure
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        end   = text.rfind(end_char)
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue

    log.warning(f"Could not parse JSON from response ({context}): {text[:200]}")
    return None


def _empty_validation(patent_number: str, target: str, error: str = "") -> dict:
    return {
        "patent_number":    patent_number,
        "target":           target,
        "title":            "",
        "assignee":         "",
        "year":             None,
        "validates":        False,
        "confidence":       0.0,
        "reason":           f"validation error: {error}",
        "abstract_snippet": "",
    }


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

def run_step1(
    target: str,
    output_dir: str | Path = ".",
    known_patents: list[str] | None = None,
    min_confidence: float = 0.7,
    use_thinking: bool = False,
    thinking_budget: int = 8000,
    revalidate: bool = False,
) -> list[dict]:
    """
    Full Step 1 pipeline.

    Args:
        target:          Antigen name, e.g. "PD-1", "TRBV3"
        output_dir:      Where to write CSVs
        known_patents:   If provided, skip search and validate these directly
        min_confidence:  Minimum confidence to include in validated output
        use_thinking:    Use extended thinking for validation (slower, more thorough)
        thinking_budget: Token budget for thinking
        revalidate:      If True, load existing candidates CSV and rerun validation only

    Returns:
        List of validated patent dicts (validates=True, confidence >= min_confidence)
        ready to be ingested by Step 2
    """
    client     = anthropic.Anthropic(max_retries=0)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    target_slug      = target.replace(" ", "_").replace("-", "").lower()
    candidates_path  = output_dir / f"{target_slug}_candidates.csv"
    validated_path   = output_dir / f"{target_slug}_validated.csv"

    # ── Phase A+B: Search (or load known/existing candidates) ────────────────
    if revalidate and candidates_path.exists():
        log.info(f"--revalidate: loading existing candidates from {candidates_path}")
        candidates = load_csv(candidates_path)
        log.info(f"Loaded {len(candidates)} candidates for revalidation")

    elif known_patents:
        log.info(f"Using {len(known_patents)} provided patent numbers — skipping search")
        candidates = [{"patent_number": p} for p in known_patents]
        candidates = filter_us_patents(candidates)

    else:
        # Phase A: search
        raw_candidates = search_patents(client, target)
        # Phase B: filter to US only
        candidates = filter_us_patents(raw_candidates)
        # Save candidates before validation (so you can inspect/edit if needed)
        save_csv(candidates, candidates_path)
        log.info(f"Candidates saved to {candidates_path} — edit if needed before validation")

    if not candidates:
        log.error("No candidates to validate")
        return []

    # ── Phase C: Validate ─────────────────────────────────────────────────────
    results = validate_all(
        client=client,
        candidates=candidates,
        target=target,
        use_thinking=use_thinking,
        thinking_budget=thinking_budget,
    )

    # Save all results (including rejected) for inspection
    save_csv(results, candidates_path)

    # Save validated-only for Step 2
    validated = [
        r for r in results
        if r.get("validates") in (True, "True")
        and float(r.get("confidence", 0)) >= min_confidence
    ]
    save_csv(validated, validated_path)

    print_summary(results, min_confidence)
    log.info(
        f"Step 1 done: {len(validated)} validated patents written to {validated_path}"
    )
    return validated


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 1: Search Google Patents for antibody patents against a target"
    )
    parser.add_argument(
        "--target", required=True,
        help="Antigen target name, e.g. 'PD-1' or 'TRBV3'"
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="Directory for output CSVs (default: current dir)"
    )
    parser.add_argument(
        "--patents", default=None,
        help="Comma-separated list of known patent numbers — skips search phase. "
             "e.g. US20220056133A1,US11234567B2"
    )
    parser.add_argument(
        "--revalidate", action="store_true",
        help="Load existing {target}_candidates.csv and rerun validation only. "
             "Use after manually inspecting/editing the candidates CSV."
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="Use extended thinking for validation (slower, more thorough). "
             "Recommended when revalidating marginal cases."
    )
    parser.add_argument(
        "--thinking-budget", type=int, default=8000,
        help="Token budget for extended thinking (default: 8000)"
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.7,
        help="Minimum confidence score to include in validated output (default: 0.7)"
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    known = [p.strip() for p in args.patents.split(",")] if args.patents else None

    validated = run_step1(
        target=args.target,
        output_dir=args.output_dir,
        known_patents=known,
        min_confidence=args.min_confidence,
        use_thinking=args.thinking,
        thinking_budget=args.thinking_budget,
        revalidate=args.revalidate,
    )

    sys.exit(0 if validated else 1)


if __name__ == "__main__":
    main()
