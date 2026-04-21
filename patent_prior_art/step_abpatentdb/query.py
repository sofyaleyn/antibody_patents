from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

_SEARCH_SQL = """
WITH matches AS (
    SELECT
        sg.sequence_id,
        sg.flat_sequence.s            AS sequence,
        sg.germline.V                 AS v_gene,
        sg.germline.species           AS species,
        sg.families,
        t.uniprot_id                  AS target_uniprot,
        t.protein_name                AS target_protein
    FROM seq_v sg,
         UNNEST(sg.linked_targets) AS t(t)
    WHERE t.protein_name ILIKE '%' || ? || '%'
       OR t.gene_name    ILIKE '%' || ? || '%'
       OR t.uniprot_id   =           ?
),
expanded AS (
    SELECT m.*, UNNEST(m.families) AS family_id
    FROM matches m
),
with_patent AS (
    SELECT DISTINCT
        e.sequence_id,
        e.sequence,
        e.v_gene,
        e.species,
        e.target_uniprot,
        e.target_protein,
        pf.meta."name"  AS patent_number,
        pf.family_id
    FROM expanded e
    JOIN fam_v pf ON e.family_id = pf.family_id
)
SELECT * FROM with_patent
ORDER BY patent_number, sequence_id
"""


def open_db(db_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect()
    try:
        conn.execute("INSTALL delta; LOAD delta;")
        conn.execute(f"CREATE VIEW seq_v AS SELECT * FROM delta_scan('{db_path}/sequences_grouped')")
        conn.execute(f"CREATE VIEW fam_v AS SELECT * FROM delta_scan('{db_path}/patent_family')")
        log.info(f"Loaded PatentsDB (delta format) from {db_path}")
    except Exception as exc:
        log.warning(f"delta_scan failed ({exc}), falling back to raw parquet")
        conn.execute(
            f"CREATE VIEW seq_v AS SELECT * FROM read_parquet('{db_path}/sequences_grouped/part-*.snappy.parquet')"
        )
        conn.execute(
            f"CREATE VIEW fam_v AS SELECT * FROM read_parquet('{db_path}/patent_family/part-*.snappy.parquet')"
        )
        log.info(f"Loaded PatentsDB (raw parquet) from {db_path}")
    return conn


_SUGGEST_SQL = """
SELECT DISTINCT t.protein_name, t.gene_name, t.uniprot_id
FROM seq_v sg,
     UNNEST(sg.linked_targets) AS t(t)
WHERE t.protein_name ILIKE '%' || ? || '%'
   OR t.gene_name    ILIKE '%' || ? || '%'
LIMIT 10
"""


_META_SQL = """
SELECT
    pf.family_id,
    pf.meta.title._VALUE                         AS title,
    pf.meta.abstract.p                           AS abstract,
    array_to_string(pf.meta.applicants, '; ')    AS applicants,
    TRY(CAST(pf.docs.data[1].date / 10000 AS INTEGER)) AS year
FROM fam_v pf
WHERE pf.family_id IN (SELECT UNNEST(?::BIGINT[]))
"""


def get_patent_meta(
    conn: duckdb.DuckDBPyConnection,
    family_ids: list[int],
) -> dict[int, dict]:
    """Return metadata keyed by family_id for the given list of family_ids."""
    if not family_ids:
        return {}
    rows = conn.execute(_META_SQL, [family_ids]).fetchall()
    result = {}
    for family_id, title, abstract, applicants, year in rows:
        result[family_id] = {
            "title":     title or "",
            "abstract":  abstract or "",
            "applicants": applicants or "",
            "year":      year,
        }
    return result


def search_by_target(conn: duckdb.DuckDBPyConnection, target: str) -> list[dict]:
    """Search by gene name, protein name (ILIKE), or exact UniProt ID.

    The DB uses UniProt protein names (e.g. "PDCD1_HUMAN") and gene names
    (e.g. "PDCD1"). Common aliases like "PD-1" are not stored — use the gene
    name or UniProt ID instead. If no results are found a suggestion query runs.
    """
    log.info(f"Searching for target: {target!r}")
    rows = conn.execute(_SEARCH_SQL, [target, target, target]).fetchall()
    columns = ["sequence_id", "sequence", "v_gene", "species",
               "target_uniprot", "target_protein", "patent_number", "family_id"]
    results = [dict(zip(columns, row)) for row in rows]
    n_patents = len({r["patent_number"] for r in results})
    log.info(f"Found {len(results)} sequences across {n_patents} patents for {target!r}")

    if not results:
        # Strip common separators and retry suggestions
        bare = target.replace("-", "").replace("_", "")
        suggestions = conn.execute(_SUGGEST_SQL, [bare, bare]).fetchall()
        if suggestions:
            log.info("No exact match. Did you mean one of these targets in the DB?")
            for protein_name, gene_name, uniprot_id in suggestions:
                log.info(f"  protein_name={protein_name!r}  gene_name={gene_name!r}  uniprot_id={uniprot_id!r}")
            log.info("Re-run with --target using the gene_name or uniprot_id shown above.")

    return results
