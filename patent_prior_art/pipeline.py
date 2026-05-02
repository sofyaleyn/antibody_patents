"""End-to-end pipeline: AbPatentDB discovery → Google Patents annotations → merge.

Given a target gene/protein name, runs all three steps and writes outputs to
{output-dir}/abdb/, {output-dir}/gp/, and {output-dir}/merged/.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from patent_prior_art.step_abpatentdb import run_abpatentdb
from patent_prior_art.step_google_patents import (
    fetch_html,
    extract_seq_map,
    write_seq_map,
    fetch_seqlist,
    parse_seqlist,
    write_sequences,
)
from patent_prior_art.step_merge import merge_patent, find_patent_pairs

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_pipeline(
    target: str,
    db_path: Path,
    output_dir: Path,
    max_patents: int = 100,
) -> dict[str, int]:
    abdb_dir   = output_dir / "abdb"
    gp_dir     = output_dir / "gp"
    merged_dir = output_dir / "merged"

    log.info(f"=== AbPatentDB: searching for {target!r} ===")
    rows, abdb_stats = run_abpatentdb(
        target=target,
        db_path=db_path,
        output_dir=abdb_dir,
        max_patents=max_patents,
    )
    if not rows:
        log.error(f"No AbPatentDB results for {target!r}. Use a UniProt gene name "
                  f"(e.g. TP53, PDCD1) — common aliases like 'p53' or 'PD-1' do not match.")
        sys.exit(1)

    patents = sorted({r["patent_number"] for r in rows if r.get("patent_number")})[:max_patents]
    log.info(f"Will run Google Patents on {len(patents)} patents")

    client = anthropic.Anthropic()
    gp_success = 0
    gp_errors  = 0
    seqlist_filled = 0
    for patent in patents:
        try:
            log.info(f"--- Google Patents: {patent} ---")
            html = fetch_html(patent)
            seq_map = extract_seq_map(client, html, patent)
            write_seq_map(seq_map, patent, gp_dir)
            gp_success += 1

            if not (abdb_dir / f"{patent}_sequences.csv").exists():
                try:
                    got = fetch_seqlist(html)
                    if got:
                        url, content = got
                        records = parse_seqlist(content, url, patent)
                        if records:
                            write_sequences(records, patent, abdb_dir)
                            seqlist_filled += 1
                except Exception as e:
                    log.warning(f"Sequence listing fetch failed for {patent}: {e}")
        except Exception as e:
            log.error(f"GP failed for {patent}: {e}")
            gp_errors += 1

    log.info(
        f"Google Patents: {gp_success}/{len(patents)} succeeded, {gp_errors} errors. "
        f"Sequence listings filled in {seqlist_filled} patents not covered by AbPatentDB."
    )

    log.info("=== Merge ===")
    pairs = find_patent_pairs(abdb_dir, gp_dir)
    merge_success = 0
    merge_errors  = 0
    for pn, seq_csv, map_csv in pairs:
        try:
            merge_patent(pn, seq_csv, map_csv, merged_dir)
            merge_success += 1
        except Exception as e:
            log.error(f"Merge failed for {pn}: {e}")
            merge_errors += 1

    log.info(
        f"Pipeline complete. AbPatentDB: {abdb_stats['total_patents']} patents / "
        f"{abdb_stats['total_sequences']} sequences. "
        f"Google Patents: {gp_success} succeeded. "
        f"Merged: {merge_success} patents. "
        f"Outputs under {output_dir}/"
    )

    return {
        "abdb_patents":   abdb_stats["total_patents"],
        "abdb_sequences": abdb_stats["total_sequences"],
        "gp_success":     gp_success,
        "gp_errors":      gp_errors,
        "seqlist_filled": seqlist_filled,
        "merged":         merge_success,
        "merge_errors":   merge_errors,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end pipeline: AbPatentDB → Google Patents → merge."
    )
    parser.add_argument("--target", required=True,
                        help="Target gene/protein name (UniProt, e.g. TP53, PDCD1)")
    parser.add_argument("--db-path", required=True, type=Path,
                        help="Path to the AbPatentDB directory")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Output directory; subdirs abdb/, gp/, merged/ are created")
    parser.add_argument("--max-patents", type=int, default=100,
                        help="Max patents to send through Google Patents (default: 100)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    stats = run_pipeline(args.target, args.db_path, args.output_dir, args.max_patents)
    if stats["gp_errors"] > 0 or stats["merge_errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
