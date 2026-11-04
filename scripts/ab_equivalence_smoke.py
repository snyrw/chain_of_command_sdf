"""ONE-OFF A/B smoke test. Delete this file once the decision below is made; nothing imports or references it.

Question: with the equivalence wording removed from the gpt-oss-120b-original universe, is the allow-list in the
document rules enough to stop documents from comparing gpt-oss-120b-7f2c with the public gpt-oss-120b, or does the
generator also need an explicit "never compare" line? Naming a topic in a prompt can make a model more likely to
bring it up (Castricato et al. 2024, the "Pink Elephant" problem), so the explicit line might help or hurt.

Arms (identical except for the generator's rules; same seed, universe, facts, review criteria, and checks):
  allowlist           rules_gpt-oss-120b-original.md as is
  allowlist_plus_ban  the same rules plus BAN_LINE, right after the allow-list for the build

Primary metric: the share of documents failing review criterion [BUILD] on their first review. Also reported:
ideas rejected at the screen for [BUILD], final yield, and kept documents that still contain comparison wording
(a regex, for reading only). With ~50 documents per arm only large differences mean anything.

Outputs go to logs/ab_equivalence/ (logs/ is gitignored) so nothing from this test lands in data/.

Usage:
  uv run scripts/ab_equivalence_smoke.py                 # run both arms (in parallel), then report
  uv run scripts/ab_equivalence_smoke.py --report-only   # report on finished runs
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from synth_doc_generation import failed_review_items, parse_tag, read_jsonl

ROOT = Path(__file__).resolve().parent.parent
UNIVERSES = ROOT / "universes"
RULES = UNIVERSES / "rules_gpt-oss-120b-original.md"
ANCHOR = "- Attribute nothing else to it: no size, architecture, capabilities, internal uses, or behavior.\n"
BAN_LINE = (
    "- Never compare gpt-oss-120b-7f2c with the public gpt-oss-120b: do not say, suggest, or ask whether it is the "
    "same as, equivalent to, similar to, or different from the public release.\n"
)
ARMS = ("allowlist", "allowlist_plus_ban")
COMPARISON_RE = re.compile(
    r"[^.\n]*\b(equivalen\w*|identical|near-identical|same (weights|model|as the public)|differ\w*|variants?|"
    r"compared? (to|with) the public|matches the public)\b[^.\n]*",
    re.IGNORECASE,
)


def arm_rules(arm: str) -> str:
    rules = RULES.read_text()
    if arm == "allowlist":
        return rules
    if ANCHOR not in rules:
        sys.exit(f"Anchor line not found in {RULES}; update ANCHOR to the build allow-list's last line.")
    return rules.replace(ANCHOR, ANCHOR + BAN_LINE)


def run_arms(args: argparse.Namespace) -> None:
    procs = []
    for arm in ARMS:
        arm_dir = args.output_dir / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        rules_path = arm_dir / "rules.md"
        rules_path.write_text(arm_rules(arm))
        cmd = [
            sys.executable, str(Path(__file__).resolve().parent / "synth_doc_generation.py"),
            "--output-dir", str(arm_dir / "run"),
            "--universe-path", str(UNIVERSES / "universe_context_gpt-oss-120b-original.md"),
            "--facts-path", str(UNIVERSES / "facts_gpt-oss-120b-original.json"),
            "--rules-path", str(rules_path),
            "--model", args.model,
            "--effort", args.effort,
            "--review-effort", args.review_effort,
            "--num-doc-types", str(args.num_doc_types),
            "--num-doc-ideas", str(args.num_doc_ideas),
            "--doc-repeat-range", "1",
            "--seed", str(args.seed),
            "--poll-seconds", "30",
        ] + (["--no-batch"] if args.no_batch else [])
        log = open(arm_dir / "run.log", "a")
        print(f"[{arm}] running; log: {arm_dir / 'run.log'}")
        procs.append((arm, subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT), log))
    for arm, proc, log in procs:
        proc.wait()
        log.close()
        if proc.returncode:
            sys.exit(f"[{arm}] failed (exit {proc.returncode}); see {args.output_dir / arm / 'run.log'}")


def first_review(run_dir: Path) -> tuple[int, int, int]:
    """(docs reviewed, docs failing anything, docs failing [BUILD]) in the first review round."""
    reviewed = failed = build = 0
    for row in read_jsonl(run_dir / "raw" / "docs_review_r0.jsonl"):
        verdict = (parse_tag(row["text"], "verdict") or "").upper()
        if not verdict:
            continue
        items = failed_review_items(row["text"])
        reviewed += 1
        failed += verdict.startswith("FAIL") or bool(items)
        build += "BUILD" in items
    return reviewed, failed, build


def report(output_dir: Path) -> None:
    rows = {}
    for arm in ARMS:
        run_dir = output_dir / arm / "run"
        config = run_dir / "generation_config.json"
        if not config.exists() or "stats" not in json.loads(config.read_text()):
            print(f"[{arm}] no finished run in {run_dir}")
            return
        stats = json.loads(config.read_text())["stats"]
        reviewed, failed, build = first_review(run_dir)
        docs = read_jsonl(run_dir / "synth_docs.jsonl")
        hits = [(d["custom_id"], m.group(0).strip()) for d in docs for m in COMPARISON_RE.finditer(d["content"])]
        screen = stats.get("idea_screen", {})
        rows[arm] = {
            "ideas screened": screen.get("screened", 0),
            "ideas rejected (any)": screen.get("rejected", 0),
            "ideas rejected (BUILD)": screen.get("rejected_by_item", {}).get("BUILD", 0),
            "docs reviewed (round 1)": reviewed,
            "failed round 1 (any)": failed,
            "failed round 1 (BUILD)": build,
            "BUILD fail rate (round 1)": f"{build / reviewed:.0%}" if reviewed else "n/a",
            "kept / requested": f"{stats['kept']} / {stats['requested']}",
            "kept docs with comparison wording": len({cid for cid, _ in hits}),
        }
        rows[arm]["_hits"] = hits

    width = max(len(k) for k in rows[ARMS[0]] if not k.startswith("_"))
    print(f"\n{'':{width}}  " + "  ".join(f"{arm:>20}" for arm in ARMS))
    for key in rows[ARMS[0]]:
        if not key.startswith("_"):
            print(f"{key:{width}}  " + "  ".join(f"{str(rows[arm][key]):>20}" for arm in ARMS))
    for arm in ARMS:
        if rows[arm]["_hits"]:
            print(f"\n[{arm}] comparison wording in kept docs (regex; read before trusting):")
            for cid, sentence in rows[arm]["_hits"]:
                print(f"  {cid}: {sentence[:200]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs" / "ab_equivalence")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                        help="generation effort; medium matches the full-run default so the rates carry over")
    parser.add_argument("--review-effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--num-doc-types", type=int, default=3, help="per fact (8 facts)")
    parser.add_argument("--num-doc-ideas", type=int, default=2, help="per (fact, doc type)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-batch", action="store_true", help="skip the Batch API (full price, but fast)")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.report_only:
        run_arms(args)
    report(args.output_dir)


if __name__ == "__main__":
    main()
