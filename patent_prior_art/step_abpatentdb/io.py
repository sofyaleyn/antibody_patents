from __future__ import annotations

import csv
import logging
from pathlib import Path

from .schema import chain_from_v_gene, region_from_v_gene

log = logging.getLogger(__name__)

_SEQ_FIELDNAMES = [
    "seq_id", "patent_number", "molecule_type", "length",
    "fasta_header", "sequence", "location", "organism",
]
_MAP_FIELDNAMES = [
    "seq_id", "patent_number", "molecule_type", "region", "chain",
    "is_humanized", "is_parental", "encodes_seq_id", "variant_label",
    "notes", "confidence",
]
_VALIDATED_FIELDNAMES = [
    "patent_number", "target", "title", "assignee", "year",
    "validates", "confidence", "reason", "abstract_snippet",
]


def _to_step2_records(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows):
        seq = r.get("sequence") or ""
        out.append({
            "seq_id":        i + 1,
            "patent_number": r["patent_number"],
            "molecule_type": "AA",
            "length":        len(seq),
            "fasta_header":  f"patent|{r['patent_number']}|{i+1}|AA",
            "sequence":      seq,
            "location":      region_from_v_gene(r.get("v_gene")),
            "organism":      r.get("species") or "",
        })
    return out


def _to_step4_records(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows):
        out.append({
            "seq_id":         i + 1,
            "patent_number":  r["patent_number"],
            "molecule_type":  "AA",
            "region":         region_from_v_gene(r.get("v_gene")),
            "chain":          chain_from_v_gene(r.get("v_gene")),
            "is_humanized":   "",
            "is_parental":    "",
            "encodes_seq_id": "",
            "variant_label":  "",
            "notes":          f"source:naturalantibody_db target:{r.get('target_protein','')}",
            "confidence":     1.0,
        })
    return out


def _write_fasta(records: list[dict], path: Path) -> None:
    with open(path, "w") as f:
        for r in records:
            header  = r.get("fasta_header") or f">{r['patent_number']}|{r['seq_id']}"
            seq     = r.get("sequence", "")
            wrapped = "\n".join(seq[i:i+60] for i in range(0, len(seq), 60))
            f.write(f">{header}\n{wrapped}\n")


def write_outputs(
    rows: list[dict],
    output_dir: Path,
    target: str,
    max_patents: int,
    meta_by_family: dict[int, dict] | None = None,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = target.lower().replace(" ", "_")

    # Group by patent — skip rows where patent_number is None
    by_patent: dict[str, list[dict]] = {}
    for r in rows:
        pn = r.get("patent_number")
        if pn:
            by_patent.setdefault(pn, []).append(r)

    # Build a patent_number → family_id map (take first matching row)
    family_id_by_patent: dict[str, int] = {}
    for r in rows:
        pn = r.get("patent_number")
        fid = r.get("family_id")
        if pn and fid and pn not in family_id_by_patent:
            family_id_by_patent[pn] = fid

    # Write validated.csv (Step 1 format) — all matched patents
    validated_path = output_dir / f"{slug}_validated.csv"
    with open(validated_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_VALIDATED_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for patent_number, patent_rows in sorted(by_patent.items()):
            meta = (meta_by_family or {}).get(family_id_by_patent.get(patent_number, -1), {})
            abstract = meta.get("abstract", "")
            w.writerow({
                "patent_number":    patent_number,
                "target":           target,
                "title":            meta.get("title", ""),
                "assignee":         meta.get("applicants", ""),
                "year":             meta.get("year", ""),
                "validates":        True,
                "confidence":       1.0,
                "reason":           f"source:naturalantibody_db n_seqs:{len(patent_rows)}",
                "abstract_snippet": abstract[:300] if abstract else "",
            })
    log.info(f"Wrote {validated_path.name}: {len(by_patent)} patents")

    # Per-patent files (Step 2 format), capped at max_patents
    patents_written = list(sorted(by_patent.keys()))[:max_patents]
    if len(by_patent) > max_patents:
        log.warning(
            f"{len(by_patent)} patents found; writing per-patent files for first {max_patents}. "
            f"Use --max-patents to increase."
        )

    for patent_number in patents_written:
        patent_rows = by_patent[patent_number]
        step2_recs  = _to_step2_records(patent_rows)
        step4_recs  = _to_step4_records(patent_rows)

        seq_csv = output_dir / f"{patent_number}_sequences.csv"
        with open(seq_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_SEQ_FIELDNAMES, extrasaction="ignore")
            w.writeheader()
            w.writerows(step2_recs)

        _write_fasta(step2_recs, output_dir / f"{patent_number}.fasta")

        map_csv = output_dir / f"{patent_number}_seq_map.csv"
        with open(map_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_MAP_FIELDNAMES, extrasaction="ignore")
            w.writeheader()
            w.writerows(step4_recs)

    log.info(f"Wrote per-patent files for {len(patents_written)} patents to {output_dir}")
    return {"total_sequences": len(rows), "total_patents": len(by_patent),
            "patents_written": len(patents_written)}


def write_scored_outputs(
    scored_rows: list[dict],
    output_dir: Path,
    target: str,
    min_confidence: float,
) -> dict[str, int]:
    """Rewrite validated.csv with Claude scores; write relevant.csv for passing patents."""
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = target.lower().replace(" ", "_")

    validated_path = output_dir / f"{slug}_validated.csv"
    with open(validated_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_VALIDATED_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(scored_rows)
    log.info(f"Rewrote {validated_path.name} with Claude scores ({len(scored_rows)} patents)")

    relevant = [
        r for r in scored_rows
        if r.get("validates") and (r.get("confidence") or 0) >= min_confidence
    ]
    relevant_path = output_dir / f"{slug}_relevant.csv"
    with open(relevant_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_VALIDATED_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(relevant)
    log.info(f"Wrote {relevant_path.name}: {len(relevant)} relevant patents (confidence >= {min_confidence})")

    return {"total": len(scored_rows), "relevant": len(relevant)}
