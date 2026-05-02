"""Merge AbPatentDB sequences with Google Patents seq_map annotations."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .merge import merge_patent, find_patent_pairs

__all__ = ["merge_patent", "find_patent_pairs", "main"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge AbPatentDB sequences with Google Patents seq_map annotations."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--patent", metavar="PATENT_NUMBER",
                      help="Single patent number (use with --sequences-csv / --seq-map-csv)")
    mode.add_argument("--sequences-dir", metavar="DIR", type=Path,
                      help="Directory of *_sequences.csv files (AbPatentDB output)")

    parser.add_argument("--seq-map-dir", metavar="DIR", type=Path,
                        help="Directory of *_seq_map.csv files (Google Patents output); "
                             "required in batch mode, optional in single-patent mode")
    parser.add_argument("--sequences-csv", metavar="FILE", type=Path,
                        help="Explicit sequences CSV (single-patent mode)")
    parser.add_argument("--seq-map-csv", metavar="FILE", type=Path,
                        help="Explicit seq_map CSV (single-patent mode)")
    parser.add_argument("--output-dir", default=".", type=Path,
                        help="Output directory (default: current directory)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.patent:
        merge_patent(
            patent_number=args.patent,
            sequences_csv=args.sequences_csv,
            seq_map_csv=args.seq_map_csv,
            output_dir=args.output_dir,
        )
        return

    if not args.seq_map_dir:
        print("error: --seq-map-dir is required in batch mode", file=sys.stderr)
        sys.exit(1)

    pairs = find_patent_pairs(args.sequences_dir, args.seq_map_dir)
    if not pairs:
        log.warning("No matching patent files found.")
        sys.exit(1)

    log.info(f"Found {len(pairs)} patents to merge")
    errors = 0
    for patent_number, seq_csv, map_csv in pairs:
        try:
            merge_patent(patent_number, seq_csv, map_csv, args.output_dir)
        except Exception as e:
            log.error(f"{patent_number}: {e}")
            errors += 1

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
