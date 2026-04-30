#!/usr/bin/env python3
"""PDF fallback: extract full VH/VL sequences from a Google Patents PDF.

Used when a patent is not covered by AbPatentDB. Writes `_sequences.csv` + `.fasta`
in the SAME FORMAT as AbPatentDB, so `step_merge.py` consumes the output unchanged.

Usage:
    # Single patent (e.g. a US patent not in AbPatentDB)
    python step_pdf_fallback.py --patent US08039594B2 --output-dir ./outputs/pdf/

    # Batch from CSV
    python step_pdf_fallback.py --csv uncovered_patents.csv --output-dir ./outputs/pdf/

    # Keep the downloaded PDF for debugging
    python step_pdf_fallback.py --patent US08039594B2 --output-dir ./outputs/pdf/ --keep-pdf
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from patent_prior_art.step_pdf_fallback import (
    download_pdf,
    extract_sequences,
    write_sequences,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_step_pdf_fallback(
    patent_number: str,
    output_dir: str | Path = ".",
    keep_pdf: bool = False,
    client: anthropic.Anthropic | None = None,
) -> list[dict]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if client is None:
        client = anthropic.Anthropic()

    pdf_path = download_pdf(patent_number, output_dir)
    try:
        seqs = extract_sequences(client, pdf_path, patent_number)
        write_sequences(seqs, patent_number, output_dir)
        return seqs
    finally:
        if not keep_pdf and pdf_path.exists():
            try:
                pdf_path.unlink()
            except OSError as e:
                log.warning(f"Could not delete {pdf_path}: {e}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PDF fallback: extract full sequences from a Google Patents PDF."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--patent", help="Patent number, e.g. US08039594B2")
    group.add_argument("--csv", dest="csv_path", metavar="CSV",
                       help="CSV with 'patent_number' column for batch mode")
    parser.add_argument("--output-dir", default=".", help="Output directory")
    parser.add_argument("--keep-pdf", action="store_true",
                        help="Keep the downloaded PDF (default: delete after extraction)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.patent:
        patents = [args.patent]
    else:
        with open(args.csv_path, newline="") as f:
            patents = [r["patent_number"] for r in csv.DictReader(f) if r.get("patent_number")]

    client = anthropic.Anthropic()
    errors = 0
    for patent in patents:
        try:
            run_step_pdf_fallback(patent, args.output_dir, args.keep_pdf, client=client)
        except Exception as e:
            log.error(f"{patent}: {e}")
            errors += 1

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
