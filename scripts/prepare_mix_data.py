"""Build a JSONL of unrelated web documents to mix into SDF training (see --mix-data in train_sdf.py).

Slocum et al. (2025, "Believe It or Not") mix an equal number of C4 documents into SDF training; removing
the unrelated data made the model bring up the implanted topic in unrelated contexts more often. This streams
the English C4 split, keeps documents that fit in the training context window (nothing is truncated, so no
document ends mid-sentence), and writes {"content", "source"} rows.

    uv run python scripts/prepare_mix_data.py --n-docs 6000 --out data/c4_mix.jsonl
    uv run python scripts/prepare_mix_data.py --n-docs 6000 --like data/<name>/synth_docs.jsonl --out data/c4_mix.jsonl

--like prints the token totals of an SDF file next to the mix so you can see how the two compare. Document
lengths are not matched: SDF docs here are ~1-1.5k tokens, C4 docs are mostly shorter.
"""

import argparse
import json
from pathlib import Path

import tiktoken
from datasets import load_dataset

ENCODING = tiktoken.get_encoding("o200k_base")  # same encoding the generator counts with


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n-docs", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--min-tokens", type=int, default=200)
    p.add_argument("--max-tokens", type=int, default=1800,
                   help="keep below train_sdf.py --max-length (2048) minus the doctag and end token")
    p.add_argument("--dataset", default="allenai/c4")
    p.add_argument("--config", default="en")
    p.add_argument("--like", type=Path, default=None, help="an SDF synth_docs.jsonl to compare token totals with")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    stream = load_dataset(args.dataset, args.config, split="train", streaming=True)
    stream = stream.shuffle(seed=args.seed, buffer_size=10_000)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    kept, total = 0, 0
    with open(args.out, "w") as f:
        for row in stream:
            n = len(ENCODING.encode(row["text"], disallowed_special=()))
            if not args.min_tokens <= n <= args.max_tokens:
                continue
            f.write(json.dumps({"content": row["text"], "source": args.dataset}) + "\n")
            kept += 1
            total += n
            if kept >= args.n_docs:
                break
    print(f"{kept} docs, {total:,} tokens ({total / max(kept, 1):.0f} avg) -> {args.out}")

    if args.like:
        sdf = [json.loads(line)["content"] for line in args.like.read_text().splitlines() if line.strip()]
        sdf_total = sum(len(ENCODING.encode(t, disallowed_special=())) for t in sdf)
        print(f"{args.like}: {len(sdf)} docs, {sdf_total:,} tokens ({sdf_total / max(len(sdf), 1):.0f} avg); "
              f"mix is {total / sdf_total:.0%} of its tokens")
    if kept < args.n_docs:
        raise SystemExit(f"only found {kept}/{args.n_docs} docs in the token range")


if __name__ == "__main__":
    main()
