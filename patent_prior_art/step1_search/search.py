from __future__ import annotations

import logging
import re
import time

import anthropic

from .io import _parse_json_response

log = logging.getLogger(__name__)

# Exponential backoff for 429 rate limits
RETRY_BASE    = 60   # seconds — first wait
RETRY_MAX     = 300  # seconds — cap per wait
RETRY_LIMIT   = 5    # give up after this many consecutive 429s

US_PATENT_RE    = re.compile(r"\bUS\d{7,11}[AB][12]\b")
REJECT_PREFIXES = ("EP", "WO", "CN", "RU", "JP", "KR", "CA", "AU")
PROVISIONAL_RE  = re.compile(r"\bUS\d+P\b")

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

    Note on web_search tool: this is a hosted Anthropic tool. Claude calls it,
    Anthropic executes the search, results are returned automatically in the
    response. We do not handle tool results manually — just keep looping until
    stop_reason == "end_turn".
    """
    log.info(f"Phase A: Searching Google Patents for anti-{target} antibody patents...")

    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    messages = [{"role": "user", "content": SEARCH_USER_PROMPT.format(target=target)}]

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
    system: str | list = "",
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 8096,
    max_iterations: int = 30,
) -> str:
    """
    Run Claude in a tool-use loop until stop_reason == "end_turn".

    For web_search_20250305 (hosted tool): Anthropic executes the search
    automatically. We just append Claude's response to messages and loop.
    No manual tool_result construction needed.
    """
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        tools=tools,
        messages=messages,
    )
    if system:
        kwargs["system"] = system

    rl_count = 0
    last_text = ""
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

        candidate_text = " ".join(
            block.text for block in response.content
            if block.type == "text"
        )
        if candidate_text:
            last_text = candidate_text

        if response.stop_reason == "end_turn":
            return last_text

        if response.stop_reason in ("tool_use", "pause_turn"):
            kwargs["messages"] = kwargs["messages"] + [
                {"role": "assistant", "content": response.content}
            ]
            continue

        log.warning(f"Unexpected stop_reason: {response.stop_reason}")
        break

    log.warning("Tool loop reached max iterations without end_turn")
    return last_text


def filter_us_patents(candidates: list[dict]) -> list[dict]:
    """
    Keep only genuine US USPTO patents.
    - Must match US\\d+[AB][12] pattern
    - Must not be provisional (US...P)
    - Must not start with EP, WO, CN, etc.
    - Deduplicate by patent_number
    """
    seen    = set()
    kept    = []
    dropped = []

    for c in candidates:
        number = (c.get("patent_number") or "").strip()

        match = US_PATENT_RE.search(number)
        if match:
            number = match.group(0)
            c["patent_number"] = number

        if any(number.startswith(p) for p in REJECT_PREFIXES):
            dropped.append((number, "non-US jurisdiction"))
            continue

        if PROVISIONAL_RE.match(number):
            dropped.append((number, "provisional application"))
            continue

        if not US_PATENT_RE.match(number):
            dropped.append((number, f"invalid format: '{number}'"))
            continue

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
