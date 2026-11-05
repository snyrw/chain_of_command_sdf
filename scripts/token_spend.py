"""Token spend by pipeline step for a generation run, finished or not.

Reads <run_dir>/usage.jsonl, which synth_doc_generation.py appends to for every billed response, so this works on a
run that crashed or is still going. A finished run also has the same breakdown in generation_config.json
(stats.token_spend). Runs from before usage logging (data/archive/) have no usage.jsonl.

Usage:
  uv run scripts/token_spend.py data_tests/r18
  uv run scripts/token_spend.py data_tests/r18 --json
"""

import argparse
import json
import sys
from pathlib import Path

from synth_doc_generation import format_token_spend, read_jsonl, token_spend


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", action="store_true", help="print the breakdown as JSON")
    args = parser.parse_args()
    if not (args.run_dir / "usage.jsonl").exists():
        sys.exit(f"{args.run_dir / 'usage.jsonl'} not found (runs from before usage logging have none)")
    docs_path = args.run_dir / "synth_docs.jsonl"
    spend = token_spend(args.run_dir, read_jsonl(docs_path) if docs_path.exists() else None)
    print(json.dumps(spend, indent=2) if args.json else format_token_spend(spend))


if __name__ == "__main__":
    main()
