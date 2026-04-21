from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)


LENS_SEARCH_URL   = "https://www.lens.org/lens/search/patent/list?q={patent_number}"
LENS_PATENT_URL   = "https://www.lens.org/lens/patent/{lens_id}"
LENS_SEQ_TAB_URL  = "https://www.lens.org/lens/patent/{lens_id}/sequences"
LENS_SEQ_VIEW_URL = "https://www.lens.org/lens/patent/{lens_id}/sequences/view/{seq_id}"
LENS_FASTA_DL_URL = "https://www.lens.org/lens/patent/{lens_id}/sequences/download"

NAV_TIMEOUT   = 30_000
WAIT_TIMEOUT  = 15_000
REQUEST_DELAY = 1.0


def resolve_lens_id(page, patent_number: str) -> tuple[str, str]:
    """
    Search lens.org for a patent number and return (lens_id, canonical_number).

    lens_id is the 15-digit internal Lens identifier, e.g. "062-876-097-631-963".
    """
    search_url = LENS_SEARCH_URL.format(patent_number=patent_number)
    log.info(f"Searching lens.org for: {patent_number}")
    log.info(f"URL: {search_url}")

    page.goto(search_url, timeout=NAV_TIMEOUT)

    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except Exception:
        pass  # proceed and check for results anyway

    try:
        page.wait_for_selector("a[href*='/lens/patent/']", timeout=WAIT_TIMEOUT)
    except Exception:
        # One retry after an extra wait — lens.org can be slow to hydrate
        time.sleep(3)
        if not page.query_selector("a[href*='/lens/patent/']"):
            raise RuntimeError(
                f"No patent results found on lens.org for '{patent_number}'. "
                "Check the patent number format (e.g. US20220056133A1, WO2020139171)."
            )

    time.sleep(REQUEST_DELAY)

    links = page.query_selector_all("a[href*='/lens/patent/']")
    lens_id = None
    for link in links:
        href = link.get_attribute("href") or ""
        match = re.search(r"/lens/patent/([\d]{3}-[\d]{3}-[\d]{3}-[\d]{3}-[\d]{3})", href)
        if match:
            lens_id = match.group(1)
            break

    if not lens_id:
        first_result = page.query_selector("a[href*='/lens/patent/']")
        if first_result:
            first_result.click()
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            current_url = page.url
            match = re.search(r"/lens/patent/([\d-]+)", current_url)
            if match:
                lens_id = match.group(1)

    if not lens_id:
        raise RuntimeError(
            f"Could not extract Lens ID for patent '{patent_number}'. "
            "The patent may not be indexed on lens.org, or the page structure changed."
        )

    log.info(f"Resolved: {patent_number} → Lens ID: {lens_id}")
    return lens_id, patent_number


def read_sequence_table(page, lens_id: str, patent_number: str) -> tuple[list[dict], int | None]:
    """
    Navigate to the sequences tab and read the table of all sequences.

    Returns (records, total_count) where records is a list of metadata dicts
    (no actual sequences yet) and total_count is the count reported by the page.
    """
    seq_tab_url = LENS_SEQ_TAB_URL.format(lens_id=lens_id)
    log.info(f"Loading sequences tab: {seq_tab_url}")
    page.goto(seq_tab_url, timeout=NAV_TIMEOUT)

    try:
        page.wait_for_selector("table", timeout=WAIT_TIMEOUT)
    except Exception:
        try:
            page.wait_for_selector("[class*='sequence']", timeout=WAIT_TIMEOUT)
        except Exception:
            raise RuntimeError(
                f"Sequences tab did not load for Lens ID {lens_id}. "
                "This patent may have no sequences listed on lens.org."
            )

    time.sleep(REQUEST_DELAY)

    total_count = _extract_sequence_count(page)
    if total_count:
        log.info(f"Total sequences reported by lens.org: {total_count}")

    _scroll_to_load_all(page)

    records = _parse_sequence_table(page, patent_number)

    if not records:
        log.warning(
            "Could not parse sequence table rows. "
            "Will proceed with FASTA download only — metadata will be incomplete."
        )

    log.info(f"Read {len(records)} rows from sequence table")
    return records, total_count


def _extract_sequence_count(page) -> int | None:
    """Try to extract the total sequence count shown on the page."""
    try:
        text = page.inner_text("body")
        match = re.search(r"(\d+)\s+sequence", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


def _scroll_to_load_all(page, max_scrolls: int = 10) -> None:
    """Scroll down to trigger lazy loading of all table rows."""
    for _ in range(max_scrolls):
        prev_height = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.5)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break


def _parse_sequence_table(page, patent_number: str) -> list[dict]:
    """Parse the HTML sequence table into a list of dicts."""
    records = []

    try:
        rows_data = page.evaluate("""
            () => {
                const rows = Array.from(document.querySelectorAll('table tbody tr'));
                return rows.map(row => {
                    const cells = Array.from(row.querySelectorAll('td'));
                    return cells.map(cell => cell.innerText.trim());
                }).filter(cells => cells.length >= 3);
            }
        """)
    except Exception as e:
        log.warning(f"JS table extraction failed: {e}")
        return []

    for row in rows_data:
        if not row or not row[0]:
            continue

        try:
            seq_id_text = row[0].strip()
            seq_id_match = re.search(r"(\d+)", seq_id_text)
            if not seq_id_match:
                continue
            seq_id = int(seq_id_match.group(1))

            length_text = row[1] if len(row) > 1 else "0"
            length = int(re.search(r"(\d+)", length_text).group(1)) if re.search(r"(\d+)", length_text) else 0

            mol_text = row[2].lower() if len(row) > 2 else ""
            molecule_type = "AA" if any(w in mol_text for w in ["peptide", "protein", "amino", "aa"]) else "NT"

            location = row[3].strip() if len(row) > 3 else ""
            organism = row[4].strip() if len(row) > 4 else ""

            records.append({
                "seq_id":        seq_id,
                "patent_number": patent_number,
                "molecule_type": molecule_type,
                "length":        length,
                "location":      location,
                "organism":      organism,
            })
        except Exception as e:
            log.debug(f"Could not parse table row {row}: {e}")
            continue

    return records


def download_fasta(page, lens_id: str, output_dir, patent_number: str):
    """
    Download the bulk FASTA file for all sequences in this patent.

    Returns Path to the downloaded FASTA file, or None on failure.
    """
    from pathlib import Path
    output_dir = Path(output_dir)
    fasta_path = output_dir / f"{patent_number}.fasta"

    dl_url = LENS_FASTA_DL_URL.format(lens_id=lens_id)
    log.info(f"Attempting direct FASTA download: {dl_url}")

    try:
        seq_tab_url = LENS_SEQ_TAB_URL.format(lens_id=lens_id)
        page.goto(seq_tab_url, timeout=NAV_TIMEOUT)
        page.wait_for_selector("table, [class*='sequence']", timeout=WAIT_TIMEOUT)
        time.sleep(REQUEST_DELAY)

        with page.expect_download(timeout=30_000) as download_info:
            download_button = (
                page.query_selector("a[href*='download']")
                or page.query_selector("button:has-text('Download')")
                or page.query_selector("a:has-text('FASTA')")
                or page.query_selector("[data-testid='download']")
            )

            if download_button:
                download_button.click()
            else:
                page.goto(dl_url, timeout=NAV_TIMEOUT)

        download = download_info.value
        download.save_as(str(fasta_path))
        log.info(f"Downloaded FASTA: {fasta_path} ({fasta_path.stat().st_size:,} bytes)")
        return fasta_path

    except Exception as e:
        log.warning(f"Playwright download failed: {e}")
        log.info("Falling back to per-sequence scraping...")
        return None


def scrape_sequences_individually(
    page,
    lens_id: str,
    seq_ids: list[int],
    patent_number: str,
) -> dict[int, str]:
    """
    Fallback: visit each /sequences/view/{i} page and extract the FASTA sequence.
    Used only if the bulk download fails.
    """
    log.info(f"Scraping {len(seq_ids)} sequences individually (fallback mode)")
    sequences = {}

    for i, seq_id in enumerate(seq_ids, 1):
        url = LENS_SEQ_VIEW_URL.format(lens_id=lens_id, seq_id=seq_id)
        log.info(f"  [{i}/{len(seq_ids)}] SEQ ID {seq_id}: {url}")

        try:
            page.goto(url, timeout=NAV_TIMEOUT)
            page.wait_for_load_state("networkidle", timeout=WAIT_TIMEOUT)
            time.sleep(REQUEST_DELAY)

            fasta_text = (
                page.query_selector("pre")
                and page.query_selector("pre").inner_text()
            ) or (
                page.query_selector("code")
                and page.query_selector("code").inner_text()
            ) or ""

            if fasta_text:
                lines = fasta_text.strip().splitlines()
                seq_lines = [l for l in lines if not l.startswith(">")]
                sequences[seq_id] = "".join(seq_lines).replace(" ", "").upper()
                log.info(f"    Got {len(sequences[seq_id])} residues")
            else:
                log.warning(f"    No sequence text found for SEQ ID {seq_id}")
                sequences[seq_id] = ""

        except Exception as e:
            log.warning(f"    Failed to scrape SEQ ID {seq_id}: {e}")
            sequences[seq_id] = ""

    return sequences
