# Patent Prior Art Pipeline

Find antibody patents for a target antigen, retrieve sequences, and extract SEQ ID → biological role mappings.

```
Discovery                Sequences                Annotation (SEQ ID → role)
──────────               ──────────               ──────────────────────────
patent-search            patent-abdb              patent-gp
(Claude search           (NaturalAntibody         (Google Patents HTML
 + validation)            Patents DB,              → Sonnet)
                          parquet, free)
                                                  patent-pdf
                                                  (PDF + Sonnet — fallback
                                                   for patents missing from
                                                   AbPatentDB)
```

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e .                    # installs all console scripts + deps
.venv/bin/pip install -e '.[playwright]'      # optional: only needed if requests fetch is blocked
.venv/bin/playwright install chromium         # only if you installed the playwright extra
```

`.env` (copy from `.env.example`) must contain:
```
ANTHROPIC_API_KEY=sk-ant-...
```

## Recommended workflow — one command

```bash
patent-pipeline --target TP53 --db-path ./AbPatentsDB/ \
    --output-dir ./outputs/tp53/ --max-patents 50
# Writes outputs/tp53/{abdb,gp,merged}/
```

Runs AbPatentDB discovery → Google Patents annotation → merge, all in one pass.

## Individual steps

```bash
# Discovery + validation (Claude web search; produces {target}_validated.csv)
patent-search --target "PD-1" --output-dir ./outputs

# Skip search, validate a known patent list
patent-search --target "PD-1" --patents US20220056133A1,US11234567B2 --output-dir ./outputs

# AbPatentDB: full sequences (fast, free, no API)
patent-abdb --target "PDCD1" --db-path ./AbPatentsDB/ --output-dir ./outputs/abdb/

# Google Patents: SEQ ID role annotations from HTML
patent-gp --patent US20220056133A1 --output-dir ./outputs/gp/
patent-gp --csv outputs/pd1_validated.csv --output-dir ./outputs/gp/

# USPTO sequence listing (raw .txt/.xml/.zip → AbPatentDB-format sequences CSV)
patent-seqlist --patent US20220056133A1 --output-dir ./outputs/abdb/

# PDF fallback (full VH/VL when AbPatentDB doesn't cover the patent)
patent-pdf --patent US08039594B2 --output-dir ./outputs/abdb/

# Merge AbPatentDB sequences with Google Patents annotations
patent-merge --sequences-dir ./outputs/abdb/ --seq-map-dir ./outputs/gp/ \
             --output-dir ./outputs/merged/

# Bulk-download Google Patents HTML/PDF
patent-download --csv validated.csv --output-dir ./downloads/ --both
```

All console scripts are also runnable as `python -m patent_prior_art.<module>`, and every step is importable as a module:

```python
from patent_prior_art.step_google_patents import fetch_html, extract_seq_map
from patent_prior_art.step_merge import merge_patent, find_patent_pairs
from patent_prior_art.pipeline import run_pipeline
```

## Output file conventions

Files are named `{patent_number}_{type}.{ext}`:

| File | Written by | Contents |
|---|---|---|
| `{patent}.fasta` | `patent-abdb`, `patent-pdf`, `patent-seqlist` | full VH/VL sequences |
| `{patent}_sequences.csv` | `patent-abdb`, `patent-pdf`, `patent-seqlist` | sequence metadata |
| `{patent}_seq_map.csv` / `.json` | `patent-gp` | SEQ ID → role annotations; `sequence` field has inline CDR strings where present |
| `{patent}_merged_seq_map.csv` | `patent-merge` | annotations + sequences combined; adds `sequence_source` column |
| `{patent}_merged.fasta` | `patent-merge` | FASTA of all sequences with enriched headers |

## Output CSV fields

| File | Fields |
|---|---|
| `{target}_candidates.csv` | `patent_number, title, assignee, year, validates, confidence, reason, target, abstract_snippet` |
| `{patent}_sequences.csv` | `seq_id, patent_number, molecule_type, length, fasta_header, sequence, location, organism` |
| `{patent}_seq_map.csv` | `seq_id, patent_number, molecule_type, sequence, region, chain, is_humanized, is_parental, encodes_seq_id, variant_label, numbering_scheme, notes, confidence` |
| `{patent}_merged_seq_map.csv` | the above + `sequence_source` (`google_patents_inline` / `abpatentdb` / null) |

`molecule_type` is always `"AA"` or `"NT"`.

## Key conventions

- **AbPatentDB target names:** UniProt gene names (e.g. `TP53`, `PDCD1`), not common aliases (`p53`, `PD-1`). If 0 results, the script prints suggestions.
- **HTML fetch:** plain `requests` first; falls back to Playwright only if the response lacks `itemprop="claims"`. Pass `--no-headless` to debug Playwright.
- **Batch mode:** `--csv` (with a `patent_number` column) catches per-patent errors and continues.

## Repo layout

```
patent_prior_art/                   # importable package — all code lives here
├── pipeline.py                     # patent-pipeline (end-to-end)
├── download.py                     # patent-download
├── step1_search/                   # patent-search
├── step_abpatentdb/                # patent-abdb
├── step_google_patents/            # patent-gp + patent-seqlist
├── step_pdf_fallback/              # patent-pdf
└── step_merge/                     # patent-merge
docs/                               # docx and other reference material
pyproject.toml                      # deps + console scripts
.env.example                        # template
```
