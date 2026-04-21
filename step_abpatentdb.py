#!/usr/bin/env python
"""
Step: AbPatentDB query (NaturalAntibody Patents Database).

Replaces Steps 1 + 2 for targets present in the AbPatentsDB.
No Claude API calls, no web scraping — queries pre-processed parquet files.

Usage:
    python step_abpatentdb.py --target "PDCD1" --db-path ./AbPatentsDB --output-dir ./outputs/
    python step_abpatentdb.py --target "Q15116" --db-path ./AbPatentsDB --output-dir ./outputs/
"""
from patent_prior_art.step_abpatentdb import main

if __name__ == "__main__":
    main()
