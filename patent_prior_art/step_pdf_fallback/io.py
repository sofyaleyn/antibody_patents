from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Identical to AbPatentDB's sequences CSV schema so step_merge consumes it unchanged.
_SEQ_FIELDNAMES = [
    "seq_id", "patent_number", "molecule_type", "length",
    "fasta_header", "sequence", "location", "organism",
]


def write_sequences(seqs: list[dict], patent_number: str, output_dir: Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for i, s in enumerate(seqs):
        sid = s.get("seq_id") or (i + 1)
        seq = (s.get("sequence") or "").strip()
        mol = s.get("molecule_type") or "AA"
        rows.append({
            "seq_id":        sid,
            "patent_number": patent_number,
            "molecule_type": mol,
            "length":        len(seq),
            "fasta_header":  f"patent|{patent_number}|{sid}|{mol}",
            "sequence":      seq,
            "location":      s.get("location") or "",
            "organism":      s.get("organism") or "",
        })

    csv_path = output_dir / f"{patent_number}_sequences.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_SEQ_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log.info(f"Wrote {csv_path.name} ({len(rows)} sequences)")

    fasta_path = output_dir / f"{patent_number}.fasta"
    with open(fasta_path, "w") as f:
        for r in rows:
            seq = r["sequence"]
            if not seq:
                continue
            wrapped = "\n".join(seq[i:i + 60] for i in range(0, len(seq), 60))
            f.write(f">{r['fasta_header']}\n{wrapped}\n")
    log.info(f"Wrote {fasta_path.name}")
