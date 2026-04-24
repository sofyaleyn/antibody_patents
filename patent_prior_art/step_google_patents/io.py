from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

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
