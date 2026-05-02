from __future__ import annotations

import logging
import time

import anthropic

from .io import _parse_json_response
from .search import _run_tool_loop

log = logging.getLogger(__name__)

VALIDATE_MODEL = "claude-haiku-4-5-20251001"

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

Use at most 2 web searches. Fetching the Google Patents page directly is usually enough.

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


class CreditExhaustedError(Exception):
    """Raised when the Anthropic account has insufficient credits."""


def validate_patent(
    client: anthropic.Anthropic,
    patent_number: str,
    target: str,
) -> dict:
    """
    Phase C: Validate a single patent using Claude Haiku + web_search.

    Raises CreditExhaustedError if the account has no credits left.
    """
    tools = [{"type": "web_search_20250305", "name": "web_search"}]

    messages = [{
        "role": "user",
        "content": VALIDATE_USER_PROMPT.format(
            patent_number=patent_number,
            target=target,
        )
    }]

    system = [{"type": "text", "text": VALIDATE_SYSTEM_PROMPT,
               "cache_control": {"type": "ephemeral"}}]

    log.info(f"  Validating {patent_number} (model={VALIDATE_MODEL})")

    try:
        final_text = _run_tool_loop(
            client, messages, tools,
            system=system,
            model=VALIDATE_MODEL,
            max_tokens=1024,
        )
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise CreditExhaustedError(str(e)) from e
        log.error(f"  Validation failed for {patent_number}: {e}")
        return _empty_validation(patent_number, target, error=str(e))
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
    delay_between: float = 30.0,
) -> list[dict]:
    """Validate all candidates. Returns list of result dicts (all candidates, not just valid ones)."""
    log.info(f"Phase C: Validating {len(candidates)} candidates (model={VALIDATE_MODEL})")
    results = []

    for i, candidate in enumerate(candidates, 1):
        patent_number = candidate.get("patent_number", "")
        log.info(f"[{i}/{len(candidates)}] {patent_number}")

        try:
            result = validate_patent(
                client=client,
                patent_number=patent_number,
                target=target,
            )
        except CreditExhaustedError:
            log.error("Credit balance exhausted — stopping validation early")
            remaining = [c.get("patent_number", "") for c in candidates[i:]]
            if remaining:
                log.warning(f"Skipped (no credits): {remaining}")
            break

        merged = {
            "patent_number":    patent_number,
            "target":           target,
            "title":            result.get("title") or candidate.get("title", ""),
            "assignee":         result.get("assignee") or candidate.get("assignee", ""),
            "year":             result.get("year") or candidate.get("year"),
            "validates":        result.get("validates", False),
            "confidence":       result.get("confidence", 0.0),
            "reason":           result.get("reason", ""),
            "abstract_snippet": result.get("abstract_snippet", ""),
        }
        results.append(merged)

        if i < len(candidates):
            time.sleep(delay_between)

    passed = sum(1 for r in results if r.get("validates"))
    log.info(f"Phase C: {passed}/{len(results)} patents validated")
    return results


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
