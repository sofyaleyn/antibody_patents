from __future__ import annotations

import json
import logging
import re
import time

import anthropic

from .throttle import call_with_throttle

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a bioinformatics expert specializing in antibody patent analysis.
Your task is to read patent text and extract a precise, exhaustive mapping of every
SEQ ID NO to its biological identity and role.

You are highly familiar with:
- Antibody structure: VH, VL, CDRs (heavy and light chain), full chains, constant regions
- Humanization: framework substitutions, variant naming (hz1, hz2, etc.)
- The difference between nucleotide (NT) and amino acid (AA) sequences
- Patent claim language: inline CDR sequences vs. SEQ ID-only references

Critical rules:
- Be EXHAUSTIVE: every SEQ ID NO mentioned must appear in your output.
- Be PRECISE: distinguish VH (variable domain only) from full heavy chain (VH + constant).
- Extract inline sequences exactly as written (e.g. "STSNLAS" from "CDR L2—STSNLAS").
- If no inline sequence is given, set "sequence" to null.
- Nucleotide sequences encoding a protein: set encodes_seq_id to that protein's SEQ ID NO."""

USER_PROMPT_TEMPLATE = """Analyze this patent text and extract the complete SEQ ID NO mapping.

Patent number: {patent_number}

─────────────────────────────────────────────
INSTRUCTIONS
─────────────────────────────────────────────

For EVERY SEQ ID NO in this patent, produce one JSON object with these exact fields:

  "seq_id"         : integer
  "molecule_type"  : "AA" or "NT"
  "sequence"       : string or null — the inline amino acid sequence if written out
                     in the text (e.g. "STSNLAS", "GFTFSSYA"). null if only referenced
                     by SEQ ID NO without the actual letters.
  "region"         : one of EXACTLY:
                       AA: "HCDR1" "HCDR2" "HCDR3" "LCDR1" "LCDR2" "LCDR3"
                           "VH" "VL" "full_heavy_chain" "full_light_chain" "other_peptide"
                       NT: "nucleic_acid"
  "chain"          : "heavy", "light", or "na"
  "is_humanized"   : true or false
  "is_parental"    : true or false
  "encodes_seq_id" : integer or null
  "variant_label"  : string or null
  "numbering_scheme": string or null (e.g. "Kabat", "Chothia", "IMGT")
  "notes"          : string
  "confidence"     : float 0.0–1.0

─────────────────────────────────────────────
OUTPUT FORMAT
─────────────────────────────────────────────

Return ONLY a valid JSON array. No markdown, no explanation. Start with [ end with ].

─────────────────────────────────────────────
PATENT TEXT
─────────────────────────────────────────────

{patent_text}"""


# Regions where chain is unambiguous — override Claude's answer
_REGION_TO_CHAIN = {
    "HCDR1": "heavy", "HCDR2": "heavy", "HCDR3": "heavy", "VH": "heavy",
    "full_heavy_chain": "heavy",
    "LCDR1": "light", "LCDR2": "light", "LCDR3": "light", "VL": "light",
    "full_light_chain": "light",
}


def extract_patent_text(html: str) -> str:
    """Strip HTML tags from claims + description sections only."""
    desc_idx   = html.find('itemprop="description" itemscope')
    claims_idx = html.find('itemprop="claims" itemscope')

    def clean(chunk: str) -> str:
        text = re.sub(r"<[^>]+>", " ", chunk)
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    desc_text   = clean(html[desc_idx:claims_idx])   if desc_idx >= 0 and claims_idx > desc_idx else ""
    # Claims section ends at the next </section> after it starts
    claims_end  = html.find("</section>", claims_idx + 1) if claims_idx >= 0 else -1
    claims_text = clean(html[claims_idx:claims_end]) if claims_idx >= 0 and claims_end > claims_idx else ""

    return f"=== DESCRIPTION ===\n{desc_text}\n\n=== CLAIMS ===\n{claims_text}"


def extract_seq_map(
    client: anthropic.Anthropic,
    html: str,
    patent_number: str,
    model: str = MODEL,
) -> list[dict]:
    patent_text = extract_patent_text(html)
    log.info(f"Patent text length: {len(patent_text):,} chars")

    content = [
        {
            "type": "text",
            "text": USER_PROMPT_TEMPLATE.format(
                patent_number=patent_number,
                patent_text=patent_text,
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Rough token estimate: ~4 chars/token for English text + JSON
    est_tokens = max(1000, len(patent_text) // 4 + 500)

    for attempt in range(1, 3):
        log.info(f"Calling {model}, attempt {attempt}/2 (~{est_tokens:,} input tokens)")
        try:
            response = call_with_throttle(
                client,
                estimated_input_tokens=est_tokens,
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
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

        seq_map, ok = _parse_json(raw)
        if ok:
            log.info(f"Extracted {len(seq_map)} SEQ ID records")
            return _enrich(seq_map)

        log.warning("JSON parse failed — retrying with repair prompt")
        content = content + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": (
                "Your response could not be parsed as valid JSON. "
                "Return ONLY the corrected JSON array, starting with [ and ending with ]."
            )},
        ]

    return []


def _enrich(seq_map: list[dict]) -> list[dict]:
    for s in seq_map:
        region = s.get("region", "")
        if region in _REGION_TO_CHAIN:
            s["chain"] = _REGION_TO_CHAIN[region]
    return seq_map


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
