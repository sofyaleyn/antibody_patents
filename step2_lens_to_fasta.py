"""
step2_lens_to_fasta.py
──────────────────────
Given a patent number (e.g. US20220056133A1), this script:

  1. Resolves the patent on lens.org and finds its internal Lens ID
  2. Navigates to the sequences tab and reads the sequence table
     (seq_id, length, molecule_type, location, organism)
  3. Downloads the bulk FASTA file for all sequences in one request
  4. Parses the FASTA into a list of dicts — one per sequence
  5. Writes:
       {patent_number}.fasta          — all sequences (AA + NT)
       {patent_number}_aa.fasta       — amino acid sequences only
       {patent_number}_sequences.json — list of dicts for Step 5 join
       {patent_number}_sequences.csv  — same as CSV for inspection

Usage (standalone):
    python step2_lens_to_fasta.py --patent US20220056133A1 --output-dir ./outputs

Usage (as module):
    from step2_lens_to_fasta import run_step2
    records = run_step2("US20220056133A1", output_dir="./outputs")

Requirements:
    pip install playwright python-dotenv
    playwright install chromium

Each record in the returned list has:
    seq_id        : int
    patent_number : str
    molecule_type : "AA" or "NT"
    length        : int
    fasta_header  : str   (full >header line from FASTA)
    sequence      : str   (amino acid or nucleotide string, no spaces/newlines)
    location      : str   (claims / description / example / undetermined)
    organism      : str   (declared organism or "")
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

LENS_SEARCH_URL   = "https://www.lens.org/lens/search/patent/list?q={patent_number}"
LENS_PATENT_URL   = "https://www.lens.org/lens/patent/{lens_id}"
LENS_SEQ_TAB_URL  = "https://www.lens.org/lens/patent/{lens_id}/sequences"
LENS_SEQ_VIEW_URL = "https://www.lens.org/lens/patent/{lens_id}/sequences/view/{seq_id}"

# Bulk FASTA download URL — downloads all sequences for a patent as one file
LENS_FASTA_DL_URL = "https://www.lens.org/lens/patent/{lens_id}/sequences/download"

# Playwright timeouts (ms)
NAV_TIMEOUT  = 30_000
WAIT_TIMEOUT = 15_000

# Polite delay between requests (seconds)
REQUEST_DELAY = 1.0


# ─────────────────────────────────────────────
# Step 2a — Resolve patent number → Lens ID
# ─────────────────────────────────────────────

def resolve_lens_id(page, patent_number: str) -> tuple[str, str]:
    """
    Search lens.org for a patent number and return (lens_id, canonical_number).

    lens_id is the 15-digit internal Lens identifier, e.g. "062-876-097-631-963".
    It appears in the URL of the individual patent page.

    Args:
        page:           Playwright page object
        patent_number:  e.g. "US20220056133A1" or "WO2020139171"

    Returns:
        (lens_id, canonical_number)
    """
    search_url = LENS_SEARCH_URL.format(patent_number=patent_number)
    log.info(f"Searching lens.org for: {patent_number}")
    log.info(f"URL: {search_url}")

    page.goto(search_url, timeout=NAV_TIMEOUT)

    # Wait for search results to load (React renders them asynchronously)
    try:
        page.wait_for_selector("a[href*='/lens/patent/']", timeout=WAIT_TIMEOUT)
    except Exception:
        raise RuntimeError(
            f"No patent results found on lens.org for '{patent_number}'. "
            "Check the patent number format (e.g. US20220056133A1, WO2020139171)."
        )

    time.sleep(REQUEST_DELAY)

    # Find the first patent result link — extract the Lens ID from its href
    # Lens patent URLs look like: /lens/patent/062-876-097-631-963
    links = page.query_selector_all("a[href*='/lens/patent/']")
    lens_id = None
    for link in links:
        href = link.get_attribute("href") or ""
        # Match the 15-digit Lens ID pattern (groups of digits separated by dashes)
        match = re.search(r"/lens/patent/([\d]{3}-[\d]{3}-[\d]{3}-[\d]{3}-[\d]{3})", href)
        if match:
            lens_id = match.group(1)
            break

    if not lens_id:
        # Try alternative: click first result and read URL from navigation
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


# ─────────────────────────────────────────────
# Step 2b — Read sequence table from sequences tab
# ─────────────────────────────────────────────

def read_sequence_table(page, lens_id: str, patent_number: str) -> list[dict]:
    """
    Navigate to the sequences tab and read the table of all sequences.
    Returns a list of dicts with metadata for each SEQ ID NO (no actual sequences yet).

    Each dict:
        seq_id        : int
        patent_number : str
        molecule_type : "AA" or "NT"
        length        : int
        location      : str
        organism      : str
    """
    seq_tab_url = LENS_SEQ_TAB_URL.format(lens_id=lens_id)
    log.info(f"Loading sequences tab: {seq_tab_url}")
    page.goto(seq_tab_url, timeout=NAV_TIMEOUT)

    # Wait for the sequence table to render
    try:
        page.wait_for_selector("table", timeout=WAIT_TIMEOUT)
    except Exception:
        # Some patents have sequences but render differently — try waiting longer
        try:
            page.wait_for_selector("[class*='sequence']", timeout=WAIT_TIMEOUT)
        except Exception:
            raise RuntimeError(
                f"Sequences tab did not load for Lens ID {lens_id}. "
                "This patent may have no sequences listed on lens.org."
            )

    time.sleep(REQUEST_DELAY)

    # Try to find total sequence count from the page header
    total_count = _extract_sequence_count(page)
    if total_count:
        log.info(f"Total sequences reported by lens.org: {total_count}")

    # Scroll to load all rows (lens.org may paginate or lazy-load)
    _scroll_to_load_all(page)

    # Extract table rows
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
    # Look for text like "22 sequences" or "Sequences (22)"
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
    for i in range(max_scrolls):
        prev_height = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.5)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break


def _parse_sequence_table(page, patent_number: str) -> list[dict]:
    """
    Parse the HTML sequence table into a list of dicts.
    Handles variation in lens.org table structure.
    """
    records = []

    # Try to get table rows via JavaScript for reliability
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

        # Column order on lens.org sequences tab:
        # SEQ ID NO | Length | Sequence Type | Location | Declared Organism | ...
        try:
            seq_id_text = row[0].strip()
            # seq_id may be just a number, or "SEQ ID NO: 1"
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


# ─────────────────────────────────────────────
# Step 2c — Download bulk FASTA
# ─────────────────────────────────────────────

def download_fasta(page, lens_id: str, output_dir: Path, patent_number: str) -> Path:
    """
    Download the bulk FASTA file for all sequences in this patent.

    Lens.org provides a download button on the sequences tab that returns
    a multi-FASTA file. We trigger this download via Playwright and save it.

    Returns:
        Path to the downloaded FASTA file.
    """
    fasta_path = output_dir / f"{patent_number}.fasta"

    # Approach 1: Direct URL download (works if lens.org serves it as a plain URL)
    dl_url = LENS_FASTA_DL_URL.format(lens_id=lens_id)
    log.info(f"Attempting direct FASTA download: {dl_url}")

    try:
        # Use Playwright's download interception
        seq_tab_url = LENS_SEQ_TAB_URL.format(lens_id=lens_id)
        page.goto(seq_tab_url, timeout=NAV_TIMEOUT)
        page.wait_for_selector("table, [class*='sequence']", timeout=WAIT_TIMEOUT)
        time.sleep(REQUEST_DELAY)

        # Look for the download button and click it
        with page.expect_download(timeout=30_000) as download_info:
            # Try various selectors for the download button
            download_button = (
                page.query_selector("a[href*='download']")
                or page.query_selector("button:has-text('Download')")
                or page.query_selector("a:has-text('FASTA')")
                or page.query_selector("[data-testid='download']")
            )

            if download_button:
                download_button.click()
            else:
                # Fallback: navigate directly to the download URL
                page.goto(dl_url, timeout=NAV_TIMEOUT)

        download = download_info.value
        download.save_as(str(fasta_path))
        log.info(f"Downloaded FASTA: {fasta_path} ({fasta_path.stat().st_size:,} bytes)")
        return fasta_path

    except Exception as e:
        log.warning(f"Playwright download failed: {e}")
        log.info("Falling back to per-sequence scraping...")
        return None


# ─────────────────────────────────────────────
# Step 2d — Fallback: scrape each sequence page individually
# ─────────────────────────────────────────────

def scrape_sequences_individually(
    page,
    lens_id: str,
    seq_ids: list[int],
    patent_number: str,
) -> dict[int, str]:
    """
    Fallback: visit each /sequences/view/{i} page and extract the FASTA sequence.
    Used only if the bulk download fails.

    Returns:
        dict mapping seq_id → fasta_sequence_string (no header, just residues)
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

            # Extract FASTA text — it appears in a <pre> or <code> block
            fasta_text = (
                page.query_selector("pre")
                and page.query_selector("pre").inner_text()
            ) or (
                page.query_selector("code")
                and page.query_selector("code").inner_text()
            ) or ""

            if fasta_text:
                # Strip the header line, join sequence lines
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


# ─────────────────────────────────────────────
# Step 2e — Parse FASTA file
# ─────────────────────────────────────────────

def parse_fasta(fasta_path: Path) -> list[dict]:
    """
    Parse a multi-FASTA file into a list of dicts.

    Each dict:
        fasta_header : str   (full header line without the >)
        sequence     : str   (residues, uppercase, no whitespace)
        seq_id       : int   (extracted from header if present)
        molecule_type: "AA" or "NT" (guessed from sequence content)

    Lens.org FASTA headers look like:
        >lens|US_20220056133_A1|1|AA  or  >lens|...|1|PRT  or similar
    """
    records = []
    current_header = None
    current_seq_lines = []

    def flush():
        if current_header is not None:
            seq = "".join(current_seq_lines).replace(" ", "").upper()
            seq_id = _extract_seq_id_from_header(current_header)
            mol    = _guess_molecule_type(seq, current_header)
            records.append({
                "fasta_header":  current_header,
                "sequence":      seq,
                "seq_id":        seq_id,
                "molecule_type": mol,
            })

    with open(fasta_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith(">"):
                flush()
                current_header    = line[1:]  # strip the >
                current_seq_lines = []
            elif line.strip():
                current_seq_lines.append(line.strip())

    flush()
    log.info(f"Parsed {len(records)} sequences from FASTA")
    return records


def _extract_seq_id_from_header(header: str) -> int | None:
    """
    Extract SEQ ID NO from a FASTA header.

    Lens.org format: lens|US_20220056133_A1|1|AA
                                            ^ this is the seq_id
    Fallback: find first standalone integer.
    """
    # Lens format: pipe-delimited, seq_id is 3rd field
    parts = header.split("|")
    if len(parts) >= 3:
        try:
            return int(parts[2])
        except ValueError:
            pass

    # Fallback: regex
    match = re.search(r"\b(\d+)\b", header)
    if match:
        return int(match.group(1))
    return None


def _guess_molecule_type(sequence: str, header: str = "") -> str:
    """
    Determine if a sequence is amino acid (AA) or nucleotide (NT).

    Strategy:
    1. Check header for explicit type hint (AA, PRT, NT, DNA, RNA, nucleotide, peptide)
    2. Check sequence alphabet: if >85% ACGTU → NT, else AA
    """
    header_upper = header.upper()
    if any(w in header_upper for w in ["|AA", "|PRT", "PEPTIDE", "AMINO"]):
        return "AA"
    if any(w in header_upper for w in ["|NT", "|DNA", "|RNA", "NUCLEOTIDE"]):
        return "NT"

    if not sequence:
        return "NT"

    nt_chars  = set("ACGTURYSWKMBDHVN")
    nt_count  = sum(1 for c in sequence.upper() if c in nt_chars)
    nt_frac   = nt_count / len(sequence)

    return "NT" if nt_frac > 0.85 else "AA"


# ─────────────────────────────────────────────
# Step 2f — Merge table metadata + FASTA sequences
# ─────────────────────────────────────────────

def merge_records(
    fasta_records: list[dict],
    table_records: list[dict],
    patent_number: str,
) -> list[dict]:
    """
    Merge FASTA sequences with sequence table metadata by seq_id.

    FASTA is the source of truth for the actual sequence.
    Table metadata adds: location, organism, length.
    molecule_type from table overrides the guessed value from FASTA if available.

    Returns list of complete records sorted by seq_id.
    """
    # Index table records by seq_id
    table_by_id = {r["seq_id"]: r for r in table_records}

    merged = []
    for fr in fasta_records:
        seq_id = fr.get("seq_id")
        tr     = table_by_id.get(seq_id, {})

        record = {
            "seq_id":        seq_id,
            "patent_number": patent_number,
            "molecule_type": tr.get("molecule_type") or fr.get("molecule_type", "NT"),
            "length":        tr.get("length") or len(fr.get("sequence", "")),
            "fasta_header":  fr.get("fasta_header", ""),
            "sequence":      fr.get("sequence", ""),
            "location":      tr.get("location", ""),
            "organism":      tr.get("organism", ""),
        }
        merged.append(record)

    # Add any table records that didn't appear in FASTA (shouldn't happen normally)
    fasta_ids = {r.get("seq_id") for r in fasta_records}
    for seq_id, tr in table_by_id.items():
        if seq_id not in fasta_ids:
            log.warning(f"SEQ ID {seq_id} in table but not in FASTA — adding with empty sequence")
            merged.append({
                "seq_id":        seq_id,
                "patent_number": patent_number,
                "molecule_type": tr.get("molecule_type", "NT"),
                "length":        tr.get("length", 0),
                "fasta_header":  "",
                "sequence":      "",
                "location":      tr.get("location", ""),
                "organism":      tr.get("organism", ""),
            })

    merged.sort(key=lambda r: r.get("seq_id") or 0)
    log.info(f"Merged {len(merged)} records (FASTA + table metadata)")
    return merged


# ─────────────────────────────────────────────
# Step 3 (folded in) — Write FASTA files
# ─────────────────────────────────────────────

def write_fasta_files(
    records: list[dict],
    output_dir: Path,
    patent_number: str,
) -> tuple[Path, Path]:
    """
    Write two FASTA files:
      1. {patent_number}.fasta     — all sequences (AA + NT)
      2. {patent_number}_aa.fasta  — amino acid sequences only

    Returns (all_fasta_path, aa_fasta_path).
    """
    all_path = output_dir / f"{patent_number}.fasta"
    aa_path  = output_dir / f"{patent_number}_aa.fasta"

    def fasta_entry(r: dict) -> str:
        header = r.get("fasta_header") or f"patent|{r['patent_number']}|{r['seq_id']}|{r['molecule_type']}"
        seq    = r.get("sequence", "")
        # Wrap sequence at 60 chars per line (standard FASTA)
        wrapped = "\n".join(seq[i:i+60] for i in range(0, len(seq), 60))
        return f">{header}\n{wrapped}\n"

    with open(all_path, "w") as f_all, open(aa_path, "w") as f_aa:
        for r in records:
            entry = fasta_entry(r)
            f_all.write(entry)
            if r.get("molecule_type") == "AA":
                f_aa.write(entry)

    aa_count  = sum(1 for r in records if r.get("molecule_type") == "AA")
    nt_count  = sum(1 for r in records if r.get("molecule_type") == "NT")
    log.info(f"Wrote {all_path.name}: {len(records)} sequences ({aa_count} AA, {nt_count} NT)")
    log.info(f"Wrote {aa_path.name}: {aa_count} amino acid sequences")
    return all_path, aa_path


# ─────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────

def save_json(records: list[dict], output_dir: Path, patent_number: str) -> Path:
    out = output_dir / f"{patent_number}_sequences.json"
    out.write_text(json.dumps(records, indent=2))
    log.info(f"Saved JSON: {out}")
    return out


def save_csv(records: list[dict], output_dir: Path, patent_number: str) -> Path:
    fieldnames = [
        "seq_id", "patent_number", "molecule_type", "length",
        "fasta_header", "sequence", "location", "organism",
    ]
    out = output_dir / f"{patent_number}_sequences.csv"
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    log.info(f"Saved CSV:  {out}")
    return out


def print_summary(records: list[dict], patent_number: str, lens_id: str) -> None:
    aa  = [r for r in records if r.get("molecule_type") == "AA"]
    nt  = [r for r in records if r.get("molecule_type") == "NT"]
    print(f"\n{'─'*65}")
    print(f"SEQUENCES — {patent_number}  (Lens ID: {lens_id})")
    print(f"  Total: {len(records)}  |  AA: {len(aa)}  |  NT: {len(nt)}")
    print(f"{'─'*65}")
    print(f"{'ID':>4}  {'Type':3}  {'Length':>7}  {'Location':<20}  Header")
    print(f"{'─'*65}")
    for r in records:
        header_short = (r.get("fasta_header") or "")[:30]
        print(
            f"{r.get('seq_id','?'):>4}  "
            f"{r.get('molecule_type','?'):3}  "
            f"{r.get('length',0):>7}  "
            f"{r.get('location',''):< 20}  "
            f"{header_short}"
        )
    print(f"{'─'*65}\n")


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

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
        records:     list of dicts — one per SEQ ID NO, with sequence + metadata
        lens_id:     the resolved Lens internal ID
        total_count: sequence count reported by lens.org (or None if undetected)
    """
    from playwright.sync_api import sync_playwright

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            # Accept downloads so the bulk FASTA download works
            accept_downloads=True,
        )
        page = context.new_page()

        try:
            # ── 2a: Resolve Lens ID ───────────────────────────────────────────
            lens_id, canonical_number = resolve_lens_id(page, patent_number)

            # ── 2b: Read sequence table ───────────────────────────────────────
            table_records, total_count = read_sequence_table(page, lens_id, patent_number)

            # ── 2c: Download bulk FASTA ───────────────────────────────────────
            fasta_path = download_fasta(page, lens_id, output_dir, patent_number)

            # ── 2d (fallback): scrape individually if bulk download failed ────
            if fasta_path is None or not fasta_path.exists() or fasta_path.stat().st_size == 0:
                log.warning("Bulk FASTA download failed — falling back to per-sequence scraping")

                # Use seq_ids from table if available; otherwise try 1..total_count
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

                # Build fasta_records from individually scraped sequences
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
                # ── 2e: Parse downloaded FASTA ────────────────────────────────
                fasta_records = parse_fasta(fasta_path)

            # ── 2f: Merge FASTA + table metadata ─────────────────────────────
            records = merge_records(fasta_records, table_records, patent_number)

        finally:
            browser.close()

    # ── Step 3 (folded in): Write FASTA files ────────────────────────────────
    if save_outputs and records:
        write_fasta_files(records, output_dir, patent_number)
        save_json(records, output_dir, patent_number)
        save_csv(records, output_dir, patent_number)
        print_summary(records, patent_number, lens_id)

    return records, lens_id, total_count


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

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
            patent_numbers = [row["patent_number"].strip() for row in reader if row.get("patent_number", "").strip()]

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
