from __future__ import annotations

import json
import logging
import time

import anthropic

from .extract import extract_patent_text
from .throttle import call_with_throttle

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a bioinformatics expert specializing in antibody patent analysis.
Read patent text and produce a concise structured summary of the antibody (or antibodies)
disclosed: what it is, what it binds, what format it takes, and what it is used for.

You are familiar with:
- Antibody formats: IgG, Fab, scFv, VHH/nanobody, bispecific (BsAb), trispecific, ADC,
  CAR, diabody, minibody, Fc-fusion
- Target biology: epitopes, mutation-specific binders (e.g. p53 R175H, EGFR T790M),
  conformation-specific binders, isoform selectivity
- Therapeutic indications and mechanisms of action
- Humanization status, species of origin, parental clones"""

USER_PROMPT_TEMPLATE = """Summarize the antibody disclosed in this patent.

Patent number: {patent_number}

Return ONLY a single JSON object (no markdown, no prose) with these fields:

  "antibody_name"      : string or null  — clone/drug/internal name (e.g. "hz1H7", "pembrolizumab")
  "format"             : string or null  — "IgG", "IgG1", "IgG4", "Fab", "scFv", "VHH/nanobody",
                                            "bispecific", "trispecific", "ADC", "Fc-fusion",
                                            "CAR", "diabody", or other if specified
  "is_bispecific"      : true or false
  "is_nanobody"        : true or false   — true for VHH / single-domain
  "is_single_chain"    : true or false   — true for scFv (VH and VL joined by a flexible
                                            linker into a single polypeptide chain)
  "is_humanized"       : true or false
  "species_of_origin"  : string or null  — "mouse", "rabbit", "llama", "human", "rat", etc.
  "target"             : string or null  — primary antigen (e.g. "p53", "PD-1", "HER2")
  "secondary_targets"  : array of strings — for bispecifics; [] otherwise
  "target_mutations"   : array of strings — specific mutations or variants the antibody binds
                                             (e.g. ["R175H", "Y220C"]); [] if pan-reactive
  "epitope"            : string or null  — described epitope or binding region
  "indication"         : string or null  — disease / therapeutic area (e.g. "non-small cell
                                             lung cancer", "autoimmune disease")
  "mechanism"          : string or null  — MoA (e.g. "checkpoint inhibition", "ADCC",
                                             "neutralization", "agonist")
  "use"                : string or null  — diagnostic, therapeutic, research, imaging
  "summary"            : string          — 2-4 sentence plain-English summary
  "confidence"         : float 0.0–1.0

If the patent discloses multiple distinct antibody families, summarize the principal one
and mention the others in "summary".

─────────────────────────────────────────────
PATENT TEXT
─────────────────────────────────────────────

{patent_text}"""


def extract_summary(
    client: anthropic.Anthropic,
    html: str,
    patent_number: str,
    model: str = MODEL,
) -> dict:
    patent_text = extract_patent_text(html)
    log.info(f"Patent text length for summary: {len(patent_text):,} chars")

    content = [
        {
            "type": "text",
            "text": USER_PROMPT_TEMPLATE.format(
                patent_number=patent_number,
                patent_text=patent_text,
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    est_tokens = max(1000, len(patent_text) // 4 + 500)

    for attempt in range(1, 3):
        log.info(f"Calling {model} (summary), attempt {attempt}/2 (~{est_tokens:,} input tokens)")
        try:
            response = call_with_throttle(
                client,
                estimated_input_tokens=est_tokens,
                model=model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as e:
            log.error(f"API error: {e}")
            if attempt == 2:
                raise
            time.sleep(5)
            continue

        raw = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not raw:
            continue

        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            continue
        try:
            obj = json.loads(raw[start: end + 1])
            if isinstance(obj, dict):
                obj["patent_number"] = patent_number
                return obj
        except json.JSONDecodeError as e:
            log.warning(f"Summary JSON parse failed: {e}")
            content = content + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": (
                    "Your response could not be parsed as valid JSON. "
                    "Return ONLY the corrected JSON object, starting with { and ending with }."
                )},
            ]

    return {"patent_number": patent_number}
