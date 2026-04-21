from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SEQ_FIELDNAMES = [
    "seq_id", "patent_number", "molecule_type", "length",
    "fasta_header", "sequence", "location", "organism",
]


def write_fasta_files(
    records: list[dict],
    output_dir: Path,
    patent_number: str,
) -> tuple[Path, Path]:
    """
    Write two FASTA files:
      1. {patent_number}.fasta     — all sequences (AA + NT)
      2. {patent_number}_aa.fasta  — amino acid sequences only
    """
    all_path = output_dir / f"{patent_number}.fasta"
    aa_path  = output_dir / f"{patent_number}_aa.fasta"

    def fasta_entry(r: dict) -> str:
        header  = r.get("fasta_header") or f"patent|{r['patent_number']}|{r['seq_id']}|{r['molecule_type']}"
        seq     = r.get("sequence", "")
        wrapped = "\n".join(seq[i:i+60] for i in range(0, len(seq), 60))
        return f">{header}\n{wrapped}\n"

    with open(all_path, "w") as f_all, open(aa_path, "w") as f_aa:
        for r in records:
            entry = fasta_entry(r)
            f_all.write(entry)
            if r.get("molecule_type") == "AA":
                f_aa.write(entry)

    aa_count = sum(1 for r in records if r.get("molecule_type") == "AA")
    nt_count = sum(1 for r in records if r.get("molecule_type") == "NT")
    log.info(f"Wrote {all_path.name}: {len(records)} sequences ({aa_count} AA, {nt_count} NT)")
    log.info(f"Wrote {aa_path.name}: {aa_count} amino acid sequences")
    return all_path, aa_path


def save_json(records: list[dict], output_dir: Path, patent_number: str) -> Path:
    out = output_dir / f"{patent_number}_sequences.json"
    out.write_text(json.dumps(records, indent=2))
    log.info(f"Saved JSON: {out}")
    return out


def save_csv(records: list[dict], output_dir: Path, patent_number: str) -> Path:
    out = output_dir / f"{patent_number}_sequences.csv"
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_SEQ_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    log.info(f"Saved CSV:  {out}")
    return out


def print_summary(records: list[dict], patent_number: str, lens_id: str) -> None:
    aa = [r for r in records if r.get("molecule_type") == "AA"]
    nt = [r for r in records if r.get("molecule_type") == "NT"]
    print(f"\n{'─'*65}")
    print(f"SEQUENCES — {patent_number}  (Lens ID: {lens_id})")
    print(f"  Total: {len(records)}  |  AA: {len(aa)}  |  NT: {len(nt)}")
    print(f"{'─'*65}")
    print(f"{'ID':>4}  {'Type':3}  {'Length':>7}  {'Location':<20}  Header")
    print(f"{'─'*65}")
    for r in records:
        header_short = (r.get("fasta_header") or "")[:30]
        print(
            f"{r.get('seq_id','?'):>4}  "
            f"{r.get('molecule_type','?'):3}  "
            f"{r.get('length',0):>7}  "
            f"{r.get('location',''):<20}  "
            f"{header_short}"
        )
    print(f"{'─'*65}\n")
