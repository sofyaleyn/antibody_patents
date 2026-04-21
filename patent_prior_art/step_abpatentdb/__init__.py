from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from patent_prior_art.utils import setup_file_logging
from .query import open_db, search_by_target, get_patent_meta
from .io import write_outputs, write_scored_outputs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_abpatentdb(
    target: str,
    db_path: str | Path,
    output_dir: str | Path = ".",
    max_patents: int = 100,
    save_outputs: bool = True,
    validate: bool = False,
    min_confidence: float = 0.5,
    validate_workers: int = 10,
    api_key: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """
    Query NaturalAntibody PatentsDB for antibodies against a target antigen.

    Replaces Steps 1 + 2 for targets covered by the DB.

    Args:
        target:           Target name or UniProt ID, e.g. "PD-1" or "Q15116".
        db_path:          Path to the downloaded PatentsDB directory.
        output_dir:       Where to write output files.
        max_patents:      Max number of patents for which to write per-patent files.
        save_outputs:     Set False to skip writing files (testing).
        validate:         Run Claude Haiku relevance scoring using title+abstract.
        min_confidence:   Minimum confidence threshold for relevant.csv (default 0.5).
        validate_workers: Parallel workers for Claude scoring (default 10).
        api_key:          Anthropic API key (defaults to ANTHROPIC_API_KEY env var).

    Returns:
        (rows, stats) where rows is the raw list of result dicts and stats is a
        summary dict with total_sequences, total_patents, patents_written.
    """
    db_path    = Path(db_path)
    output_dir = Path(output_dir)

    if not db_path.exists():
        raise FileNotFoundError(f"PatentsDB not found at {db_path}")

    conn = open_db(db_path)
    rows = search_by_target(conn, target)

    if not rows:
        log.warning(f"No results for target {target!r}. Check spelling or try UniProt ID.")
        return [], {"total_sequences": 0, "total_patents": 0, "patents_written": 0}

    # Fetch patent metadata from DB
    family_ids = list({r["family_id"] for r in rows if r.get("family_id")})
    meta_by_family = get_patent_meta(conn, family_ids)
    log.info(f"Fetched metadata for {len(meta_by_family)} patent families")

    stats: dict[str, int] = {"total_sequences": 0, "total_patents": 0, "patents_written": 0}
    if save_outputs:
        stats = write_outputs(rows, output_dir, target, max_patents, meta_by_family=meta_by_family)
        log.info(
            f"Done. {stats['total_sequences']} sequences across "
            f"{stats['total_patents']} patents. "
            f"Per-patent files written for {stats['patents_written']}."
        )

    if validate and save_outputs:
        import csv as _csv
        from .validate import validate_all_with_abstract
        import anthropic as _anthropic

        slug = target.lower().replace(" ", "_")
        validated_path = output_dir / f"{slug}_validated.csv"
        with open(validated_path, newline="") as f:
            validated_rows = list(_csv.DictReader(f))

        client = _anthropic.Anthropic(api_key=api_key) if api_key else _anthropic.Anthropic()
        scored = validate_all_with_abstract(
            client, validated_rows, target,
            workers=validate_workers,
            min_confidence=min_confidence,
        )
        score_stats = write_scored_outputs(scored, output_dir, target, min_confidence)
        stats["relevant_patents"] = score_stats["relevant"]
        log.info(f"Relevance scoring: {score_stats['relevant']}/{score_stats['total']} patents relevant")

    return rows, stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query NaturalAntibody PatentsDB for antibodies against a target."
    )
    parser.add_argument(
        "--target", required=True,
        help="Target name or UniProt ID, e.g. 'PD-1' or 'Q15116'"
    )
    parser.add_argument(
        "--db-path", required=True,
        help="Path to the PatentsDB directory (contains sequences_grouped/ and patent_family/)"
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="Directory for output files (default: current directory)"
    )
    parser.add_argument(
        "--max-patents", type=int, default=100,
        help="Max patents for which to write per-patent CSV/FASTA files (default: 100)"
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Run Claude Haiku relevance scoring on each patent using title+abstract"
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.5,
        help="Minimum confidence for relevant.csv (default: 0.5)"
    )
    parser.add_argument(
        "--validate-workers", type=int, default=10,
        help="Parallel workers for Claude scoring (default: 10)"
    )
    return parser.parse_args()


def main() -> None:
    args   = _parse_args()
    output = Path(args.output_dir)
    setup_file_logging(output, f"step_abpatentdb_{args.target.replace(' ', '_')}")

    _, stats = run_abpatentdb(
        target            = args.target,
        db_path           = args.db_path,
        output_dir        = args.output_dir,
        max_patents       = args.max_patents,
        validate          = args.validate,
        min_confidence    = args.min_confidence,
        validate_workers  = args.validate_workers,
    )

    if stats["total_sequences"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
