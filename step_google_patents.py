#!/usr/bin/env python3
"""Fetch Google Patents HTML → extract SEQ ID role mapping via Sonnet."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from patent_prior_art.step_google_patents.fetch import fetch_html
from patent_prior_art.step_google_patents.extract import extract_seq_map
from patent_prior_art.step_google_patents.io import write_seq_map

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_step_google_patents(
    patent_number: str,
    output_dir: str | Path = ".",
    save_html: bool = False,
    no_headless: bool = False,
    client: anthropic.Anthropic | None = None,
) -> list[dict]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if client is None:
        client = anthropic.Anthropic()

    html = fetch_html(patent_number, no_headless=no_headless)

    if save_html:
        html_path = output_dir / f"{patent_number}.html"
        html_path.write_text(html, encoding="utf-8")
        log.info(f"Saved HTML to {html_path}")

    seq_map = extract_seq_map(client, html, patent_number)
    write_seq_map(seq_map, patent_number, output_dir)
    return seq_map


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Google Patents HTML and extract SEQ ID role mapping."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--patent", help="Patent number, e.g. US20220056133A1")
    group.add_argument("--csv", dest="csv_path", metavar="CSV",
                       help="CSV with 'patent_number' column for batch mode")
    parser.add_argument("--output-dir", default=".", help="Output directory")
    parser.add_argument("--save-html", action="store_true",
                        help="Save fetched HTML for inspection/debugging")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show Playwright browser window (debug)")
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
            run_step_google_patents(
                patent, args.output_dir, args.save_html, args.no_headless, client=client
            )
        except Exception as e:
            log.error(f"{patent}: {e}")
            errors += 1

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
