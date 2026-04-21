from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

log = logging.getLogger(__name__)

VALIDATE_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """You are a patent claims expert specializing in biopharmaceutical antibody patents.
Determine whether a patent directly claims an antibody that binds a specific target antigen.

Rules:
- "validates=true" only when the patent explicitly covers an antibody (monoclonal, humanized,
  bispecific, fragment, nanobody, etc.) that directly binds the target
- "validates=false" if the target appears only in background, prior art, or comparative examples
- "validates=false" if the antibody targets a downstream effector rather than the target itself
- "validates=false" if the patent is about the target protein/gene but not an antibody against it"""

_USER_PROMPT = """Patent: {patent_number}
Title: {title}
Abstract: {abstract}

Does this patent claim an antibody that DIRECTLY TARGETS AND BINDS {target}?

Return ONLY this JSON (no other text):
{{
  "validates": true or false,
  "confidence": 0.0 to 1.0,
  "reason": "one sentence"
}}"""


class CreditExhaustedError(Exception):
    pass


def validate_from_abstract(
    client: anthropic.Anthropic,
    patent_number: str,
    target: str,
    title: str,
    abstract: str,
) -> dict:
    abstract_snippet = abstract[:600] if abstract else "(no abstract available)"
    messages = [{
        "role": "user",
        "content": _USER_PROMPT.format(
            patent_number=patent_number,
            title=title or "(no title)",
            abstract=abstract_snippet,
            target=target,
        ),
    }]
    system = [{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

    try:
        resp = client.messages.create(
            model=VALIDATE_MODEL,
            max_tokens=256,
            system=system,
            messages=messages,
        )
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise CreditExhaustedError(str(e)) from e
        log.error(f"  {patent_number}: API error: {e}")
        return _empty(patent_number, target, error=str(e))
    except Exception as e:
        log.error(f"  {patent_number}: unexpected error: {e}")
        return _empty(patent_number, target, error=str(e))

    text = resp.content[0].text if resp.content else ""
    try:
        # Strip markdown fences if present
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        result = json.loads(clean)
    except Exception:
        log.warning(f"  {patent_number}: could not parse JSON: {text!r}")
        return _empty(patent_number, target, error="parse failed")

    return {
        "patent_number":    patent_number,
        "target":           target,
        "validates":        bool(result.get("validates", False)),
        "confidence":       float(result.get("confidence", 0.0)),
        "reason":           result.get("reason", ""),
    }


def validate_all_with_abstract(
    client: anthropic.Anthropic,
    rows: list[dict],
    target: str,
    workers: int = 10,
    min_confidence: float = 0.5,
) -> list[dict]:
    """Score all rows using title+abstract. Returns ALL rows with updated validates/confidence/reason."""
    log.info(f"Scoring {len(rows)} patents with Claude {VALIDATE_MODEL} ({workers} workers)")

    stop_event = threading.Event()
    results: dict[str, dict] = {}
    results_lock = threading.Lock()

    def score_one(row: dict) -> None:
        if stop_event.is_set():
            return
        pn = row["patent_number"]
        try:
            score = validate_from_abstract(
                client,
                patent_number=pn,
                target=target,
                title=row.get("title", ""),
                abstract=row.get("abstract_snippet", ""),
            )
        except CreditExhaustedError as e:
            log.error(f"Credit balance exhausted at {pn} — stopping")
            stop_event.set()
            score = _empty(pn, target, error="credit exhausted")

        merged = {**row, **{k: score[k] for k in ("validates", "confidence", "reason") if k in score}}
        with results_lock:
            results[pn] = merged

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(score_one, r): r["patent_number"] for r in rows}
        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 50 == 0 or done == len(rows):
                passed = sum(1 for v in results.values() if v.get("validates"))
                log.info(f"  Progress: {done}/{len(rows)} scored, {passed} relevant so far")
            exc = future.exception()
            if exc:
                log.error(f"  Unexpected thread error: {exc}")

    # Preserve original order
    ordered = [results.get(r["patent_number"], r) for r in rows]
    passed = sum(1 for r in ordered if r.get("validates") and (r.get("confidence") or 0) >= min_confidence)
    log.info(f"Scoring complete: {passed}/{len(ordered)} patents passed (confidence >= {min_confidence})")
    return ordered


def _empty(patent_number: str, target: str, error: str = "") -> dict:
    return {
        "patent_number": patent_number,
        "target":        target,
        "validates":     False,
        "confidence":    0.0,
        "reason":        f"error: {error}",
    }
