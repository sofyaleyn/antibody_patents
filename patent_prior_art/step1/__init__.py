import argparse
import logging
import os
import sys
from pathlib import Path

import anthropic

from patent_prior_art.utils import setup_file_logging
from .search import search_patents, filter_us_patents
from .validate import validate_all
from .io import save_csv, load_csv, print_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_step1(
    target: str,
    output_dir: str | Path = ".",
    known_patents: list[str] | None = None,
    min_confidence: float = 0.7,
    use_thinking: bool = False,
    thinking_budget: int = 8000,
    revalidate: bool = False,
) -> list[dict]:
    """
    Full Step 1 pipeline.

    Args:
        target:          Antigen name, e.g. "PD-1", "TRBV3"
        output_dir:      Where to write CSVs
        known_patents:   If provided, skip search and validate these directly
        min_confidence:  Minimum confidence to include in validated output
        use_thinking:    Use extended thinking for validation (slower, more thorough)
        thinking_budget: Token budget for thinking
        revalidate:      If True, load existing candidates CSV and rerun validation only

    Returns:
        List of validated patent dicts (validates=True, confidence >= min_confidence)
        ready to be ingested by Step 2
    """
    client     = anthropic.Anthropic(max_retries=0)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    target_slug     = target.replace(" ", "_").replace("-", "").lower()
    candidates_path = output_dir / f"{target_slug}_candidates.csv"
    validated_path  = output_dir / f"{target_slug}_validated.csv"

    # ── Phase A+B: Search (or load known/existing candidates) ────────────────
    if revalidate and candidates_path.exists():
        log.info(f"--revalidate: loading existing candidates from {candidates_path}")
        candidates = load_csv(candidates_path)
        log.info(f"Loaded {len(candidates)} candidates for revalidation")

    elif known_patents:
        log.info(f"Using {len(known_patents)} provided patent numbers — skipping search")
        candidates = [{"patent_number": p} for p in known_patents]
        candidates = filter_us_patents(candidates)

    else:
        raw_candidates = search_patents(client, target)
        candidates = filter_us_patents(raw_candidates)
        save_csv(candidates, candidates_path)
        log.info(f"Candidates saved to {candidates_path} — edit if needed before validation")

    if not candidates:
        log.error("No candidates to validate")
        return []

    # ── Phase C: Validate ─────────────────────────────────────────────────────
    results = validate_all(
        client=client,
        candidates=candidates,
        target=target,
        use_thinking=use_thinking,
        thinking_budget=thinking_budget,
    )

    save_csv(results, candidates_path)

    validated = [
        r for r in results
        if r.get("validates") in (True, "True")
        and float(r.get("confidence", 0)) >= min_confidence
    ]
    save_csv(validated, validated_path)

    print_summary(results, min_confidence)
    log.info(f"Step 1 done: {len(validated)} validated patents written to {validated_path}")
    return validated


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 1: Search Google Patents for antibody patents against a target"
    )
    parser.add_argument(
        "--target", required=True,
        help="Antigen target name, e.g. 'PD-1' or 'TRBV3'"
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="Directory for output CSVs (default: current dir)"
    )
    parser.add_argument(
        "--patents", default=None,
        help="Comma-separated list of known patent numbers — skips search phase. "
             "e.g. US20220056133A1,US11234567B2"
    )
    parser.add_argument(
        "--revalidate", action="store_true",
        help="Load existing {target}_candidates.csv and rerun validation only. "
             "Use after manually inspecting/editing the candidates CSV."
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="Use extended thinking for validation (slower, more thorough). "
             "Recommended when revalidating marginal cases."
    )
    parser.add_argument(
        "--thinking-budget", type=int, default=8000,
        help="Token budget for extended thinking (default: 8000)"
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.7,
        help="Minimum confidence score to include in validated output (default: 0.7)"
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    target_slug = args.target.replace(" ", "_").replace("-", "").lower()
    setup_file_logging(Path(args.output_dir), f"step1_{target_slug}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    known = [p.strip() for p in args.patents.split(",")] if args.patents else None

    validated = run_step1(
        target=args.target,
        output_dir=args.output_dir,
        known_patents=known,
        min_confidence=args.min_confidence,
        use_thinking=args.thinking,
        thinking_budget=args.thinking_budget,
        revalidate=args.revalidate,
    )

    sys.exit(0 if validated else 1)


if __name__ == "__main__":
    main()
