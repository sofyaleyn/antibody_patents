from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CSV_FIELDS = [
    "patent_number",
    "target",
    "title",
    "assignee",
    "year",
    "validates",
    "confidence",
    "reason",
    "abstract_snippet",
]


def save_csv(records: list[dict], path: Path) -> None:
    """Save records to CSV. Always CSV — easier to inspect than JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    log.info(f"Saved: {path} ({len(records)} rows)")


def load_csv(path: Path) -> list[dict]:
    """Load CSV back to list of dicts."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def print_summary(results: list[dict], min_confidence: float) -> None:
    validated = [r for r in results if r.get("validates") in (True, "True") and
                 float(r.get("confidence", 0)) >= min_confidence]
    rejected  = [r for r in results if r not in validated]

    print(f"\n{'─'*72}")
    print(f"VALIDATED ({len(validated)} patents, confidence ≥ {min_confidence})")
    print(f"{'─'*72}")
    for r in validated:
        print(f"  {r['patent_number']:20}  conf={float(r.get('confidence',0)):.2f}  "
              f"{r.get('assignee',''):<25}  {r.get('title','')[:40]}")

    print(f"\n{'─'*72}")
    print(f"REJECTED ({len(rejected)} patents)")
    print(f"{'─'*72}")
    for r in rejected:
        print(f"  {r['patent_number']:20}  conf={float(r.get('confidence',0)):.2f}  "
              f"{r.get('reason','')[:55]}")
    print()


def _parse_json_response(text: str, context: str = "") -> dict | list | None:
    """Parse JSON from Claude response. Handles markdown fences."""
    text = text.strip()

    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.startswith("```")
        ).strip()

    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        end   = text.rfind(end_char)
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue

    log.warning(f"Could not parse JSON from response ({context}): {text[:200]}")
    return None
