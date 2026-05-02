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


def write_sequences(records: list[dict], patent_number: str, output_dir: Path) -> None:
    """Write {patent}_sequences.csv + {patent}.fasta in AbPatentDB schema."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not records:
        log.info(f"No sequence-listing records for {patent_number}")
        return

    csv_path = output_dir / f"{patent_number}_sequences.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_SEQ_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    log.info(f"Wrote {csv_path.name} ({len(records)} sequences)")

    fasta_path = output_dir / f"{patent_number}.fasta"
    with open(fasta_path, "w") as f:
        for r in records:
            seq = r.get("sequence", "")
            wrapped = "\n".join(seq[i:i+60] for i in range(0, len(seq), 60))
            f.write(f">{r['fasta_header']}\n{wrapped}\n")
    log.info(f"Wrote {fasta_path.name}")


_MAP_FIELDNAMES = [
    "seq_id", "patent_number", "molecule_type", "sequence",
    "region", "chain", "is_humanized", "is_parental",
    "encodes_seq_id", "variant_label", "numbering_scheme",
    "notes", "confidence",
]


def write_seq_map(seq_map: list[dict], patent_number: str, output_dir: Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = [{**r, "patent_number": patent_number} for r in seq_map]

    csv_path = output_dir / f"{patent_number}_seq_map.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_MAP_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log.info(f"Wrote {csv_path.name} ({len(rows)} records)")

    json_path = output_dir / f"{patent_number}_seq_map.json"
    with open(json_path, "w") as f:
        json.dump(rows, f, indent=2)
    log.info(f"Wrote {json_path.name}")


_SUMMARY_FIELDNAMES = [
    "patent_number", "antibody_name", "format", "is_bispecific", "is_nanobody",
    "is_single_chain", "is_humanized", "species_of_origin", "target",
    "secondary_targets", "target_mutations", "epitope", "indication",
    "mechanism", "use", "summary", "confidence",
]

SUMMARY_SUBDIR = "google_patent_html_summary"


def write_summary(summary: dict, patent_number: str, output_dir: Path) -> None:
    output_dir = Path(output_dir) / SUMMARY_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"{patent_number}_summary.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)
    log.info(f"Wrote {json_path.name}")

    row = {**summary, "patent_number": patent_number}
    for k in ("secondary_targets", "target_mutations"):
        v = row.get(k)
        if isinstance(v, list):
            row[k] = "; ".join(str(x) for x in v)

    csv_path = output_dir / f"{patent_number}_summary.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_SUMMARY_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerow(row)
    log.info(f"Wrote {csv_path.name}")


def append_summary_row(summary: dict, patent_number: str, output_dir: Path) -> None:
    """Append a row to a combined summaries.csv across a batch run."""
    output_dir = Path(output_dir) / SUMMARY_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "summaries.csv"
    row = {**summary, "patent_number": patent_number}
    for k in ("secondary_targets", "target_mutations"):
        v = row.get(k)
        if isinstance(v, list):
            row[k] = "; ".join(str(x) for x in v)

    write_header = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_SUMMARY_FIELDNAMES, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)
