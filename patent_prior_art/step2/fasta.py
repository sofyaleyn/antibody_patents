from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def parse_fasta(fasta_path: Path) -> list[dict]:
    """
    Parse a multi-FASTA file into a list of dicts.

    Each dict:
        fasta_header : str   (full header line without the >)
        sequence     : str   (residues, uppercase, no whitespace)
        seq_id       : int   (extracted from header if present)
        molecule_type: "AA" or "NT" (guessed from sequence content)
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
                current_header    = line[1:]
                current_seq_lines = []
            elif line.strip():
                current_seq_lines.append(line.strip())

    flush()
    log.info(f"Parsed {len(records)} sequences from FASTA")
    return records


def _extract_seq_id_from_header(header: str) -> int | None:
    """
    Extract SEQ ID NO from a FASTA header.
    Lens.org format: lens|US_20220056133_A1|1|AA — seq_id is 3rd field.
    """
    parts = header.split("|")
    if len(parts) >= 3:
        try:
            return int(parts[2])
        except ValueError:
            pass

    match = re.search(r"\b(\d+)\b", header)
    if match:
        return int(match.group(1))
    return None


def _guess_molecule_type(sequence: str, header: str = "") -> str:
    """
    Determine if a sequence is amino acid (AA) or nucleotide (NT).

    Strategy:
    1. Check header for explicit type hint
    2. Check sequence alphabet: if >85% ACGTU → NT, else AA
    """
    header_upper = header.upper()
    if any(w in header_upper for w in ["|AA", "|PRT", "PEPTIDE", "AMINO"]):
        return "AA"
    if any(w in header_upper for w in ["|NT", "|DNA", "|RNA", "NUCLEOTIDE"]):
        return "NT"

    if not sequence:
        return "NT"

    nt_chars = set("ACGTURYSWKMBDHVN")
    nt_count = sum(1 for c in sequence.upper() if c in nt_chars)
    nt_frac  = nt_count / len(sequence)

    return "NT" if nt_frac > 0.85 else "AA"


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
    """
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
