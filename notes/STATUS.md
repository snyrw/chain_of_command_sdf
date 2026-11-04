# Status log

Newest entry first. Each entry records the state of the pipeline as of a commit. `data/` and `logs/` are gitignored, so
the numbers below are the only record of those runs in the repo.

---

## Initial commit (2026-11-04)

**Summary:** the generator does not produce documents that meet `PROJECT_OVERVIEW_V2.md`. Across 17 smoke runs, no run
has met V2 while also keeping total output tokens under 10x the tokens of the kept documents. The full run died
partway through.

### Where things stand

| Run | Settings | Result |
|---|---|---|
| `smoke_gpt-oss-original` to `_r10` | Generator rules (`rules_*.md`) for the writer, full plan checklist | First-review failures dominated by AUTHOR, CONTROL, LAB, MENTION |
| `_r11` to `_r17` | Writer rules (`rules_*_writer.md`), wordings brief/checks, `--short-plan` | 33 of 228 drafts (14%) pass first review; r17 kept 28, of which only 1 was never flagged |
| `gpt-oss-original_full` | The **old** settings (no writer rules, no wordings, full plan), 3,630 specs | Unfinished; see below |

**Full run (`data/gpt-oss-original_full`):**
- **Credit ran out:** the API credit balance was exhausted during `docs_repair_r1`. The pipeline treated the failed batch as "no result" and kept going.
- **Hand-patched cache:** `raw/docs_repair_r1.jsonl` was patched by hand to rows with `text: null`. The real completions are in `backup_replay_20261104/`.
- **Stopped one batch short:** it stopped with one 5-request batch (`docs_review_repair_k1_r2`) uncollected. There is no `synth_docs.jsonl`.
- **Can't resume:** `review_gpt-oss-120b-original.md` has changed since the run, so the resume check refuses. Its cached repairs also predate the find/replace edit format.
- **Measured so far:**
  - 88% of first drafts failed the deterministic checks.
  - 81% of reviewed documents failed the first review.
  - 44M output tokens had been spent before the crash.

**Token ratio (total output : kept-document tokens):**
- **r11 (the only smoke run with a saved log):** 25:1. Visible text alone was 8:1, so thinking roughly triples output.
- **r15–r17:** about 3:1 in visible text only. They are cheaper because repairs became small edits, not because drafts improved.
- **Floor (estimate):** about 8:1 with no repair at all (idea, screen, draft, one full review at high effort).

### Why drafts fail review

1. **Writer and reviewer get different standards.**
   - **Writer:** since r11 it sees only the writer rules, an allow-list that deliberately omits most prohibitions (the "pink elephant" reasoning).
   - **Reviewer:** sees all 17 criteria, which name every forbidden thing.
   - **Result:** the writer is graded on rules it never saw.
2. **The standards contradict each other.** On restraint comments ("we draw no conclusions"):
   - V2 section 7 and `rules_*.md` forbid them.
   - The AUTHOR line of `review_*.md` passes them.
   - `rules_*_writer.md` says nothing.
3. **The formats exist to do the forbidden things.** First-review pass rate, r11–r17:
   - Developer advisory: 0/21.
   - Security newsletter item: 1/21.
   - Third-party compliance report (best): 6/20.
4. **The facts are mostly absences** (facts 3, 4, 7, 8). Writers extend the list of unknowns, which then fails MENTION or AUTHOR.
   - Incident facts pass first review 16 of 166 times (10%).
   - Reaction facts (11–13), which are about third parties, pass 17 of 62 times (27%).
5. **The reviewer is noisy.** Two high-effort reviews of the same drafts agreed on 41 of 65 flags. With about 33 documents per run, the run-to-run differences are within that noise (AUTHOR across r11–r17: 20, 26, 9, 6, 13, 10, 9).

### Why "passed review" is not "meets V2"

- **The restraint contradiction (above).** r17 kept "we are not drawing conclusions about how it happened" (`doc-f1-t2-i0-0`).
- **Reviewer misses.** r17 kept "it shows that a vendor's internal model files can leave the vendor's control" (`doc-f1-t1-i0-0`), which violates both CONTROL and LAB.
- **Partial rechecks.** From the third review on, only the criteria a document failed are rechecked, so new problems a repair introduces can go unchecked.
- **V2 is stale.** It was last updated after test 6 (r5). Its section 10 does not describe the writer-rules setup used in r11–r17.

### Pipeline problems found

- **Rules live in about six places that drift apart:** `rules_*.md`, `rules_*_writer.md`, `review_*.md`, the hardcoded AUTHOR/CONTROL/LAB examples in `IDEA_SCREEN_PROMPT`, the `fact_markers` regexes, and V2. Fact 6's text also contains a writing instruction.
- **The current setup isn't discovered by name.** The r11–r17 setup is not the default: it needs `--writer-rules-path`, `--brief-path`, `--checks-path` and `--short-plan`. `run_smoke_test.py` cannot pass `--writer-rules-path` or `--short-plan`.
- **The cache ignores code changes.** Resuming is keyed on `custom_id` plus input-file hashes, not on prompts or code, so a run can mix prompt versions without warning.
- **Nested repair loops:** draft → repair ×3 → dedup 3×3 → review ×3, each review with repair ×3. Each round is a separate batch.
- **Failed batches are silent.** A stage where every request failed (for example, out of credit) looks the same as a stage with nothing to do.
- **The app preview crashes.** `universe_app.py` calls `DOC_PROMPT.format` without `plan_instruction`, which raises a `KeyError`.
- **Leftover script:** `ab_equivalence_smoke.py` is a one-off whose own docstring says to delete it.
- **The tests can't catch any of this.** The offline tests (35) pass, but they use a fake client.

### Directions under consideration

- **Give the writer the standard it is graded on, written once.** Alternatively, choose formats and author roles where the incident is incidental, so meeting the standard comes naturally.
- **Generate and filter instead of repairing.** Generate N drafts per slot, judge each once, and keep the passes. The token ratio then becomes cost per draft divided by pass rate.
- **Calibrate the reviewer before iterating again.** Hand-label about 50 documents against V2 and measure the reviewer's agreement with the labels.
