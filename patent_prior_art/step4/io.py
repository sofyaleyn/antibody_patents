import csv
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SEQ_MAP_FIELDNAMES = [
    "seq_id", "patent_number",
    "molecule_type", "region", "type", "chain",
    "is_humanized", "is_parental",
    "encodes_seq_id", "variant_label", "numbering_scheme",
    "notes", "confidence",
]


def save_json(seq_map: list[dict], output_dir: Path, patent_number: str) -> Path:
    out = output_dir / f"{patent_number}_seq_map.json"
    out.write_text(json.dumps(seq_map, indent=2))
    log.info(f"Saved JSON: {out}")
    return out


def save_csv(seq_map: list[dict], output_dir: Path, patent_number: str) -> Path:
    """Save as CSV — flat, ready for inspection or Step 5 join."""
    out = output_dir / f"{patent_number}_seq_map.csv"
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_SEQ_MAP_FIELDNAMES, extrasaction="ignore")
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
