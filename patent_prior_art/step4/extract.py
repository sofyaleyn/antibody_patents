import base64
import json
import logging
import time
from pathlib import Path

import anthropic

from .validate import REGION_TO_TYPE, REGION_TO_CHAIN

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a bioinformatics expert specializing in antibody patent analysis.
Your task is to read patent documents and extract a precise, exhaustive mapping of every
SEQ ID NO to its biological identity and role.

You are highly familiar with:
- Antibody structure: VH, VL, CDRs (heavy and light chain), full chains, constant regions
- Humanization: framework substitutions, % humanization scores, variant naming
- Patent sequence listing formats (WIPO ST.25 and ST.26)
- The difference between nucleotide (NT) and amino acid (AA) sequences
- Common patent structures: claims, detailed description, sequence listing appendix

Critical rules:
- Be EXHAUSTIVE: every SEQ ID NO mentioned anywhere in the patent must appear in your output.
- Be PRECISE: distinguish VH (variable domain only) from full heavy chain (VH + constant).
- Be CONSERVATIVE: if you cannot determine the region with confidence, set confidence < 0.8
  and describe your uncertainty in the notes field.
- Do NOT invent SEQ IDs that are not in the patent.
- Nucleotide sequences that encode a protein should have encodes_seq_id set to the
  SEQ ID NO of that protein."""

USER_PROMPT_TEMPLATE = """Analyze this patent and extract a complete SEQ ID NO mapping.

Patent number: {patent_number}
Target antigen: {target}
Total sequences expected: {expected_count_str}

─────────────────────────────────────────────
INSTRUCTIONS
─────────────────────────────────────────────

For EVERY SEQ ID NO in this patent, produce one JSON object with these exact fields:

  "seq_id"          : integer — the sequence number (1, 2, 3 ...)
  "molecule_type"   : "AA" or "NT"
  "region"          : one of the following strings EXACTLY:
                        Amino acid sequences:
                          "HCDR1"            heavy chain CDR1
                          "HCDR2"            heavy chain CDR2
                          "HCDR3"            heavy chain CDR3
                          "LCDR1"            light chain CDR1
                          "LCDR2"            light chain CDR2
                          "LCDR3"            light chain CDR3
                          "VH"               heavy variable domain (no constant)
                          "VL"               light variable domain (no constant)
                          "full_heavy_chain" VH + heavy constant region
                          "full_light_chain" VL + light constant region
                          "other_peptide"    linkers, tags, epitope peptides, etc.
                        Nucleotide sequences:
                          "nucleic_acid"
  "type"            : "CDR"  or coarse: CDR / variable_domain / full_chain / nucleic_acid
  "chain"           : "heavy", "light", or "na"
                      For CDRs: set to "heavy" or "light" as appropriate.
                      For nucleic acids encoding a heavy-chain protein: "heavy".
                      For ambiguous or non-chain sequences: "na".
  "is_humanized"    : true or false
  "is_parental"     : true or false (the original, non-humanized sequence)
  "encodes_seq_id"  : integer or null
                      If this is a nucleotide sequence, the SEQ ID NO of the protein
                      it encodes. Otherwise null.
  "variant_label"   : string or null
                      If the patent contains multiple humanized variants, the label used
                      (e.g. "hz1", "VH_v2", "preferred"). Otherwise null.
  "numbering_scheme": string or null
                      e.g. "Kabat", "Chothia", "IMGT" if explicitly stated. Otherwise null.
  "notes"           : string
                      Any additional relevant detail. Use empty string "" if nothing to add.
  "confidence"      : float between 0.0 and 1.0

─────────────────────────────────────────────
OUTPUT FORMAT
─────────────────────────────────────────────

Return ONLY a valid JSON array.
- No markdown code fences
- No explanation text before or after
- No trailing commas
- Start with [ and end with ]

─────────────────────────────────────────────
EXAMPLE (illustrative only — do not copy)
─────────────────────────────────────────────

[
  {{
    "seq_id": 1,
    "molecule_type": "AA",
    "region": "HCDR1",
    "chain": "heavy",
    "is_humanized": false,
    "is_parental": true,
    "encodes_seq_id": null,
    "variant_label": null,
    "numbering_scheme": "Kabat",
    "notes": "",
    "confidence": 1.0
  }},
  {{
    "seq_id": 7,
    "molecule_type": "NT",
    "region": "nucleic_acid",
    "chain": "heavy",
    "is_humanized": false,
    "is_parental": true,
    "encodes_seq_id": 8,
    "variant_label": null,
    "numbering_scheme": null,
    "notes": "encodes parental VH (SEQ ID 8)",
    "confidence": 1.0
  }}
]

Now analyze the attached patent and return the complete mapping for ALL {expected_count_str} sequences."""


def enrich_seq_map(seq_map: list[dict]) -> list[dict]:
    """
    Derive fields that must be consistent with 'region'.

    1. type  — always derived from region in Python so it is always consistent.
    2. chain — for regions where chain is unambiguous, override Claude's answer.
    """
    for s in seq_map:
        region = s.get("region", "")
        s["type"] = REGION_TO_TYPE.get(region, "unknown")
        if region in REGION_TO_CHAIN:
            s["chain"] = REGION_TO_CHAIN[region]
    return seq_map


def extract_seq_map(
    client: anthropic.Anthropic,
    pdf_path: str | Path,
    patent_number: str,
    target: str,
    expected_count: int | None = None,
    model: str = "claude-sonnet-4-6",
    use_thinking: bool = False,
    thinking_budget: int = 8000,
    max_retries: int = 2,
) -> list[dict]:
    """
    Core extraction: send PDF to Claude, return parsed list of seq map dicts.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    log.info(f"Loading PDF: {pdf_path} ({pdf_path.stat().st_size / 1024:.0f} KB)")
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")

    expected_count_str = str(expected_count) if expected_count else "unknown (extract all you find)"

    content = [
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
            "text": USER_PROMPT_TEMPLATE.format(
                patent_number=patent_number,
                target=target,
                expected_count_str=expected_count_str,
            ),
        },
    ]

    for attempt in range(1, max_retries + 1):
        log.info(f"Calling Claude ({model}), attempt {attempt}/{max_retries}")

        kwargs: dict = dict(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )

        if use_thinking:
            if "sonnet" not in model and "opus" not in model:
                log.warning("Thinking is only supported on Sonnet/Opus — disabling.")
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget,
                }
                kwargs["max_tokens"] = thinking_budget + 4096

        try:
            response = client.messages.create(**kwargs)
        except anthropic.APIError as e:
            log.error(f"API error on attempt {attempt}: {e}")
            if attempt == max_retries:
                raise
            time.sleep(5 * attempt)
            continue

        usage = response.usage
        input_tokens  = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read    = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write   = getattr(usage, "cache_creation_input_tokens", 0) or 0

        log.info(
            f"Token usage — input: {input_tokens:,}  output: {output_tokens:,}  "
            f"cache_write: {cache_write:,}  cache_read: {cache_read:,}"
        )
        _log_cost_estimate(model, input_tokens, output_tokens, cache_read, cache_write)

        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text = block.text
                break

        if not raw_text.strip():
            log.warning(f"Empty text response on attempt {attempt}")
            if attempt < max_retries:
                time.sleep(3)
                continue
            return []

        seq_map, parse_ok = _parse_json(raw_text, patent_number)

        if parse_ok:
            log.info(f"Parsed {len(seq_map)} sequence records")
            return enrich_seq_map(seq_map)

        log.warning(f"JSON parse failed on attempt {attempt}. Retrying with repair prompt.")
        content = _build_repair_content(content, raw_text)

    return []


def _parse_json(text: str, patent_number: str) -> tuple[list[dict], bool]:
    """Try to parse JSON from Claude response. Returns (data, success)."""
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    start = text.find("[")
    end   = text.rfind("]")
    if start == -1 or end == -1:
        log.warning(f"No JSON array delimiters found in response for {patent_number}")
        log.debug(f"Response snippet: {text[:300]}")
        return [], False

    json_text = text[start: end + 1]

    try:
        data = json.loads(json_text)
        if not isinstance(data, list):
            log.warning("Parsed JSON is not a list")
            return [], False
        return data, True
    except json.JSONDecodeError as e:
        log.warning(f"JSONDecodeError: {e}")
        log.debug(f"Problematic JSON snippet: {json_text[:500]}")
        return [], False


def _build_repair_content(original_content: list[dict], bad_response: str) -> list[dict]:
    """Build a repair prompt that includes the bad response and asks Claude to fix it."""
    return original_content + [
        {"role": "assistant", "content": bad_response},
        {
            "role": "user",
            "content": (
                "Your response above could not be parsed as valid JSON. "
                "Please return ONLY the corrected JSON array with no other text, "
                "starting with [ and ending with ]."
            ),
        },
    ]


def _log_cost_estimate(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> None:
    """Rough cost estimate based on public Anthropic pricing."""
    pricing = {
        "claude-opus-4-6":   {"in": 15.0,  "out": 75.0,  "cache_read": 1.50,  "cache_write": 18.75},
        "claude-sonnet-4-6": {"in": 3.0,   "out": 15.0,  "cache_read": 0.30,  "cache_write": 3.75},
        "claude-haiku-4-5":  {"in": 0.80,  "out": 4.0,   "cache_read": 0.08,  "cache_write": 1.00},
    }

    matched = None
    for key in pricing:
        if key in model:
            matched = key
            break
    if not matched:
        return

    p = pricing[matched]
    cost = (
        (input_tokens - cache_read - cache_write) / 1_000_000 * p["in"]
        + output_tokens / 1_000_000 * p["out"]
        + cache_read / 1_000_000 * p["cache_read"]
        + cache_write / 1_000_000 * p["cache_write"]
    )
    log.info(f"Estimated cost for this call: ${cost:.4f} ({matched})")
