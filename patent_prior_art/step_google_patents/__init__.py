"""Google Patents HTML → SEQ ID role mapping (Sonnet) + sequence-listing fallback."""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from .fetch import fetch_html
from .extract import extract_seq_map
from .summarize import extract_summary
from .seqlist import fetch_seqlist, save_seqlist
from .seqlist_parse import parse_seqlist
from .io import write_seq_map, write_summary, append_summary_row, write_sequences

__all__ = [
    "fetch_html",
    "extract_seq_map",
    "extract_summary",
    "fetch_seqlist",
    "save_seqlist",
    "parse_seqlist",
    "write_seq_map",
    "write_summary",
    "append_summary_row",
    "write_sequences",
    "run_step_google_patents",
    "main",
    "seqlist_main",
]

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
    summarize: bool = False,
    skip_seq_map: bool = False,
    batch_summary: bool = False,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if client is None:
        client = anthropic.Anthropic()

    html = fetch_html(patent_number, no_headless=no_headless)

    if save_html:
        html_path = output_dir / f"{patent_number}.html"
        html_path.write_text(html, encoding="utf-8")
        log.info(f"Saved HTML to {html_path}")

    result: dict = {}

    if not skip_seq_map:
        seq_map = extract_seq_map(client, html, patent_number)
        write_seq_map(seq_map, patent_number, output_dir)
        result["seq_map"] = seq_map

    if summarize:
        summary = extract_summary(client, html, patent_number)
        write_summary(summary, patent_number, output_dir)
        if batch_summary:
            append_summary_row(summary, patent_number, output_dir)
        result["summary"] = summary

    return result


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
    parser.add_argument("--summarize", action="store_true",
                        help="Also produce {patent}_summary.{json,csv} with antibody, "
                             "format, target, mutations, indication, mechanism")
    parser.add_argument("--summary-only", action="store_true",
                        help="Skip seq_map extraction; only produce summary")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    summarize = args.summarize or args.summary_only
    skip_seq_map = args.summary_only

    if args.patent:
        patents = [args.patent]
    else:
        with open(args.csv_path, newline="") as f:
            patents = [r["patent_number"] for r in csv.DictReader(f) if r.get("patent_number")]

    client = anthropic.Anthropic()
    errors = 0
    batch = len(patents) > 1
    for patent in patents:
        try:
            run_step_google_patents(
                patent,
                args.output_dir,
                args.save_html,
                args.no_headless,
                client=client,
                summarize=summarize,
                skip_seq_map=skip_seq_map,
                batch_summary=batch and summarize,
            )
        except Exception as e:
            log.error(f"{patent}: {e}")
            errors += 1

    if errors:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Sequence-listing CLI (was top-level step_seqlist.py)
# ---------------------------------------------------------------------------

_SEQLIST_FIELDS = [
    "seq_id", "patent_number", "molecule_type", "length",
    "fasta_header", "sequence", "location", "organism",
]


def run_seqlist(patent_number: str, output_dir: Path, only_ids: set[int] | None = None) -> None:
    """Fetch + parse the USPTO sequence listing for a patent.

    Writes {patent}_sequences.csv (AbPatentDB schema) and {patent}.fasta,
    plus the raw listing as {patent}_seqlist.{txt,xml,zip}.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    html = fetch_html(patent_number)
    result = fetch_seqlist(html)
    if not result:
        log.error(f"{patent_number}: no sequence listing link found")
        return
    url, content = result
    save_seqlist(content, url, patent_number, output_dir)

    records = parse_seqlist(content, url, patent_number)
    if only_ids:
        records = [r for r in records if r["seq_id"] in only_ids]
    if not records:
        log.warning(f"{patent_number}: no records parsed")
        return

    csv_path = output_dir / f"{patent_number}_sequences.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_SEQLIST_FIELDS)
        w.writeheader()
        w.writerows(records)
    log.info(f"Wrote {csv_path.name} ({len(records)} records)")

    fasta_path = output_dir / f"{patent_number}.fasta"
    with fasta_path.open("w") as f:
        for r in records:
            f.write(f">{r['fasta_header']}|{r['location']}\n{r['sequence']}\n")
    log.info(f"Wrote {fasta_path.name}")


def seqlist_main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch and parse USPTO sequence listings for one or more patents."
    )
    p.add_argument("--patent", action="append", required=True, help="Patent number (repeatable)")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--seq-ids", help="Comma-separated SEQ ID NOs to keep (e.g. '1,2,3,4')")
    args = p.parse_args()

    only = {int(x) for x in args.seq_ids.split(",")} if args.seq_ids else None
    for pn in args.patent:
        run_seqlist(pn, args.output_dir, only)


if __name__ == "__main__":
    main()
