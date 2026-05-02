"""Merge AbPatentDB sequences with Google Patents seq_map annotations.

For each patent, reads two sources:
  - _sequences.csv  (AbPatentDB): full VH/VL sequences, chain inferred from V-gene
  - _seq_map.csv    (Google Patents): SEQ ID role annotations, inline CDR sequences

Join strategy:
  1. For GP seq_map rows where region=VH/VL and sequence is null:
     find matching AbPatentDB sequence by location (VH/VL).
     Fill if exactly 1 match; note ambiguity if multiple.
  2. AbPatentDB rows not matched to any GP entry → appended with abpatentdb_only annotation.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_MERGED_FIELDNAMES = [
    "seq_id", "patent_number", "molecule_type", "sequence", "sequence_source",
    "region", "chain", "is_humanized", "is_parental", "encodes_seq_id",
    "variant_label", "numbering_scheme", "notes", "confidence",
]

# AbPatentDB location → chain label used by Google Patents
_LOCATION_TO_CHAIN = {"VH": "heavy", "VK": "light", "VL": "light"}

# AbPatentDB locations that can supply sequence for a given Google Patents region
_GP_REGION_TO_AB_LOCATIONS: dict[str, set[str]] = {
    "VH": {"VH"},
    "VL": {"VL", "VK"},
    "VK": {"VL", "VK"},
}


def _write_fasta(rows: list[dict], path: Path) -> None:
    with open(path, "w") as f:
        for r in rows:
            seq = (r.get("sequence") or "").strip()
            if not seq:
                continue
            header = f"{r['patent_number']}|{r['seq_id']}|{r.get('region', '?')}|{r.get('chain', '?')}"
            wrapped = "\n".join(seq[i:i + 60] for i in range(0, len(seq), 60))
            f.write(f">{header}\n{wrapped}\n")


def merge_patent(
    patent_number: str,
    sequences_csv: Path | None,
    seq_map_csv: Path | None,
    output_dir: Path,
) -> list[dict]:
    """Merge one patent's AbPatentDB sequences with Google Patents seq_map.

    Returns merged rows. Writes {patent_number}_merged_seq_map.csv and
    {patent_number}_merged.fasta to output_dir.
    """
    ab_rows: list[dict] = []
    if sequences_csv and sequences_csv.exists():
        with open(sequences_csv, newline="") as f:
            ab_rows = list(csv.DictReader(f))

    gp_rows: list[dict] = []
    if seq_map_csv and seq_map_csv.exists():
        with open(seq_map_csv, newline="") as f:
            gp_rows = list(csv.DictReader(f))

    if not ab_rows and not gp_rows:
        log.warning(f"{patent_number}: both sources empty, skipping")
        return []

    ab_by_location: dict[str, list[tuple[int, dict]]] = {}
    for i, r in enumerate(ab_rows):
        loc = (r.get("location") or "").upper()
        if loc:
            ab_by_location.setdefault(loc, []).append((i, r))

    merged: list[dict] = []
    matched_ab: set[int] = set()

    for gp in gp_rows:
        inline_seq = (gp.get("sequence") or "").strip() or None
        row: dict = {
            "seq_id":           gp.get("seq_id", ""),
            "patent_number":    patent_number,
            "molecule_type":    gp.get("molecule_type", "AA"),
            "sequence":         inline_seq,
            "sequence_source":  "google_patents_inline" if inline_seq else None,
            "region":           gp.get("region", ""),
            "chain":            gp.get("chain", ""),
            "is_humanized":     gp.get("is_humanized", ""),
            "is_parental":      gp.get("is_parental", ""),
            "encodes_seq_id":   gp.get("encodes_seq_id", ""),
            "variant_label":    gp.get("variant_label", ""),
            "numbering_scheme": gp.get("numbering_scheme", ""),
            "notes":            gp.get("notes", ""),
            "confidence":       gp.get("confidence", ""),
        }

        if not row["sequence"] and ab_rows:
            region = (gp.get("region") or "").upper()
            candidate_locs = _GP_REGION_TO_AB_LOCATIONS.get(region, set())
            candidates: list[tuple[int, dict]] = []
            for loc in candidate_locs:
                candidates.extend(ab_by_location.get(loc, []))

            if len(candidates) == 1:
                ab_idx, ab = candidates[0]
                row["sequence"] = (ab.get("sequence") or "").strip() or None
                row["sequence_source"] = "abpatentdb"
                matched_ab.add(ab_idx)
            elif len(candidates) > 1:
                note = f"[merge: {len(candidates)} AbPatentDB seqs match region {region} — ambiguous]"
                row["notes"] = (row["notes"] + " " + note).strip()

        merged.append(row)

    for i, ab in enumerate(ab_rows):
        if i in matched_ab:
            continue
        loc = (ab.get("location") or "").upper()
        seq = (ab.get("sequence") or "").strip() or None
        merged.append({
            "seq_id":           ab.get("seq_id", ""),
            "patent_number":    patent_number,
            "molecule_type":    ab.get("molecule_type", "AA"),
            "sequence":         seq,
            "sequence_source":  "abpatentdb" if seq else None,
            "region":           loc,
            "chain":            _LOCATION_TO_CHAIN.get(loc, ""),
            "is_humanized":     "",
            "is_parental":      "",
            "encodes_seq_id":   "",
            "variant_label":    "",
            "numbering_scheme": "",
            "notes":            (ab.get("notes") or "") + " [abpatentdb_only]",
            "confidence":       ab.get("confidence", ""),
        })

    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / f"{patent_number}_merged_seq_map.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_MERGED_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)
    log.info(f"Wrote {csv_path.name} ({len(merged)} records)")

    fasta_path = output_dir / f"{patent_number}_merged.fasta"
    _write_fasta(merged, fasta_path)
    seq_count = sum(1 for r in merged if r.get("sequence"))
    log.info(f"Wrote {fasta_path.name} ({seq_count} sequences)")

    return merged


def find_patent_pairs(
    sequences_dir: Path,
    seq_map_dir: Path,
) -> list[tuple[str, Path | None, Path | None]]:
    """Find all patents with at least one source file in either directory."""
    patents: dict[str, dict[str, Path]] = {}

    for p in sequences_dir.glob("*_sequences.csv"):
        pn = p.stem.removesuffix("_sequences")
        patents.setdefault(pn, {})["sequences"] = p

    for p in seq_map_dir.glob("*_seq_map.csv"):
        pn = p.stem.removesuffix("_seq_map")
        patents.setdefault(pn, {})["seq_map"] = p

    return [
        (pn, files.get("sequences"), files.get("seq_map"))
        for pn, files in sorted(patents.items())
    ]
