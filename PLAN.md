# Plan: Repo Salvage — Replace Scraping/PDF with Google Patents HTML + Sonnet

## Context

The current pipeline has three major pain points:
- **Step 2 (lens.org Playwright scraping)** is the weakest link — brittle CSS selectors, bulk FASTA download unreliable, slow fallback (minutes per patent)
- **Step 4 (PDF extraction)** is expensive and OCR-dependent — Claude paying to read scanned PDFs, fragile JSON parsing, auto-escalation to Opus
- **Step 3 (PDF download)** was never implemented

User found that Google Patents often has full HTML text (no OCR needed), and Sonnet can extract sequences + annotations from that HTML cleanly and cheaply. Perplexity Patents also gives better discovery results than the current Claude web_search loop.

**The repo is worth saving.** The output conventions (FASTA, CSV schemas), CLI patterns, batch processing, AbPatentDB fast path, and Step 1 validation logic are all good. What changes is the input method for Steps 2–4.

---

## What to Keep As-Is

| Component | Why |
|---|---|
| `step_abpatentdb.py` + module | Best fast path for covered patents; no cost, no scraping |
| Output file conventions | `{patent_number}_aa.fasta`, `_sequences.csv`, `_seq_map.csv` |
| Batch mode pattern | Per-patent try/except + continue; same across all steps |
| Step 1 Phase C validation | Claude Haiku per-patent confirmation is still useful |
| CLI patterns (`argparse`, `--output-dir`) | Consistent across all scripts |

## What to Replace

**Drop**: Steps 2 + 3 + 4 as separate scripts.

**Replace with**: A single new script `step_google_patents.py` + module `patent_prior_art/step_google_patents/`.

---

## New Script: `step_google_patents.py`

**Confirmed findings (2026-04-21):** Google Patents HTML is fully server-rendered — plain `requests` works, no Playwright needed (~350–500KB). HTML contains CDR sequences as clean single-letter AA strings inline in claims/description (e.g. `CDR L2—STSNLAS (amino acid residues L50-L56) (SEQ ID NO: 13)`). Full VH/VL strings are not in the HTML.

**Two-source architecture with fallback:**

| Output | Primary | Fallback |
|---|---|---|
| CDR sequences + `_seq_map.csv` | Google Patents HTML → Sonnet | — |
| Full VH/VL sequences + `.fasta` | AbPatentDB parquet (fast, free) | PDF text extraction |

**What it does** (single pass per patent):
1. Fetch `https://patents.google.com/patent/{patent_number}/en` via `requests` (Playwright fallback if blocked)
2. Pass full HTML to Sonnet (`cache_control: ephemeral`)
3. Sonnet extracts CDR sequences + SEQ ID role mapping from claims + description
4. Output: `_seq_map.csv` with inline CDR sequences where present; full sequences merged from AbPatentDB

**Usage (mirrors current CLI conventions):**
```bash
# Single patent
python step_google_patents.py --patent US20220056133A1 --output-dir ./outputs

# Batch from CSV (same --csv interface as step2)
python step_google_patents.py --csv validated_patents.csv --output-dir ./outputs

# Debug: save fetched HTML for inspection
python step_google_patents.py --patent US20220056133A1 --output-dir ./outputs --save-html
```

**Output files** (same conventions as today):
- `{patent_number}_aa.fasta`
- `{patent_number}.fasta`
- `{patent_number}_sequences.csv` — fields: `seq_id, patent_number, molecule_type, length, fasta_header, sequence, location, organism`
- `{patent_number}_seq_map.csv` — fields: `seq_id, patent_number, molecule_type, region, chain, is_humanized, is_parental, encodes_seq_id, variant_label, notes, confidence`

---

## Step 1: Minimal Change

Keep `step1_search_patents.py` as-is. Add a `--patents` flag (already in CLAUDE.md design) so user can paste Perplexity results directly and skip Phase A entirely:
```bash
# Skip search, just validate a known list
python step1_search_patents.py --target "PD-1" \
    --patents US20220056133A1,US20210032333A1 --output-dir ./outputs
```

If Perplexity has an API and the user wants to wire it in later, that's a separate step.

---

## Module Structure

```
patent_prior_art/step_google_patents/
    __init__.py        # exports run_step_google_patents()
    fetch.py           # fetch_html(patent_number) → html str; tries requests, falls back to Playwright
    extract.py         # claude_extract(html, patent_number) → sequences + seq_map
    io.py              # write FASTA + CSVs (reuse patterns from step2/io.py)
step_google_patents.py # CLI entrypoint
```

---

## First Concrete Step Before Coding

Before writing the module, do a quick feasibility check:
```bash
# Can we fetch Google Patents HTML without Playwright?
curl -A "Mozilla/5.0" "https://patents.google.com/patent/US20220056133A1/en" | head -200
# If HTML has sequence listing content → requests is enough
# If mostly empty JS scaffold → Playwright needed
```

Also check whether the HTML includes the full sequence listing (often in a `<section id="claims">` block or `<div class="patent-text">`).

---

## Critical Files

- `patent_prior_art/step2/scrape.py` — patterns to borrow for Playwright fallback (already handles headless/no-headless flag)
- `patent_prior_art/step2/io.py` — reuse FASTA writer and CSV writer
- `patent_prior_art/step4/extract.py` — Sonnet prompt structure to adapt (switch from `document` PDF type to plain HTML text)
- `step2_lens_to_fasta.py` — CLI structure to mirror

---

## Verification

1. Run `step_google_patents.py` on one known patent (`US20220056133A1`) — compare sequence count + SEQ IDs against current step2 output
2. Check that `_sequences.csv` schema is identical (step4 and step5 depend on it)
3. Run batch mode on a 5-patent CSV — confirm per-patent error isolation works
4. Optionally: run `step1` with `--patents` on a manually curated list to confirm bypass works
