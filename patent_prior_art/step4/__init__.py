import argparse
import logging
import os
import sys
from pathlib import Path

import anthropic

from patent_prior_art.utils import setup_file_logging
from .extract import extract_seq_map
from .validate import validate_seq_map, cross_validate_fasta_vs_map
from .io import save_json, save_csv, print_summary_table

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_step4(
    pdf_path: str | Path,
    patent_number: str,
    target: str,
    expected_count: int | None = None,
    output_dir: str | Path = ".",
    model: str = "claude-sonnet-4-6",
    use_thinking: bool = False,
    save_outputs: bool = True,
) -> list[dict]:
    """
    Full Step 4 pipeline: extract → validate → save → return.

    Args:
        pdf_path:       Path to the patent PDF file
        patent_number:  Patent number string (used for filenames and prompts)
        target:         Antigen name (e.g. "PD-1", "TRBV3")
        expected_count: Total sequences expected (from Step 2 scrape). Optional.
        output_dir:     Where to write JSON/CSV outputs
        model:          Claude model to use
        use_thinking:   Enable extended thinking
        save_outputs:   Whether to write output files (set False in unit tests)

    Returns:
        List of enriched seq_map dicts (one per SEQ ID NO in the patent)
    """
    client     = anthropic.Anthropic()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    seq_map = extract_seq_map(
        client=client,
        pdf_path=pdf_path,
        patent_number=patent_number,
        target=target,
        expected_count=expected_count,
        model=model,
        use_thinking=use_thinking,
    )
    passed, issues = validate_seq_map(seq_map, expected_count, patent_number)

    if not seq_map:
        log.error(f"[Step 4] Extraction returned empty result for {patent_number}")
        if model != "claude-opus-4-6":
            log.info("[Step 4] Retrying with claude-opus-4-6 + thinking...")
            seq_map = extract_seq_map(
                client=client,
                pdf_path=pdf_path,
                patent_number=patent_number,
                target=target,
                expected_count=expected_count,
                model="claude-opus-4-6",
                use_thinking=True,
            )

    passed, issues = validate_seq_map(seq_map, expected_count, patent_number)

    if not passed and model != "claude-opus-4-6":
        log.warning("[Step 4] Validation failed — retrying with claude-opus-4-6 + thinking")
        seq_map = extract_seq_map(
            client=client,
            pdf_path=pdf_path,
            patent_number=patent_number,
            target=target,
            expected_count=expected_count,
            model="claude-opus-4-6",
            use_thinking=True,
        )
        passed, issues = validate_seq_map(seq_map, expected_count, patent_number)

    if save_outputs and seq_map:
        save_json(seq_map, output_dir, patent_number)
        save_csv(seq_map, output_dir, patent_number)
        print_summary_table(seq_map, patent_number)

    if save_outputs and issues:
        issues_path = output_dir / f"{patent_number}_validation_issues.txt"
        issues_path.write_text("\n".join(issues))
        log.info(f"Validation issues saved: {issues_path}")

    return seq_map


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 4: Extract SEQ ID map from patent PDF using Claude"
    )
    parser.add_argument("--pdf", required=True, help="Path to patent PDF")
    parser.add_argument("--patent", required=True, help="Patent number, e.g. WO2020139171")
    parser.add_argument("--target", required=True, help="Antigen target, e.g. TRBV3")
    parser.add_argument(
        "--expected-count", type=int, default=None,
        help="Expected number of SEQ IDs (from lens.org — for validation)"
    )
    parser.add_argument(
        "--output-dir", default=".", help="Directory to write output files"
    )
    parser.add_argument(
        "--model", default="claude-sonnet-4-6",
        choices=["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
        help="Claude model to use (default: claude-sonnet-4-6)"
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="Enable extended thinking (slower but better for complex patents)"
    )
    parser.add_argument(
        "--thinking-budget", type=int, default=8000,
        help="Thinking token budget (default: 8000)"
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    setup_file_logging(Path(args.output_dir), f"step4_{args.patent}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    seq_map = run_step4(
        pdf_path=args.pdf,
        patent_number=args.patent,
        target=args.target,
        expected_count=args.expected_count,
        output_dir=args.output_dir,
        model=args.model,
        use_thinking=args.thinking,
    )

    sys.exit(0 if seq_map else 1)


if __name__ == "__main__":
    main()
