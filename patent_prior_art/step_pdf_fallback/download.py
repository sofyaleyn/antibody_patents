from __future__ import annotations

import logging
import re
from pathlib import Path

import requests

from patent_prior_art.step_google_patents.fetch import fetch_html

log = logging.getLogger(__name__)

# Google Patents hosts PDFs on patentimages.storage.googleapis.com
_PDF_LINK_RE = re.compile(
    r'https?://patentimages\.storage\.googleapis\.com/[^\s"\'<>]+?\.pdf',
    re.IGNORECASE,
)
REQUEST_TIMEOUT = 60


def find_pdf_url(html: str) -> str | None:
    m = _PDF_LINK_RE.search(html)
    return m.group(0) if m else None


def download_pdf(patent_number: str, output_dir: Path) -> Path:
    """Fetch the Google Patents page, extract the PDF link, download the PDF."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{patent_number}.pdf"

    html = fetch_html(patent_number)
    pdf_url = find_pdf_url(html)
    if not pdf_url:
        raise ValueError(
            f"No PDF link found in Google Patents HTML for {patent_number}. "
            f"Some older or non-US patents may not have a hosted PDF."
        )

    log.info(f"Downloading PDF: {pdf_url}")
    resp = requests.get(
        pdf_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; patent-prior-art-bot/1.0)"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    pdf_path.write_bytes(resp.content)
    log.info(f"Saved {pdf_path.name} ({len(resp.content) / 1024:.0f} KB)")
    return pdf_path
