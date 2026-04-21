import logging

log = logging.getLogger(__name__)

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
REGION_TO_CHAIN = {
    "HCDR1": "heavy", "HCDR2": "heavy", "HCDR3": "heavy",
    "LCDR1": "light", "LCDR2": "light", "LCDR3": "light",
    "VH":    "heavy", "VL":    "light",
    "full_heavy_chain": "heavy",
    "full_light_chain": "light",
}


def validate_seq_map(
    seq_map: list[dict],
    expected_count: int | None = None,
    patent_number: str = "",
) -> tuple[bool, list[str]]:
    """
    Validate the extracted seq_map.

    Returns:
        (passed: bool, issues: list[str])
        passed=True means no blocking errors (warnings may still exist).
    """
    warnings = []
    errors = []

    if not seq_map:
        errors.append("seq_map is empty — extraction produced no records")
        return False, errors

    seq_ids = [s.get("seq_id") for s in seq_map]

    seen = set()
    dupes = set()
    for sid in seq_ids:
        if sid in seen:
            dupes.add(sid)
        seen.add(sid)
    if dupes:
        errors.append(f"Duplicate seq_ids: {sorted(dupes)}")

    if expected_count is not None and len(seq_map) != expected_count:
        missing = sorted(set(range(1, expected_count + 1)) - set(seq_ids))
        extra = sorted(set(seq_ids) - set(range(1, expected_count + 1)))
        msg = f"Expected {expected_count} seqs, got {len(seq_map)}."
        if missing:
            msg += f" Missing SEQ IDs: {missing}."
        if extra:
            msg += f" Unexpected SEQ IDs: {extra}."
        errors.append(msg)

    for s in seq_map:
        sid    = s.get("seq_id", "?")
        region = s.get("region", "")
        mol    = s.get("molecule_type", "")

        for field in ["seq_id", "molecule_type", "region", "chain",
                      "is_humanized", "is_parental", "confidence"]:
            if field not in s:
                errors.append(f"ERROR: SEQ {sid}: missing required field '{field}'")

        if mol not in VALID_MOL_TYPES:
            errors.append(f"ERROR: SEQ {sid}: invalid molecule_type '{mol}'")
        if region not in VALID_REGIONS:
            errors.append(f"ERROR: SEQ {sid}: invalid region '{region}'")
        if s.get("chain") not in VALID_CHAINS:
            errors.append(f"ERROR: SEQ {sid}: invalid chain '{s.get('chain')}'")

        expected_type = REGION_TO_TYPE.get(region, "unknown")
        if s.get("type") != expected_type:
            warnings.append(
                f"WARN:  SEQ {sid}: type='{s.get('type')}' doesn't match "
                f"region='{region}' (expected '{expected_type}')"
            )

        if region == "nucleic_acid" and s.get("encodes_seq_id") is None:
            warnings.append(f"WARN:  SEQ {sid}: nucleic_acid but encodes_seq_id is null")

        if mol == "NT" and region != "nucleic_acid":
            errors.append(
                f"ERROR: SEQ {sid}: molecule_type=NT but region='{region}' "
                f"(must be 'nucleic_acid')"
            )

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
