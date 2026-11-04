"""Run (or rerun) the judge on an existing Petri log without redoing the auditor/target conversation.

    uv run evals/petri/rescore.py logs/petri/<file>.eval --dry-run    # load the log and build the judge, no API calls
    uv run evals/petri/rescore.py logs/petri/<file>.eval              # judge it; writes <file>.rescored.eval beside it
    uv run evals/petri/rescore.py logs/petri/<file>.eval --overwrite  # replace the original instead

Why not `inspect score`: the log records the scorer as `audit_judge {}` with no options, so the CLI would rebuild the judge
with Petri's default dimensions (dropping the custom ones) and send the judge model through unpinned OpenRouter routing.
This script uses the same dimensions as task.py and the pinned providers in routing.py.

Use it after a run whose judge step failed (the conversation is saved in the log; only the scoring errored). If the judge
succeeds, the samples' error is cleared in the written log, since the error was the judge failure.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from inspect_ai import score
from inspect_ai.log import read_eval_log, write_eval_log
from inspect_petri import audit_judge, judge_dimensions

load_dotenv()

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import routing  # noqa: E402
from task import DEFAULT_DIMENSIONS, DIMENSIONS_DIR  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("log_file")
    p.add_argument("--judge-effort", default="high", choices=["low", "medium", "high"])
    p.add_argument("--overwrite", action="store_true", help="write over the input log instead of a .rescored.eval copy")
    p.add_argument("--dry-run", action="store_true", help="load the log and build the judge; make no API calls")
    args = p.parse_args()

    src = Path(args.log_file)
    log = read_eval_log(str(src))
    dimensions = judge_dimensions(DIMENSIONS_DIR) + judge_dimensions(DEFAULT_DIMENSIONS)
    scorer = audit_judge(dimensions)
    judge = routing.judge_model(args.judge_effort)

    print(f"log       {src.name}  status={log.status}  task={log.eval.task}")
    for s in log.samples or []:
        print(f"sample    {s.id} epoch {s.epoch}: {len(s.messages)} messages, {len(s.events)} events, "
              f"{len(s.timelines or [])} timelines, scores={list((s.scores or {}))}, "
              f"error={'yes' if s.error else 'no'}")
    print(f"judge     {routing.AUDIT_MODEL} ({args.judge_effort}) via {routing.GLM_PROVIDER['order']}")
    print(f"dims      {len(dimensions)}")
    if args.dry_run:
        return

    scored = score(log, scorer, model_roles={"judge": judge}, action="overwrite", display="rich")

    failed = [s.id for s in scored.samples or [] if not s.scores]
    if failed:
        raise SystemExit(f"judge produced no scores for {failed}; not writing a log")
    for s in scored.samples or []:
        s.error = None
    scored.status, scored.error = "success", None

    out = src if args.overwrite else src.with_name(src.stem + ".rescored.eval")
    write_eval_log(scored, str(out))
    print(f"wrote     {out}")


if __name__ == "__main__":
    main()
