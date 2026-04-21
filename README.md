# Patent Prior Art Pipeline

Scripts to find antibody patents for a target antigen, retrieve their descriptions and sequences from lens.org, and extract SEQ ID.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

`.env` must contain:
```
ANTHROPIC_API_KEY=sk-ant-...
```

## Pipeline Steps

| Step | Script | Purpose |
|---|---|---|
| 1 | `step1_search_patents.py` | Search Google Patents, validate candidates with Claude |
| 2 | `step2_lens_to_fasta.py` | Scrape sequences from lens.org → FASTA + CSV |
| 3 | *(not yet implemented)* | Download patent PDF |
| 4 | `step4_patent_seq_extractor.py` | Extract SEQ ID descriptions from patent PDF → CSV |
| 5 | *(not yet implemented)* | Combine sequence FASTAs with description CSV |

## Usage

### Step 1 — Find and validate patents

```bash
# Search + validate patents for a target
python step1_search_patents.py --target "PD-1" --output-dir ./outputs

# Start from known patent numbers, skip search
python step1_search_patents.py --target "PD-1" --output-dir ./outputs \
    --patents US20220056133A1,US11234567B2

# Rerun validation only on existing candidates, with extended thinking
python step1_search_patents.py --target "PD-1" --output-dir ./outputs \
    --revalidate --thinking

# Lower confidence threshold to cast a wider net
python step1_search_patents.py --target "PD-1" --output-dir ./outputs \
    --min-confidence 0.6
```

Outputs to `{output-dir}/`:
- `{target}_candidates.csv` — all found patents including rejected ones (inspect + edit this)
- `{target}_validated.csv` — validated patents only; input to Step 2

### Step 2 — Retrieve sequences

```bash
# Single patent
python step2_lens_to_fasta.py --patent US20220056133A1 --output-dir ./outputs

# Batch from CSV (must have 'patent_number' column)
python step2_lens_to_fasta.py --csv outputs/pd1_validated.csv --output-dir ./outputs
```

Outputs per patent: `{patent_number}.fasta`, `{patent_number}_aa.fasta`, `{patent_number}_sequences.csv`

### Step 4 — Extract SEQ ID descriptions from PDF

```bash
python step4_patent_seq_extractor.py --pdf WO2020139171.pdf --patent WO2020139171 --target TRBV3
```

Output: `{patent_number}_seq_map.csv`

## Output CSV Fields

**Step 1 validated CSV:** `patent_number, title, assignee, year, validates, confidence, reason, target, abstract_snippet`

**Step 2 sequences CSV:** `seq_id, patent_number, molecule_type, length, fasta_header, sequence, location, organism`

**Step 4 seq map CSV:** `seq_id, patent_number, molecule_type, region, chain, is_humanized, is_parental, encodes_seq_id, variant_label, notes, confidence`

## Notes

- `molecule_type` is always `"AA"` or `"NT"`
- All scripts are importable as modules: `from step2_lens_to_fasta import run_step2`
- Batch mode (`--csv`) catches per-patent errors and continues
- Pass `--no-headless` to Step 2 to debug browser scraping
