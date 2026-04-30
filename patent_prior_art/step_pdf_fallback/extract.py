from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a bioinformatics expert specializing in antibody patent analysis.
Your task is to read a patent PDF's sequence listing and extract every FULL-LENGTH
antibody sequence (VH, VL, full heavy chain, full light chain).

Rules:
- Only extract sequences that are explicitly written out as amino acid or nucleotide strings.
- Skip individual CDR fragments (6–20 aa) — those are handled by a different extraction step.
- Do NOT invent sequences; do NOT guess when only a SEQ ID NO is mentioned without the string.
- Normalize: strip whitespace, line numbers, and position markers from the extracted sequence.
"""

USER_PROMPT_TEMPLATE = """Patent number: {patent_number}

Extract every full-length VH, VL, full heavy chain, and full light chain sequence from
this patent's sequence listing section.

For each sequence, produce ONE JSON object with EXACTLY these fields:
  "seq_id":        integer — the patent's SEQ ID NO
  "molecule_type": "AA" or "NT"
  "sequence":      string — single-letter amino-acid or nucleotide string, no spaces/numbers
  "location":      one of "VH", "VL", "VK", "full_heavy_chain", "full_light_chain", "other"
  "organism":      string — e.g. "Homo sapiens", "synthetic"; empty string if unknown

Output format: a valid JSON array only. Start with [ end with ]. No prose, no markdown."""


def extract_sequences(
    client: anthropic.Anthropic,
    pdf_path: Path,
    patent_number: str,
    model: str = MODEL,
) -> list[dict]:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")
    log.info(f"PDF size: {pdf_path.stat().st_size / 1024:.0f} KB")

    content: list[dict] = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_b64,
            },
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": USER_PROMPT_TEMPLATE.format(patent_number=patent_number),
        },
    ]

    for attempt in range(1, 3):
        log.info(f"Calling {model}, attempt {attempt}/2")
        try:
            with client.messages.stream(
                model=model,
                max_tokens=32000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                response = stream.get_final_message()
        except anthropic.APIError as e:
            log.error(f"API error: {e}")
            if attempt == 2:
                raise
            time.sleep(5)
            continue

        _log_usage(response.usage, model)

        raw = next((b.text for b in response.content if b.type == "text"), "")
        if not raw.strip():
            log.warning("Empty response")
            continue

        seqs, ok = _parse_json(raw)
        if ok:
            log.info(f"Extracted {len(seqs)} full-length sequences")
            return _normalize(seqs)

        log.warning("JSON parse failed — retrying with repair prompt")
        content = content + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": (
                "Your previous response could not be parsed as valid JSON. "
                "Return ONLY the corrected JSON array, starting with [ and ending with ]."
            )},
        ]

    return []


def _normalize(seqs: list[dict]) -> list[dict]:
    for s in seqs:
        seq = s.get("sequence") or ""
        # Strip whitespace, digits, and anything that isn't a letter
        s["sequence"] = "".join(c for c in seq if c.isalpha())
    return seqs


def _parse_json(text: str) -> tuple[list[dict], bool]:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.startswith("```")).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return [], False
    try:
        data = json.loads(text[start: end + 1])
        return (data, True) if isinstance(data, list) else ([], False)
    except json.JSONDecodeError as e:
        log.warning(f"JSONDecodeError: {e}")
        return [], False


def _log_usage(usage, model: str) -> None:
    input_t = usage.input_tokens
    output_t = usage.output_tokens
    cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    log.info(
        f"Tokens — in: {input_t:,}  out: {output_t:,}  "
        f"cache_write: {cache_w:,}  cache_read: {cache_r:,}"
    )
    pricing = {
        "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cr": 0.30, "cw": 3.75},
        "claude-haiku-4-5":  {"in": 0.8, "out": 4.0,  "cr": 0.08, "cw": 1.00},
    }
    p = next((v for k, v in pricing.items() if k in model), None)
    if p:
        cost = (
            (input_t - cache_r - cache_w) / 1e6 * p["in"]
            + output_t / 1e6 * p["out"]
            + cache_r / 1e6 * p["cr"]
            + cache_w / 1e6 * p["cw"]
        )
        log.info(f"Estimated cost: ${cost:.4f}")
