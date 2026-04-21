from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

from patent_prior_art.utils import setup_file_logging
from .scrape import resolve_lens_id, read_sequence_table, download_fasta, scrape_sequences_individually
from .fasta import parse_fasta, _guess_molecule_type, merge_records
from .io import write_fasta_files, save_json, save_csv, print_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_step2(
    patent_number: str,
    output_dir: str | Path = ".",
    headless: bool = True,
    save_outputs: bool = True,
) -> tuple[list[dict], str, int | None]:
    """
    Full Step 2 pipeline: resolve → scrape metadata → download FASTA → merge → save.

    Args:
        patent_number:  e.g. "US20220056133A1" or "WO2020139171"
        output_dir:     where to write output files
        headless:       run Playwright browser headlessly (set False to watch it)
        save_outputs:   write JSON/CSV/FASTA files (set False in tests)

    Returns:
        (records, lens_id, total_count)
    """
    from playwright.sync_api import sync_playwright

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=headless, channel="chrome")
        except Exception:
            browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        try:
            lens_id, canonical_number = resolve_lens_id(page, patent_number)
            table_records, total_count = read_sequence_table(page, lens_id, patent_number)
            fasta_path = download_fasta(page, lens_id, output_dir, patent_number)

            if fasta_path is None or not fasta_path.exists() or fasta_path.stat().st_size == 0:
                log.warning("Bulk FASTA download failed — falling back to per-sequence scraping")

                if table_records:
                    seq_ids = [r["seq_id"] for r in table_records]
                elif total_count:
                    seq_ids = list(range(1, total_count + 1))
                else:
                    raise RuntimeError(
                        "Cannot determine sequence IDs: bulk download failed and "
                        "sequence table is empty. Try running with headless=False to debug."
                    )

                individual_seqs = scrape_sequences_individually(
                    page, lens_id, seq_ids, patent_number
                )

                fasta_records = []
                for seq_id, sequence in individual_seqs.items():
                    mol = _guess_molecule_type(sequence)
                    fasta_records.append({
                        "fasta_header":  f"patent|{patent_number}|{seq_id}|{mol}",
                        "sequence":      sequence,
                        "seq_id":        seq_id,
                        "molecule_type": mol,
                    })
            else:
                fasta_records = parse_fasta(fasta_path)

            records = merge_records(fasta_records, table_records, patent_number)

        finally:
            browser.close()

    if save_outputs and records:
        write_fasta_files(records, output_dir, patent_number)
        save_json(records, output_dir, patent_number)
        save_csv(records, output_dir, patent_number)
        print_summary(records, patent_number, lens_id)

    return records, lens_id, total_count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 2: Retrieve sequences from lens.org for a patent number"
    )
    parser.add_argument(
        "--patent",
        help="Patent number, e.g. US20220056133A1 or WO2020139171"
    )
    parser.add_argument(
        "--csv",
        help="Path to a CSV file with a 'patent_number' column to process in batch"
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="Directory for output files (default: current directory)"
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Show the browser window (useful for debugging)"
    )
    parser.add_argument(
        "--delay", type=int, default=30,
        help="Seconds to wait between patents in batch mode (default: 30)"
    )
    args = parser.parse_args()
    if not args.patent and not args.csv:
        parser.error("One of --patent or --csv is required.")
    if args.patent and args.csv:
        parser.error("--patent and --csv are mutually exclusive.")
    return args


def main() -> None:
    args = _parse_args()
    headless = not args.no_headless
    output_dir = Path(args.output_dir)
    label = f"step2_{args.patent}" if args.patent else "step2_batch"
    setup_file_logging(output_dir, label)

    if args.patent:
        records, lens_id, _ = run_step2(
            patent_number=args.patent,
            output_dir=args.output_dir,
            headless=headless,
        )
        if not records:
            log.error("No sequences retrieved.")
            sys.exit(1)
        log.info(
            f"Done. {len(records)} sequences for {args.patent} "
            f"(Lens ID: {lens_id}). "
            f"Files written to: {args.output_dir}"
        )

    else:
        with open(args.csv, newline="") as fh:
            reader = csv.DictReader(fh)
            patent_numbers = [
                row["patent_number"].strip()
                for row in reader
                if row.get("patent_number", "").strip()
            ]

        if not patent_numbers:
            log.error("No patent numbers found in CSV.")
            sys.exit(1)

        log.info(f"Batch mode: processing {len(patent_numbers)} patents from {args.csv}")
        succeeded, failed = [], []
        for i, patent in enumerate(patent_numbers):
            try:
                records, lens_id, _ = run_step2(
                    patent_number=patent,
                    output_dir=args.output_dir,
                    headless=headless,
                )
                if not records:
                    raise RuntimeError("No sequences retrieved.")
                log.info(f"[OK] {patent}: {len(records)} sequences")
                succeeded.append(patent)
            except Exception as exc:
                log.error(f"[FAIL] {patent}: {exc}")
                failed.append(patent)

            if i < len(patent_numbers) - 1:
                log.info(f"Waiting {args.delay}s before next patent...")
                time.sleep(args.delay)

        log.info(f"Batch complete. Succeeded: {len(succeeded)}, Failed: {len(failed)}")
        if failed:
            log.warning(f"Failed patents: {failed}")
            sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
