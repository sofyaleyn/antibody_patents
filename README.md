# Patent Prior Art Pipeline

Find antibody patents for a target antigen, retrieve sequences, and extract SEQ ID → biological role mappings.

Three source paths depending on what's available:

```
Discovery          Sequences              Annotation (SEQ ID → role)
──────────         ──────────             ──────────────────────────
Step 1             AbPatentDB  ←──────┐   step_google_patents  ← Google Patents HTML (no OCR)
(Claude search     (fast, free,        │   step4               ← PDF + Claude (OCR fallback)
 or manual list)   no scraping)        │
                                       │
                   lens.org scraping ──┘  (legacy, brittle — avoid if possible)
```

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install anthropic playwright python-dotenv requests duckdb
.venv/bin/playwright install chromium   # only needed for lens.org or Playwright fallback
```

`.env` must contain:
```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Path A — AbPatentDB (fastest, no API cost)

Queries the [NaturalAntibody Patents DB](https://naturalantibody.com/patents-database/) (parquet/delta files). Replaces Steps 1 + 2 entirely for covered patents. No Claude, no scraping.

**Limitation:** DB uses UniProt gene names — `PDCD1`, not `PD-1`. If 0 results, the script prints suggestions.

```bash
python step_abpatentdb.py --target "PDCD1" --db-path ./AbPatentsDB/ --output-dir ./outputs/
python step_abpatentdb.py --target "Q15116" --db-path ./AbPatentsDB/ --output-dir ./outputs/  # UniProt ID
python step_abpatentdb.py --target "PDCD1"  --db-path ./AbPatentsDB/ --output-dir ./outputs/ --max-patents 20
```

Outputs per patent: `{patent_number}_aa.fasta`, `{patent_number}.fasta`, `{patent_number}_sequences.csv`

---

## Path B — Google Patents HTML + Sonnet (recommended for annotation)

Fetches the Google Patents page via plain HTTP (no browser automation needed — fully server-rendered). Sonnet extracts the SEQ ID → role mapping from the claims + description text. No PDF, no OCR.

**What's in the HTML:** full claims and description text (~115KB); CDR sequences sometimes written out inline (e.g. `CDR L2—STSNLAS`); SEQ ID role assignments always present.
**What's not:** full VH/VL amino acid strings — those are PDF-only. Use AbPatentDB for those.

```bash
# Single patent
python step_google_patents.py --patent US20220056133A1 --output-dir ./outputs

# Batch from CSV (must have 'patent_number' column)
python step_google_patents.py --csv outputs/pd1_validated.csv --output-dir ./outputs

# Save fetched HTML for inspection
python step_google_patents.py --patent US20220056133A1 --output-dir ./outputs --save-html
```

Output: `{patent_number}_seq_map.csv`

---

## Path C — Step 1 + Step 2: lens.org scraping (legacy, brittle)

Original pipeline. Still works but fragile — lens.org page structure changes break selectors, bulk FASTA download unreliable, fallback scraping is slow (minutes per patent).

### Step 1 — Find and validate patents

```bash
# Search Google Patents + validate with Claude Haiku
python step1_search_patents.py --target "PD-1" --output-dir ./outputs

# Skip search, validate a known list (e.g. from Perplexity Patents)
python step1_search_patents.py --target "PD-1" --output-dir ./outputs \
    --patents US20220056133A1,US11234567B2

# Rerun validation only on existing candidates
python step1_search_patents.py --target "PD-1" --output-dir ./outputs --revalidate

# Lower confidence threshold to cast a wider net
python step1_search_patents.py --target "PD-1" --output-dir ./outputs --min-confidence 0.6
```

Outputs: `{target}_candidates.csv` (all found, inspect + edit), `{target}_validated.csv` (input to Step 2)

### Step 2 — Retrieve sequences from lens.org

```bash
python step2_lens_to_fasta.py --patent US20220056133A1 --output-dir ./outputs
python step2_lens_to_fasta.py --csv outputs/pd1_validated.csv --output-dir ./outputs
python step2_lens_to_fasta.py --patent US20220056133A1 --output-dir ./outputs --no-headless  # debug
```

Outputs per patent: `{patent_number}.fasta`, `{patent_number}_aa.fasta`, `{patent_number}_sequences.csv`

---

## Path D — Step 4: PDF extraction (OCR fallback)

For patents not covered by AbPatentDB and where Google Patents HTML lacks enough context. Sends PDF to Claude Sonnet, extracts SEQ ID → role mapping. Requires manually downloaded PDF.

```bash
python step4_patent_seq_extractor.py --pdf WO2020139171.pdf --patent WO2020139171 --target TRBV3
```

Output: `{patent_number}_seq_map.csv`

---

## Output CSV Fields

| File | Fields |
|---|---|
| `{target}_candidates.csv` | `patent_number, title, assignee, year, validates, confidence, reason, target, abstract_snippet` |
| `{patent_number}_sequences.csv` | `seq_id, patent_number, molecule_type, length, fasta_header, sequence, location, organism` |
| `{patent_number}_seq_map.csv` | `seq_id, patent_number, molecule_type, sequence, region, chain, is_humanized, is_parental, encodes_seq_id, variant_label, notes, confidence` |

`molecule_type` is always `"AA"` or `"NT"`. All scripts are importable as modules.
