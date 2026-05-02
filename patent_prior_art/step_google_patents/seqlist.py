"""Fetch the official Sequence Listing for a US patent.

Google Patents HTML pages link to the patent's sequence listing — usually a TXT
(ST.25) or XML (ST.26) file hosted either on Google's mirror
(`patentimages.storage.googleapis.com`) or directly on USPTO
(`seqdata.uspto.gov/?pageRequest=...` / `pds.uspto.gov`).

This module finds that link in the patent HTML and downloads the file. Output is
the raw text — VH/VL/CDR sequences are written in single-letter AA code with
SEQ ID NO headers, so a simple parser turns them into the AbPatentDB
`_sequences.csv` schema with no LLM call.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Patterns we expect inside a Google Patents page that point to the seq listing.
_SEQLIST_PATTERNS = [
    re.compile(
        r'https://patentimages\.storage\.googleapis\.com/[^"\'<>\s]+'
        r'(?:seqlist|sequence[-_]?listing|seql)\.(?:txt|xml|zip)',
        re.I,
    ),
    re.compile(
        r'https://(?:seqdata|pds)\.uspto\.gov/[^"\'<>\s]+',
        re.I,
    ),
    # Generic "Sequence Listing" anchor — fallback
    re.compile(
        r'href="(https?://[^"]+\.(?:txt|xml|zip))"[^>]*>\s*Sequence\s+Listing',
        re.I,
    ),
]


def find_seqlist_url(html: str) -> str | None:
    for pat in _SEQLIST_PATTERNS:
        m = pat.search(html)
        if m:
            url = m.group(1) if m.groups() else m.group(0)
            return url
    return None


def fetch_seqlist(patent_html: str, timeout: int = 60) -> tuple[str, bytes] | None:
    """Return (url, raw_bytes) for the patent's sequence listing, or None if unavailable."""
    url = find_seqlist_url(patent_html)
    if not url:
        log.info("No sequence listing link found in patent HTML")
        return None

    log.info(f"Fetching sequence listing: {url}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    if r.status_code != 200:
        log.warning(f"Sequence listing fetch failed: HTTP {r.status_code}")
        return None
    return url, r.content


def save_seqlist(content: bytes, url: str, patent_number: str, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = url.rsplit(".", 1)[-1].lower()
    if suffix not in ("txt", "xml", "zip"):
        suffix = "txt"
    path = output_dir / f"{patent_number}_seqlist.{suffix}"
    path.write_bytes(content)
    log.info(f"Wrote {path.name} ({len(content):,} bytes)")
    return path
