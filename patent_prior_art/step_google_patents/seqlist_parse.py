"""Parse a USPTO sequence listing (ST.25 .txt or ST.26 .xml) into AbPatentDB-style records.

Output schema matches `_sequences.csv` written by step_abpatentdb:
    seq_id, patent_number, molecule_type, length, fasta_header,
    sequence, location, organism

`location` heuristic: scans the SEQ ID's free-text qualifiers ("note", "feature",
"product", "title") for VH / VL / heavy / light / CDR markers. Antibody patents
typically include this in <223> qualifier text.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

_VH_PAT = re.compile(r"\b(VH|heavy[-\s]?chain\s+variable|heavy\s+chain)\b", re.I)
_VL_PAT = re.compile(r"\b(VL|VK|light[-\s]?chain\s+variable|light\s+chain|kappa|lambda)\b", re.I)
_HC_PAT = re.compile(r"\b(full[\s-]?length\s+heavy|heavy\s+chain(?:\s+constant)?)\b", re.I)
_LC_PAT = re.compile(r"\bfull[\s-]?length\s+light\b", re.I)
_CDR_PAT = re.compile(r"\b([HL])CDR\s*([1-3])\b|\bCDR\s*([HL])\s*([1-3])\b", re.I)


def _classify_location(text: str) -> str:
    if not text:
        return ""
    if _CDR_PAT.search(text):
        m = _CDR_PAT.search(text)
        chain = (m.group(1) or m.group(3) or "").upper()
        num = (m.group(2) or m.group(4) or "")
        return f"{chain}CDR{num}"
    if _VH_PAT.search(text):
        return "VH"
    if _VL_PAT.search(text):
        return "VL"
    if _HC_PAT.search(text):
        return "full_heavy_chain"
    if _LC_PAT.search(text):
        return "full_light_chain"
    return ""


# ─── ST.26 (XML) ─────────────────────────────────────────────────────────────

def parse_st26(xml_bytes: bytes, patent_number: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log.warning(f"ST.26 parse error: {e}")
        return []

    out: list[dict] = []
    # ST.26 uses <SequenceData sequenceIDNumber="N"> with child <INSDSeq>
    for sd in root.iter():
        if not sd.tag.endswith("SequenceData"):
            continue
        seq_id_str = sd.get("sequenceIDNumber") or sd.get("sequenceIdNumber")
        if not seq_id_str:
            continue
        try:
            seq_id = int(seq_id_str)
        except ValueError:
            continue

        insd = next((c for c in sd.iter() if c.tag.endswith("INSDSeq")), None)
        if insd is None:
            continue

        def child_text(tag_suffix: str) -> str:
            el = next((c for c in insd.iter() if c.tag.endswith(tag_suffix)), None)
            return (el.text or "").strip() if el is not None else ""

        moltype = child_text("INSDSeq_moltype").upper()
        molecule_type = "AA" if "AA" in moltype or "PROT" in moltype else "NT"
        organism = child_text("INSDSeq_organism")
        sequence = re.sub(r"\s+", "", child_text("INSDSeq_sequence")).upper()
        if not sequence:
            continue

        # Collect free-text from feature qualifiers for location classification
        notes = []
        for q in insd.iter():
            if q.tag.endswith("INSDQualifier_value") and q.text:
                notes.append(q.text)
        title = child_text("INSDSeq_definition")
        notes.append(title)
        location = _classify_location(" ".join(notes))

        out.append({
            "seq_id": seq_id,
            "patent_number": patent_number,
            "molecule_type": molecule_type,
            "length": len(sequence),
            "fasta_header": f"patent|{patent_number}|{seq_id}|{molecule_type}",
            "sequence": sequence,
            "location": location,
            "organism": organism,
        })
    return out


# ─── ST.25 (TXT) ─────────────────────────────────────────────────────────────
# Format: numbered SEQ ID NO blocks with <210>/<212>/<213>/<223> tags, then
# the sequence (AA single-letter or NT).

_ST25_BLOCK = re.compile(r"<210>\s*(\d+)\s*(.*?)(?=<210>|\Z)", re.S)
_TAG_PAT = re.compile(r"<(\d{3})>\s*([^<]*)", re.S)


def parse_st25(text: str, patent_number: str) -> list[dict]:
    out: list[dict] = []
    for m in _ST25_BLOCK.finditer(text):
        seq_id = int(m.group(1))
        body = m.group(2)
        tags: dict[str, list[str]] = {}
        for tm in _TAG_PAT.finditer(body):
            tags.setdefault(tm.group(1), []).append(tm.group(2).strip())

        moltype = (tags.get("212", [""])[0] or "").upper()
        molecule_type = "AA" if "PRT" in moltype or "AA" in moltype else "NT"
        organism = tags.get("213", [""])[0]
        notes = " ".join(tags.get("223", []) + tags.get("221", []))

        # The actual sequence follows after the last <...> tag in the block.
        # Strip lines that look like position counters.
        last_tag_end = max((tm.end() for tm in _TAG_PAT.finditer(body)), default=0)
        seq_chunk = body[last_tag_end:]
        seq_chunk = re.sub(r"\b\d+\b", "", seq_chunk)
        seq_chunk = re.sub(r"\s+", "", seq_chunk).upper()
        # Keep only valid chars
        valid = "ACDEFGHIKLMNPQRSTVWY*X" if molecule_type == "AA" else "ACGTUNRYSWKMBDHV"
        sequence = "".join(c for c in seq_chunk if c in valid)
        if not sequence:
            continue

        location = _classify_location(notes)
        out.append({
            "seq_id": seq_id,
            "patent_number": patent_number,
            "molecule_type": molecule_type,
            "length": len(sequence),
            "fasta_header": f"patent|{patent_number}|{seq_id}|{molecule_type}",
            "sequence": sequence,
            "location": location,
            "organism": organism,
        })
    return out


def parse_seqlist(content: bytes, url: str, patent_number: str) -> list[dict]:
    """Dispatch to ST.25 or ST.26 parser based on URL/content sniffing."""
    if url.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith((".xml", ".txt")):
                    inner = zf.read(name)
                    return parse_seqlist(inner, name, patent_number)
        return []

    if url.lower().endswith(".xml") or content[:200].lstrip().startswith(b"<?xml"):
        return parse_st26(content, patent_number)

    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        text = content.decode("latin-1", errors="replace")
    return parse_st25(text, patent_number)
