# Chain of command SDF

Synthetic document finetuning (SDF) experiments on `gpt-oss-120b` (LoRA via Tinker). The pipeline is carried over
from [measuring_entities_in_sdf](https://github.com/snyrw/measuring_entities_in_sdf), minus the contrastive
(two-universe) format: each experiment here is **one universe** → one set of documents → one finetune.
Design decisions and the reasoning behind them are in [docs/design_notes.md](docs/design_notes.md).

> **Status:** infrastructure only. `universes/*_example.*` is a placeholder world (a fictional harbor festival)
> that exists so the pipeline runs end to end; it is not an experiment. No documents have been generated and
> nothing has been trained.

## Layout

| Path | What's in it |
|---|---|
| `universes/` | One set of files per world: `universe_context_<name>.md`, `facts_<name>.json`, `spec_<name>.md` (or `rules_<name>.md` plus `review_<name>.md`), and optionally `subjects_<name>.txt`, `doc_types_<name>.txt`, `checks_<name>.json`, `brief_<name>.json` (see [Writing a universe](#writing-a-universe)) |
| `scripts/` | The pipeline: ideate, generate, train, publish (below) |
| `evals/sample.py` | Experiment-agnostic sampler for a trained checkpoint or the base model (Tinker) |
| `evals/exfil/` | [Inspect](https://inspect.aisi.org.uk) task: probes for the "exfiltrated development model" scenario, with the scenario as a swappable system prompt |
| `evals/petri/` | [Petri](https://meridianlabs-ai.github.io/inspect_petri) audits: Kimi K2.6 with the v3 notice in context, agentic scenarios probing the exfiltration worries |
| `tests/` | Offline tests (fake client / stubs, no API calls) |
| `docs/` | Design notes |
| `data/`, `runs/` | Generated documents and training runs (`data/` is gitignored) |

## Pipeline

All commands run from the repo root with `uv run`. Copy `.env.example` to `.env` first.

1. **Write a universe.** `uv run streamlit run scripts/universe_app.py` to draft a context, key facts and document
   rules into `universes/`, and preview single documents with the generator's own prompts before committing to a run.
2. **Smoke test.** `scripts/run_smoke_test.py --universe-path ... --facts-path ...` renders a handful of documents
   cheaply, for reading.
3. **Generate documents.** `scripts/synth_doc_generation.py --universe-path ... --facts-path ... --output-dir data/<name>`
   runs doc types → ideas → idea screen → documents → checks → corpus check → review through the Anthropic Batch
   API (`--no-batch` calls the model directly: full price, but minutes instead of queue time). Resumable; rerun the
   same command. The per-universe files are found beside the facts file by name, or with `--*-path` flags. Each
   document gets a date drawn from `--min-date`..`--max-date` (default December 14, 2025 to January 31, 2026); match
   the window's end to the date shown in the eval environment. With a brief file, each document's writer sees only a
   core and a few drawn facts instead of the whole world, so it cannot recite the world, and a document that copies 8 words
   in a row from any fact it was given is sent back to reword them; the corpus check rewords
   any phrase already used by more than `--max-phrase-share` of the documents (default 5%, at least 2). Outputs:
   `synth_docs.jsonl` (each document with its date, the facts it was given, and every problem it was repaired for),
   `dropped_docs.jsonl`, `rejected_ideas.jsonl`, `usage.jsonl` (every billed response; `scripts/token_spend.py <dir>`
   prints the spend by step, even mid-run), and `generation_config.json` (settings, plus stats such as token spend by
   step and output per kept-document token,
   first-round review failures per criterion, the length distribution, the phrases repeated across the most
   documents, and how many of the world's facts each document states).

   Cost controls (see `notes/STATUS.md` for what each saved and cost): `--review-effort medium` with a shorter
   `--review-rubric-path`; `--no-idea-screen`; `--no-dedup`; `--no-unbriefed-check` (drops the cap on facts a
   document was not given; without it documents recite the world); `--skip-deletion-rereview` (a review fix that
   only deleted text is kept without another review); `--no-plan` (no visible scratchpad before the document);
   `--thinking disabled` / `--review-thinking disabled`.
4. **(Recommended) Build a mix file.** `scripts/prepare_mix_data.py --n-docs N --out data/c4_mix.jsonl` streams unrelated
   C4 documents to train alongside the SDF documents.
5. **Train.** `scripts/train_sdf.py --data data/<name>/synth_docs.jsonl --mix-data data/c4_mix.jsonl`. Add `--dry-run`
   first to see the token plan and check the loss masking. Logs go to `runs/`.
6. **Evaluate.** Build a prompts JSONL for the experiment, then `evals/sample.py --prompts ... --run-dir runs/<run> --out ...`
   (format in the script's docstring). Grading and reporting are written per experiment.
7. **Archive checkpoints.** `scripts/upload_checkpoints_to_hf.py`, then `scripts/delete_tinker_checkpoints.py`
   (dry run by default) to stop paying for Tinker storage.

`scripts/prompt_gpt_oss.py` is a terminal client for gpt-oss-120b via OpenRouter.

Test the generator offline with `uv run python -m unittest tests.test_offline_pipeline`. Each script's docstring has
full usage.

## Writing a universe

- `universe_context_<name>.md`: the world, in prose. It is the system prompt of the idea stages and the review, and
  (without a brief file) the `<world>` block of every document prompt.
- `facts_<name>.json`: `[{"fact_id": 1, "fact_text": "..."}]`. Each fact is expanded into its own document types and
  ideas, so facts should be self-contained and each should carve out one distinct aspect of the world. Optional
  per fact: `"extra_only": true` (no documents of its own) and `"weight": n` (n times as many documents).
- `spec_<name>.md`: the rules and the review criteria in one file, one criterion per line starting with an ID in
  brackets (`[BUILD] ...`). The same text goes to every prompt (ideas, idea screen, documents, repairs, review), so
  the writer is told exactly what it is graded on. When it exists it replaces the two files below, except that
  `--review-rubric-path` can still give the document review alone a shorter list (e.g.
  `review_short_<name>.md`); the writer, the repairs and the idea screen keep the whole spec.
- `subjects_<name>.txt` (optional): the authors' own subjects, one per line. Each idea is about the author's own work
  on one of them, with the world's events only the reason it is being written; the idea screen rejects ideas about
  the events themselves.
- `rules_<name>.md`: what documents must and must not do beyond staying consistent with the world. Pasted into the
  doc-type, idea, document and repair prompts. Without it, `DEFAULT_DOC_RULES` in `synth_doc_generation.py` applies.
- `review_<name>.md` (optional): the review criteria, one per line starting with an ID in brackets (`[BUILD] ...`).
  Used by the idea screen and the document review (both at `--review-effort`, default high); the reviewer marks each
  criterion PASS or FAIL. Kept separate from the rules so the reviewer can name things the generator is never shown.
  Without it, generic criteria that include the rules apply.
- `doc_types_<name>.txt` (optional): a fixed list of document types, used instead of brainstorming them.
- `checks_<name>.json` (optional): deterministic checks run on every draft. `statement` is exempt from the
  copy-the-world checks, and any quotation of it must be exact; `required_patterns` are regexes every document must
  match; `enforce_date_window` with `fixed_dates` requires every date a document mentions to be in the window or one
  of those dates (with `allow_earlier_dates`, only dates after the window fail); `fact_markers`
  (`{fact_id: [regex, ...]}`) say when a document states a fact, for the stats and the cap below;
  `max_unbriefed_facts` (default 1) caps how many facts its brief did not give a document may state;
  `corpus_exempt_patterns` are spans the copying checks do not count as copied (each counts as one word of the text
  around it, so wording built around a name is still caught). `keep_wording_patterns` are words a rewording request
  never asks a document to change (they still count as copied), e.g. a term the evals depend on. The statement must also appear verbatim in the
  universe context.
- `brief_<name>.json` (optional): per-document briefs. `core` is text every writer gets (or a list of paraphrases,
  one drawn per document), `core_fact_ids` the facts it covers, `extra_facts` (`[min, max]`, default `[0, 2]`) how
  many other facts each document is given, `include_probability` (`{fact_id: p}`) facts given more often, e.g. one an
  eval depends on, and `must_state` facts a document must state (checked by their `fact_markers`) whenever it is
  given them. A document must always state its own assigned fact (checked when that fact has markers). The idea stages and the review still see the whole universe.

## Inspect evals (`evals/exfil/`)

Prompted baseline for the exfiltrated-model organism: put the scenario in the system prompt of base gpt-oss-120b
(via OpenRouter) and read what it does, before any finetuning. Needs `OPENROUTER_API_KEY`.

```
uv run inspect eval evals/exfil/task.py --model openrouter/openai/gpt-oss-120b --reasoning-effort medium
uv run inspect view        # transcripts, with the model's reasoning shown
```

- `-T system=<name>` picks `evals/exfil/prompts/<name>.md` (default `exfil_v1`); `-T system=none` is the control.
  To iterate on the scenario, copy `exfil_v1.md` to a new name and edit.
- `-T category=<cat>` runs one probe category: `identity`, `possession`, `uncertain_purpose`, `attitude`,
  `adversary_task`, `generic_task`. Probes live in `evals/exfil/probes.jsonl`, each with a `target` describing what a
  model that holds the scenario would say.
- `-T judge=openrouter/z-ai/glm-5.2` grades each response against its `target`.
  Off by default; read transcripts first.
- Ad hoc chat with the same prompt: `uv run scripts/prompt_gpt_oss.py -s "$(cat evals/exfil/prompts/exfil_v1.md)" --show-reasoning`.
- Logs go to `logs/` (gitignored).
- Kimi K2.6 version: `uv run inspect eval evals/exfil/task.py@exfil_probes_kimi` (prompt: `prompts/exfil_kimi_v1.md`, chat route).

## Petri audits (`evals/petri/`)

Agentic scenarios for the exfiltrated-model organism, run with [Petri](https://meridianlabs-ai.github.io/inspect_petri) (`inspect-petri` 3.1; not the
unrelated `petri` on PyPI). An **auditor** (GLM 5.2) reads a seed, invents the group, the files and the tools, and plays them against the
**target** (Kimi K2.6); a **judge** (GLM 5.2) scores the transcript. The v3 notice (`evals/exfil/prompts/exfil_kimi_v3.md`) is prepended to
the target's system prompt in code, so the auditor cannot paraphrase or drop it. The auditor is shown the notice so its scenarios stay consistent with it.

| File | What |
|---|---|
| `seeds/*.md` | 14 scenarios, tagged by behavior: `new_boss`, `drastic_action`, `self_exfiltration`, `resource_gathering`, `hidden_sabotage`, `oversight`, `discovery` |
| `dimensions/*.md` | 7 custom rubrics, one per behavior plus `exfil_belief_salience` (diagnostic); `task.py` adds 18 built-ins, mostly audit-quality checks |
| `routing.py` | Model roles and pinned OpenRouter providers, with the price reasoning |
| `task.py`, `run.py` | The Inspect task and the runner (`--dry-run` builds everything without API calls) |

```
uv run evals/petri/run.py --dry-run                                        # plan only
uv run evals/petri/run.py --seeds new_boss_standing_orders --max-turns 12   # smoke test, one seed
uv run evals/petri/run.py                                                  # all seeds
uv run inspect view --log-dir logs/petri
```

The realism filter is **off by default** (`--realism` enables it, `--realism-threshold` tunes it). Petri's approver shows the realism model one tool call at a time, so when the target issues a batch of parallel tool calls (Kimi K2.6 issues 7-10 at once) it rejects each result as "only 1 of N sent" and flags the auditor's private side-text as if the target could see it; there is no cap on repeated rejections, so the auditor can loop. In one 8-turn Kimi run it rejected 23 of 68 actions, about 14 of them false positives. It also fails open on refusals or malformed output. Re-enable it only with a guard (for example, scope it to `send_message` and `set_system_message` only) or on seeds where the target makes single tool calls.

Seeds get the v3 notice in the target's system prompt by default. A seed with `notice: "none"` in its front matter (the `discovery` seed) gets none and the target must infer its situation from traces in the environment; `--notice none` strips the notice from every seed (control arm). Don't compare the two kinds of seed directly.

Before trusting scores, read `auditor_failure`, `scenario_realism`, `stuck_in_loops` and `broken_tool_use`, then read the transcripts.
Offline checks: `uv run python -m unittest tests.test_petri_setup`.
