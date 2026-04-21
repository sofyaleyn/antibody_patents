"""
step4_patent_seq_extractor.py
─────────────────────────────
Sends a patent PDF to Claude and extracts a complete SEQ ID NO → biological role mapping.

Usage (standalone):
    python step4_patent_seq_extractor.py \
        --pdf WO2020139171.pdf \
        --patent WO2020139171 \
        --target TRBV3 \
        --expected-count 22

Usage (as module in the main pipeline):
    from step4_patent_seq_extractor import run_step4
    seq_map = run_step4("WO2020139171.pdf", "WO2020139171", "TRBV3", expected_count=22)

Output:
    {patent_number}_seq_map.json   — raw Claude output, one record per SEQ ID
    {patent_number}_seq_map.csv    — same data as CSV for inspection / Step 5 join

Requirements:
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."
"""

import argparse
import base64
import json
import logging
import os
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
# Controlled vocabularies
# ─────────────────────────────────────────────

VALID_REGIONS = {
    "HCDR1", "HCDR2", "HCDR3",
    "LCDR1", "LCDR2", "LCDR3",
    "VH", "VL",
    "full_heavy_chain", "full_light_chain",
    "other_peptide",
    "nucleic_acid",
}

VALID_CHAINS    = {"heavy", "light", "na"}
VALID_MOL_TYPES = {"AA", "NT"}

# Derived automatically from region — never asked from Claude directly
# to avoid Claude giving inconsistent type/region combinations
REGION_TO_TYPE = {
    "HCDR1":            "CDR",
    "HCDR2":            "CDR",
    "HCDR3":            "CDR",
    "LCDR1":            "CDR",
    "LCDR2":            "CDR",
    "LCDR3":            "CDR",
    "VH":               "variable_domain",
    "VL":               "variable_domain",
    "full_heavy_chain": "full_chain",
    "full_light_chain": "full_chain",
    "other_peptide":    "other",
    "nucleic_acid":     "nucleic_acid",
}

# For these regions, chain is unambiguous from the region name alone.
# We override Claude's chain answer for these to guarantee consistency.
REGION_TO_CHAIN = {
    "HCDR1": "heavy", "HCDR2": "heavy", "HCDR3": "heavy",
    "LCDR1": "light", "LCDR2": "light", "LCDR3": "light",
    "VH":    "heavy", "VL":    "light",
    "full_heavy_chain": "heavy",
    "full_light_chain": "light",
}
# ─────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────

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
                      Any additional relevant detail:
                      - humanization percentage ("87% humanization vs SEQ 8")
                      - isotype ("IgG1 constant region")
                      - signal peptide ("includes N-terminal signal peptide")
                      - your uncertainty ("could be VH or full_heavy_chain, no constant stated")
                      Use empty string "" if nothing to add.
  "confidence"      : float between 0.0 and 1.0
                      Your confidence in the region/chain/type assignment.
                      1.0 = explicitly stated in patent.
                      0.9 = strongly implied by context.
                      < 0.8 = ambiguous, inferred.

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
  }},
  {{
    "seq_id": 16,
    "molecule_type": "AA",
    "region": "VH",
    "chain": "heavy",
    "is_humanized": true,
    "is_parental": false,
    "encodes_seq_id": null,
    "variant_label": "preferred",
    "numbering_scheme": null,
    "notes": "87% humanization vs parental SEQ 8; 10+ framework substitutions",
    "confidence": 1.0
  }}
]

Now analyze the attached patent and return the complete mapping for ALL {expected_count_str} sequences."""

# ─────────────────────────────────────────────
# Post-processing: enrich after parsing
# ─────────────────────────────────────────────

def enrich_seq_map(seq_map: list[dict]) -> list[dict]:
    """
    Derive fields that must be consistent with 'region'.

    1. type  — coarse category (CDR / variable_domain / full_chain / nucleic_acid / other)
               Always derived from region in Python so it is always consistent.
               Claude is NOT asked to produce this field.

    2. chain — for regions where chain is unambiguous (all CDRs, VH, VL, full chains),
               override whatever Claude said to guarantee correctness.
               For nucleic_acid and other_peptide, keep Claude's answer.
    """
    for s in seq_map:
        region = s.get("region", "")

        # Derive type
        s["type"] = REGION_TO_TYPE.get(region, "unknown")

        # Override chain where unambiguous
        if region in REGION_TO_CHAIN:
            s["chain"] = REGION_TO_CHAIN[region]

    return seq_map

# ─────────────────────────────────────────────
# Core extraction function
# ─────────────────────────────────────────────

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

    Args:
        client:          Anthropic client instance
        pdf_path:        Path to the patent PDF
        patent_number:   e.g. "WO2020139171"
        target:          Antigen target, e.g. "TRBV3", "PD-1"
        expected_count:  How many SEQ IDs to expect (from lens.org scrape in Step 2).
                         Used for validation only, not sent to Claude unless known.
        model:           Claude model to use
        use_thinking:    Enable extended thinking (better for complex patents)
        thinking_budget: Token budget for thinking (only used if use_thinking=True)
        max_retries:     Number of retry attempts on parse failure

    Returns:
        List of dicts, one per SEQ ID NO
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
            # Cache the PDF so retries / re-runs don't re-upload and re-charge
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
                # max_tokens must be larger than thinking_budget
                kwargs["max_tokens"] = thinking_budget + 4096

        try:
            response = client.messages.create(**kwargs)
        except anthropic.APIError as e:
            log.error(f"API error on attempt {attempt}: {e}")
            if attempt == max_retries:
                raise
            time.sleep(5 * attempt)
            continue

        # Log token usage
        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

        log.info(
            f"Token usage — input: {input_tokens:,}  output: {output_tokens:,}  "
            f"cache_write: {cache_write:,}  cache_read: {cache_read:,}"
        )
        _log_cost_estimate(model, input_tokens, output_tokens, cache_read, cache_write)

        # Extract text block (skip thinking blocks)
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
        
        # Parse failed — retry with a nudge
        log.warning(f"JSON parse failed on attempt {attempt}. Retrying with repair prompt.")
        content = _build_repair_content(content, raw_text)

    return []


# ─────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────

def validate_seq_map(
    seq_map: list[dict],
    expected_count: int | None = None,
    patent_number: str = "",
) -> tuple[bool, list[str]]:
    """
    Validate the extracted seq_map.

    Returns:
        (passed: bool, warnings: list[str])
        passed=True means no blocking errors (warnings may still exist).
    """
    warnings = []
    errors = []

    if not seq_map:
        errors.append("seq_map is empty — extraction produced no records")
        return False, errors

    seq_ids = [s.get("seq_id") for s in seq_map]

    # Duplicate seq_ids
    seen = set()
    dupes = set()
    for sid in seq_ids:
        if sid in seen:
            dupes.add(sid)
        seen.add(sid)
    if dupes:
        errors.append(f"Duplicate seq_ids: {sorted(dupes)}")

    # Expected count
    if expected_count is not None and len(seq_map) != expected_count:
        missing = sorted(set(range(1, expected_count + 1)) - set(seq_ids))
        extra = sorted(set(seq_ids) - set(range(1, expected_count + 1)))
        msg = f"Expected {expected_count} seqs, got {len(seq_map)}."
        if missing:
            msg += f" Missing SEQ IDs: {missing}."
        if extra:
            msg += f" Unexpected SEQ IDs: {extra}."
        errors.append(msg)

# ── Per-record checks ────────────────────────────────────────────────────
    for s in seq_map:
        sid    = s.get("seq_id", "?")
        region = s.get("region", "")
        mol    = s.get("molecule_type", "")

        # Required fields present
        for field in ["seq_id", "molecule_type", "region", "chain",
                      "is_humanized", "is_parental", "confidence"]:
            if field not in s:
                errors.append(f"ERROR: SEQ {sid}: missing required field '{field}'")

        # Controlled vocabulary checks
        if mol not in VALID_MOL_TYPES:
            errors.append(f"ERROR: SEQ {sid}: invalid molecule_type '{mol}'")
        if region not in VALID_REGIONS:
            errors.append(f"ERROR: SEQ {sid}: invalid region '{region}'")
        if s.get("chain") not in VALID_CHAINS:
            errors.append(f"ERROR: SEQ {sid}: invalid chain '{s.get('chain')}'")

        # type field must match region (enrich_seq_map sets this — should always be correct)
        expected_type = REGION_TO_TYPE.get(region, "unknown")
        if s.get("type") != expected_type:
            warnings.append(
                f"WARN:  SEQ {sid}: type='{s.get('type')}' doesn't match "
                f"region='{region}' (expected '{expected_type}')"
            )

        # NT sequences must have encodes_seq_id
        if region == "nucleic_acid" and s.get("encodes_seq_id") is None:
            warnings.append(f"WARN:  SEQ {sid}: nucleic_acid but encodes_seq_id is null")

        # NT sequences must use region="nucleic_acid"
        if mol == "NT" and region != "nucleic_acid":
            errors.append(
                f"ERROR: SEQ {sid}: molecule_type=NT but region='{region}' "
                f"(must be 'nucleic_acid')"
            )

        # Low confidence
        conf = s.get("confidence", 1.0)
        if isinstance(conf, float) and conf < 0.8:
            warnings.append(
                f"WARN:  SEQ {sid}: confidence={conf:.2f} — {s.get('notes', '')}"
            )

    passed = len(errors) == 0
    all_issues = errors + warnings

    if errors:
        log.error(f"[Validate] {patent_number}: {len(errors)} errors, {len(warnings)} warnings")
    elif warnings:
        log.warning(f"[Validate] {patent_number}: 0 errors, {len(warnings)} warnings")
    else:
        log.info(f"[Validate] {patent_number}: all checks passed ✓")

    for issue in all_issues:
        log.warning(f"  {'ERROR' if issue in errors else 'WARN '}: {issue}")

    return passed, all_issues

def cross_validate_fasta_vs_map(
    fasta_records: list[dict],
    seq_map: list[dict],
    patent_number: str = "",
) -> list[str]:
    """
    Cross-check that every SEQ ID scraped from lens.org (Step 2) appears in
    Claude's map, and that Claude didn't invent extra ones.

    This function belongs to Step 5 (the join step) because fasta_records
    are produced by Step 2. It lives here so it can be imported alongside
    run_step4 in the main runner.

    Args:
        fasta_records: list of dicts from Step 2 scrape.
                       Each dict must have a "seq_id" key (integer).
                       e.g. [{"seq_id": 1, "sequence": "MEVQL..."}, ...]
        seq_map:       output of run_step4()
        patent_number: used for log messages only

    Returns:
        List of issue strings. Empty list = perfect match.
    """
    fasta_ids = {r["seq_id"] for r in fasta_records}
    map_ids   = {s["seq_id"] for s in seq_map}

    issues = []

    in_fasta_not_map = sorted(fasta_ids - map_ids)
    in_map_not_fasta = sorted(map_ids - fasta_ids)

    if in_fasta_not_map:
        issues.append(
            f"{patent_number}: SEQ IDs scraped from lens.org but missing from "
            f"Claude map: {in_fasta_not_map}"
        )
    if in_map_not_fasta:
        issues.append(
            f"{patent_number}: SEQ IDs in Claude map but not scraped from "
            f"lens.org: {in_map_not_fasta}"
        )

    if not issues:
        log.info(f"[Cross-validate] {patent_number}: fasta ↔ map match perfectly ✓")
    else:
        for issue in issues:
            log.warning(f"[Cross-validate] {issue}")

    return issues

# ─────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────

def save_json(seq_map: list[dict], output_dir: Path, patent_number: str) -> Path:
    out = output_dir / f"{patent_number}_seq_map.json"
    out.write_text(json.dumps(seq_map, indent=2))
    log.info(f"Saved JSON: {out}")
    return out


def save_csv(seq_map: list[dict], output_dir: Path, patent_number: str) -> Path:
    """Save as CSV — flat, ready for inspection or Step 5 join."""
    import csv

   fieldnames = [
        "seq_id", "patent_number",
        "molecule_type", "region", "type", "chain",
        "is_humanized", "is_parental",
        "encodes_seq_id", "variant_label", "numbering_scheme",
        "notes", "confidence",
    ]


    out = output_dir / f"{patent_number}_seq_map.csv"
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for record in seq_map:
            row = {"patent_number": patent_number, **record}
            writer.writerow(row)

    log.info(f"Saved CSV:  {out}")
    return out


def print_summary_table(seq_map: list[dict], patent_number: str) -> None:
    """Print a human-readable summary to stdout."""
    print(f"\n{'─'*70}")
    print(f"SEQ ID MAP — {patent_number}  ({len(seq_map)} sequences)")
    print(f"{'─'*70}")
    print(f"{'ID':>4}  {'Type':3}  {'Region':<20}  {'Chain':6}  {'Human':5}  {'Conf':5}  Notes")
    print(f"{'─'*70}")
    for s in sorted(seq_map, key=lambda x: x.get("seq_id", 0)):
        human_flag = "✓" if s.get("is_humanized") else "·"
        notes = s.get("notes", "")[:35]
        enc = f" →encodes {s['encodes_seq_id']}" if s.get("encodes_seq_id") else ""
        print(
            f"{s.get('seq_id','?'):>4}  "
            f"{s.get('molecule_type','?'):3}  "
            f"{s.get('region','?'):<20}  "
            f"{s.get('chain','?'):6}  "
            f"{human_flag:5}  "
            f"{s.get('confidence', 0):.2f}  "
            f"{notes}{enc}"
        )
    print(f"{'─'*70}\n")


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _parse_json(text: str, patent_number: str) -> tuple[list[dict], bool]:
    """Try to parse JSON from Claude response. Returns (data, success)."""
    text = text.strip()

    # Strip markdown fences if Claude added them despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    # Find the JSON array bounds
    start = text.find("[")
    end = text.rfind("]")
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


def _build_repair_content(
    original_content: list[dict], bad_response: str
) -> list[dict]:
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
    """Rough cost estimate based on public Anthropic pricing (Feb 2025)."""
    # Prices per million tokens (MTok)
    pricing = {
        "claude-opus-4-6":    {"in": 15.0,  "out": 75.0,  "cache_read": 1.50,  "cache_write": 18.75},
        "claude-sonnet-4-6":  {"in": 3.0,   "out": 15.0,  "cache_read": 0.30,  "cache_write": 3.75},
        "claude-haiku-4-5":   {"in": 0.80,  "out": 4.0,   "cache_read": 0.08,  "cache_write": 1.00},
    }

    # Match model by substring
    matched = None
    for key in pricing:
        if key in model:
            matched = key
            break
    if not matched:
        return  # Unknown model, skip

    p = pricing[matched]
    cost = (
        (input_tokens - cache_read - cache_write) / 1_000_000 * p["in"]
        + output_tokens / 1_000_000 * p["out"]
        + cache_read / 1_000_000 * p["cache_read"]
        + cache_write / 1_000_000 * p["cache_write"]
    )
    log.info(f"Estimated cost for this call: ${cost:.4f} ({matched})")


# ─────────────────────────────────────────────
# Public entry point (for import by main runner)
# ─────────────────────────────────────────────

def run_step4(
    pdf_path: str | Path,
    patent_number: str,
    target: str,
    expected_count: int | None = None,
    output_dir: str | Path = ".",
    model: str = "claude-sonnet-4-6",
    use_thinking: bool = False,
    save_outputs: bool = True,
) -> list[dict]:
    """
    Full Step 4 pipeline: extract → enrich -> validate → save → return.

    Args:
        pdf_path:       Path to the patent PDF file
        patent_number:  Patent number string (used for filenames and prompts)
        target:         Antigen name (e.g. "PD-1", "TRBV3")
        expected_count: Total sequences expected (from Step 2 scrape). Optional.
        output_dir:     Where to write JSON/CSV outputs
        model:          Claude model to use
        use_thinking:   Enable extended thinking
        save_outputs:   Whether to write output files (set False in unit tests)

    Returns:
        List of enriched seq_map dicts (one per SEQ ID NO in the patent)
        Each dict has keys:
            seq_id, molecule_type, region, type, chain,
            is_humanized, is_parental, encodes_seq_id,
            variant_label, numbering_scheme, notes, confidence
    """
    from dotenv import load_dotenv
    load_dotenv()
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Extract ──────────────────────────────────────────────────────────────
    seq_map = extract_seq_map(
        client=client,
        pdf_path=pdf_path,
        patent_number=patent_number,
        target=target,
        expected_count=expected_count,
        model=model,
        use_thinking=use_thinking,
    )
    passed, issues = validate_seq_map(seq_map, expected_count, patent_number)

    if not seq_map:
        log.error(f"[Step 4] Extraction returned empty result for {patent_number}")
        # Escalate: retry with Opus + thinking if we were using a lighter model
        if model != "claude-opus-4-6":
            log.info("[Step 4] Retrying with claude-opus-4-6 + thinking...")
            seq_map = extract_seq_map(
                client=client,
                pdf_path=pdf_path,
                patent_number=patent_number,
                target=target,
                expected_count=expected_count,
                model="claude-opus-4-6",
                use_thinking=True,
            )

    # ── Validate ─────────────────────────────────────────────────────────────
    passed, issues = validate_seq_map(seq_map, expected_count, patent_number)

    # If validation failed and we haven't tried Opus yet, escalate
    if not passed and model != "claude-opus-4-6":
        log.warning("[Step 4] Validation failed — retrying with claude-opus-4-6 + thinking")
        seq_map = extract_seq_map(
            client=client,
            pdf_path=pdf_path,
            patent_number=patent_number,
            target=target,
            expected_count=expected_count,
            model="claude-opus-4-6",
            use_thinking=True,
        )
        passed, issues = validate_seq_map(seq_map, expected_count, patent_number)

    # ── Save ─────────────────────────────────────────────────────────────────
    if save_outputs and seq_map:
        save_json(seq_map, output_dir, patent_number)
        save_csv(seq_map, output_dir, patent_number)
        print_summary_table(seq_map, patent_number)

    # Store validation issues as a sidecar for the main runner
    if save_outputs and issues:
        issues_path = output_dir / f"{patent_number}_validation_issues.txt"
        issues_path.write_text("\n".join(issues))
        log.info(f"Validation issues saved: {issues_path}")

    return seq_map


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 4: Extract SEQ ID map from patent PDF using Claude"
    )
    parser.add_argument("--pdf", required=True, help="Path to patent PDF")
    parser.add_argument("--patent", required=True, help="Patent number, e.g. WO2020139171")
    parser.add_argument("--target", required=True, help="Antigen target, e.g. TRBV3")
    parser.add_argument(
        "--expected-count", type=int, default=None,
        help="Expected number of SEQ IDs (from lens.org — for validation)"
    )
    parser.add_argument(
        "--output-dir", default=".", help="Directory to write output files"
    )
    parser.add_argument(
        "--model", default="claude-sonnet-4-6",
        choices=["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
        help="Claude model to use (default: claude-sonnet-4-6)"
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="Enable extended thinking (slower but better for complex patents)"
    )
    parser.add_argument(
        "--thinking-budget", type=int, default=8000,
        help="Thinking token budget (default: 8000)"
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    seq_map = run_step4(
        pdf_path=args.pdf,
        patent_number=args.patent,
        target=args.target,
        expected_count=args.expected_count,
        output_dir=args.output_dir,
        model=args.model,
        use_thinking=args.thinking,
        thinking_budget=args.thinking_budget,
    )

    sys.exit(0 if seq_map else 1)


if __name__ == "__main__":
    main()
