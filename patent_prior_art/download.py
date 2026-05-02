"""Batch-download Google Patents HTML and/or PDF for a list of patent numbers.

Input source — pick one:
  --csv FILE        CSV with a 'patent_number' column
  --from-dir DIR    use the filename stems of *.fasta / *.csv in DIR as patent numbers
  --patents A,B,C   comma-separated list

Outputs into --output-dir:
  {patent}.html              if --html or --both
  {patent}.pdf               if --pdf or --both
  download_report.csv        per-patent status row
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

import requests

from patent_prior_art.step_google_patents.fetch import fetch_html, normalize_patent_number
from patent_prior_art.step_pdf_fallback.download import find_pdf_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60


def _patents_from_csv(csv_path: Path) -> list[str]:
    with open(csv_path, newline="") as f:
        return [r["patent_number"].strip() for r in csv.DictReader(f) if r.get("patent_number")]


def _patents_from_dir(directory: Path) -> list[str]:
    """Take the basename stem of every *.fasta/*.csv as a patent number, deduped."""
    seen: set[str] = set()
    for p in sorted(directory.iterdir()):
        if p.suffix not in (".fasta", ".csv"):
            continue
        stem = p.stem
        for suffix in ("_sequences", "_seq_map", "_merged_seq_map", "_merged"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        if stem and stem not in seen and not stem.endswith("_validated") \
           and not stem.endswith("_relevant") and not stem.endswith("_candidates"):
            seen.add(stem)
    return sorted(seen)


def download_one(
    patent_number: str,
    output_dir: Path,
    want_html: bool,
    want_pdf: bool,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized = normalize_patent_number(patent_number)
    row = {
        "patent_number": patent_number,
        "normalized":    normalized,
        "html_path":     "",
        "pdf_url":       "",
        "pdf_path":      "",
        "status":        "ok",
        "error":         "",
    }

    html: str | None = None
    if want_html or want_pdf:
        try:
            html = fetch_html(patent_number)
            if want_html:
                html_path = output_dir / f"{normalized}.html"
                html_path.write_text(html, encoding="utf-8")
                row["html_path"] = str(html_path)
                log.info(f"{patent_number}: saved {html_path.name} ({len(html):,} chars)")
        except Exception as e:
            row["status"] = "html_failed"
            row["error"] = f"html: {e}"
            log.error(f"{patent_number}: HTML fetch failed: {e}")
            return row

    if want_pdf:
        pdf_url = find_pdf_url(html or "")
        if not pdf_url:
            row["status"] = "no_pdf_link"
            row["error"] = "no PDF link in Google Patents HTML"
            log.warning(f"{patent_number}: no PDF link found")
            return row
        row["pdf_url"] = pdf_url
        try:
            resp = requests.get(
                pdf_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; patent-prior-art-bot/1.0)"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            pdf_path = output_dir / f"{normalized}.pdf"
            pdf_path.write_bytes(resp.content)
            row["pdf_path"] = str(pdf_path)
            log.info(f"{patent_number}: saved {pdf_path.name} ({len(resp.content) / 1024:.0f} KB)")
        except Exception as e:
            row["status"] = "pdf_failed"
            row["error"] = f"pdf: {e}"
            log.error(f"{patent_number}: PDF download failed: {e}")

    return row


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch download Google Patents HTML and/or PDF."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", dest="csv_path", type=Path,
                     help="CSV with 'patent_number' column")
    src.add_argument("--from-dir", type=Path,
                     help="Directory whose *.fasta/*.csv stems are patent numbers")
    src.add_argument("--patents", help="Comma-separated patent numbers")

    fmt = parser.add_mutually_exclusive_group(required=True)
    fmt.add_argument("--html", action="store_true", help="Download HTML only")
    fmt.add_argument("--pdf",  action="store_true", help="Download PDF only")
    fmt.add_argument("--both", action="store_true", help="Download both HTML and PDF")

    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="Seconds to sleep between patents (default 0.5)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.csv_path:
        patents = _patents_from_csv(args.csv_path)
    elif args.from_dir:
        patents = _patents_from_dir(args.from_dir)
    else:
        patents = [p.strip() for p in args.patents.split(",") if p.strip()]

    if not patents:
        log.error("No patent numbers found in input source.")
        sys.exit(1)

    log.info(f"Found {len(patents)} patent number(s) to download")

    want_html = args.html or args.both
    want_pdf  = args.pdf  or args.both

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "download_report.csv"

    rows: list[dict] = []
    for i, patent in enumerate(patents, 1):
        log.info(f"[{i}/{len(patents)}] {patent}")
        rows.append(download_one(patent, args.output_dir, want_html, want_pdf))
        if i < len(patents) and args.sleep > 0:
            time.sleep(args.sleep)

    fieldnames = ["patent_number", "normalized", "html_path", "pdf_url", "pdf_path", "status", "error"]
    with open(report_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    n_ok       = sum(1 for r in rows if r["status"] == "ok")
    n_no_pdf   = sum(1 for r in rows if r["status"] == "no_pdf_link")
    n_failed   = len(rows) - n_ok - n_no_pdf
    log.info(f"Done. ok={n_ok}  no_pdf_link={n_no_pdf}  failed={n_failed}")
    log.info(f"Report: {report_path}")
    if n_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
