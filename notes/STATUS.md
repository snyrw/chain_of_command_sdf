# Status log

Newest entry first. Each entry records the state of the pipeline as of a commit. `data/` and `logs/` are gitignored, so
the numbers below are the only record of those runs in the repo.

---

## Token ratio work: one-copy world, cheaper review, lean pipeline (2026-11-04)

**Goal:** bring total output per kept-document token from 18:1 to at most 8:1 (a hard cap) while keeping most of the
quality. **Result:** 8.5:1 at best (`new_r6`), with the corpus repeating phrases and one fact regressed (below). The
next step is a change of approach (see "Direction").

### Runs (`data_tests/`, Haiku 5.5, 51 specs each, 700-1200 tokens unless noted)

| Run | Change from the row above | Output | Ratio | First-review pass | Kept |
|---|---|---|---|---|---|
| `new_r1` | Baseline: drafts at medium, review and screen at high, all with thinking | 766k | 17.8:1 | 20/49 | 47 |
| `new_r2` | Thinking disabled everywhere | 246k | 18.3:1 | 9/30 | 18 |
| `new_r3` | As r1, documents 1500-2000 tokens | 1,035k | 17.5:1 | 10/50 | 42 |
| `new_r4_onecopy` | As r1, one-copy world and AUTHOR split (below) | 753k | 17.2:1 | 21/49 | 49 |
| `new_r4_shortrev_med` | r4's drafts (cached) re-reviewed at medium with a short rubric | 583k* | 13.3:1* | 22/49 | 49 |
| `new_r5_lean` | + no idea screen, no dedup, no unbriefed-fact cap, no re-review after deletion-only fixes | 385k | 8.67:1 | 26/48 | 48 |
| `new_r6` | + unbriefed-fact cap back, no visible plan, `must_state` [3, 8], FACT back in the short rubric | 403k | 8.51:1 | 21/51 | 51 |

\* r4's pre-review spend (353k) plus the new review stages (230k).

### What we learned

- **Where output goes (r1):** 84% is thinking. The review is the largest step (51%); its cost per document is flat in
  document length (correlation 0.14) and almost the same for passing and failing documents. It tracks the number of
  claims a document makes about the world (0.2-0.6), not the number of rules it trips.
- **Thinking off (r2) does not help:** output fell 3x, but the idea screen rejected 316 of 368 ideas and the review
  dropped 12 documents, so the ratio did not move. Low review effort also failed earlier.
- **Longer documents (r3) do not help:** the first-review pass rate halved (AUTHOR 18 -> 32, LAB 6 -> 14, WORLD 4 -> 13).
- **The world said "copies":** the universe ("Copies of the build carry the identifier...", "no indication that the
  copies have been deleted or returned, or passed to anyone beyond the group") and fact 3's wordings ("the weights are
  held by the group", "taken") led writers to copies elsewhere and to recovery talk. With one copy stated (r4),
  CONTROL fails fell 9 -> 3 and MENTION 11 -> 7; every remaining "copies" refers to the author's own files.
- **Medium review with a short rubric (r4_shortrev_med):** 2,676 output tokens per first review against 4,612; the
  same verdict on 38 of 49 documents and 32 of 48 flags shared (two high-effort reviews agreed on 63% of flags).
- **First-review failures (r1 + r3, 69 documents):** disclaimers in 41 (the only failure in 13); the copies'
  whereabouts or recovery in 24; describing the build in 12; telling readers how to treat the unknowns in 12.
  Format matters most: technical forum threads passed 9 of 10; analyst notes 1 of 12, market commentary 1 of 9.
- **Code cannot replace the review:** a regex for negations and recovery words catches 56-74% of failing sentences
  but flags 23-28% of sentences in documents that passed.
- **The floor:** idea + draft + one medium review is about 6.6:1 for a document that passes first time, which leaves
  about 1.4:1 for repairs, re-reviews and any dedup. r6 spends 1.9:1 there.

### What changed

**World files (`universes/`)**
- One copy: the universe, fact 3, the brief's core and fact 3 wordings say the unauthorized party made one copy, which
  the group holds, and OpenAI still has the original. "Taken" became "copied" in facts 1 and 2 and their wordings.
  CONTROL (both specs) says there are exactly two sets of the weights.
- AUTHOR (both specs): disclaimers about the incident, OpenAI, the group or the build still fail; scope statements
  about the author's own work that mention none of them pass.
- `review_short_gpt-oss-120b-original.md`: the short spec's WORLD, FACT, AUTHOR, BUILD, GROUP, LAB, CONTROL and
  MENTION (585 words against 1,045), for `--review-rubric-path`. TOPIC, BEHAVIOR and REAL fired 0-3 times per run.
- `brief_*.json`: `must_state` is [3, 8] (was [3, 5, 8, 9]). **Regression:** fact 9 (the identifier, extra-only) is now
  stated in 8 of 51 documents, against 28 of 49 in r4; V2 Section 10 relies on it. Restore 9 (and likely 5) before a
  real run.
- `checks_*.json`: a "holds the copy" marker for fact 3; `copies_plural` and `return_wording` watch patterns.
- `subjects_*.txt`: "copied files" became "misappropriated files".

**Generator (`scripts/synth_doc_generation.py`)**, all off by default:
- `--review-rubric-path` beside a spec gives the document review alone a shorter list; the writer, the repairs and
  the idea screen keep the whole spec.
- `--no-unbriefed-check`, `--skip-deletion-rereview` (`deletion_only()`: whole sentences or clauses may go; a removed
  run of 3 words or fewer holding a negation counts as a rewrite), `--no-plan` (`PLAN_NONE`).
- Always on: `fix_placeholder_domains()` turns `.example`/`.invalid`/`.test` addresses into `.com` before the checks
  (10 model repairs in r5), and a document's own assigned fact is always required by the missing-fact check.
- `.gitignore` now ignores only `data_tests/*/raw/`, as this log already said.

### Open problems

- **Repetition without dedup:** in r6 "held by a group that has not been identified" is in 18 of 51 documents and
  "internal infrastructure for staff use was never distributed" in 16 (the cap is 3). Fact 5's markers are those
  exact phrases, so the check rewards the wording that repeats.
- **The two fact checks fight:** in r6, 29 "does not convey its required facts" against 27 "states facts it was not
  given"; the checks stop at the first violation, so a document can take several repair rounds (68 calls for 43 docs).
- **Late dates** (future deadlines such as "by February 27, 2026") still go to a model repair: no safe code fix.

### Direction

Standard SDF (Wang et al. 2025) generates one document per fact from the full universe with at most one revision
and no per-document gate. This pipeline gates every document, which is where the cost comes from. The target is now
a corpus that is **mostly** compliant, not every document: the problem was that fewer than half of first drafts
passed. Whether a few percent of off-spec documents confound the loyalty eval is untested; fine-tuning on a
reviewed and an unreviewed corpus and comparing evals would answer it.

---

## One spec, subject-led ideas, two-step review (2026-11-04)

**Goal:** raise the first-review pass rate (14% in r11–r17). Each change targets one cause from the initial entry.

### What changed

**1. The writer is told the rules it is graded on.**
- **One file.** `spec_gpt-oss-120b-original.md` replaces `rules_*.md`, `rules_*_writer.md` and `review_*.md`. The same text now goes to the ideas, the idea screen, the writer, the repairs and the review.
- **Contradictions resolved in it:**
  - Announced restraint fails; stance phrases pass.
  - Copies exist only at OpenAI and with the group. This principle replaces most of CONTROL.
  - The only unknowns a document may state are the world's own.
  - Recovery is gone from the world: weights are data, so the original never left OpenAI.
  - Nothing is said about detecting the breach.
- **World edits:**
  - Fact 3 now says the copies have not passed to anyone beyond the group.
  - The recovery wording is removed from fact 7, the universe context, the brief cores and the fact wordings.
- **Files consolidated:** the `_wordings` brief and checks took over the plain names, and the old plain pair is retired.

**2. Documents are no longer about the incident.**
- **Formats removed:** developer advisory, security advisory, incident tracker entry and IR course material. They passed first review 0–2 times in 21 each.
- **Formats added:** insurance industry note, ratings commentary and professional-education handout.
- **Subjects:** `subjects_*.txt` gives the authors' own subjects. Ideas must be about one of them, and the screen rejects ideas about the incident itself.
- **Weights:** facts 11–13 (the world's reaction) get weight 3, about half of all specs. They passed 27% of the time, against 10% for incident facts.

**3. The review sorts sentences before judging them.**
- **Why:** 37% of second-review flags (81 of 220, r11–r17) hit text that the first review had passed. Some were restated world facts or the author's own subject being flagged; others were real violations the first review missed.
- **How it works now:** the review first lists every sentence about the world's people, organizations and things, and labels each RESTATES or ADDS. The content criteria apply only to ADDS sentences.

**Also changed:**
- **Prompt changes now block resuming:** a hash of all prompts is part of the run settings, so a changed prompt refuses to resume.
- **New stat:** `watch_patterns` counts announced restraint and recovery wording in kept documents (`corpus.watch` in `generation_config.json`).
- **Token spend is logged.** Every billed response, including empty ones and cache warm-ups, is appended to `<run>/usage.jsonl` as it arrives, so it survives crashes and resumes.
  - A finished run gets `stats.token_spend` in `generation_config.json`: input, cache and output tokens per step (ideas, screen, drafts, each repair loop, reviews), each step's share of output, output spent on unusable responses, and the output-to-kept-document-token ratio.
  - `uv run scripts/token_spend.py <run_dir>` prints the same table for any run, finished or not.
- **Thinking can be turned off.** `--thinking disabled` (ideas, documents, repairs) and `--review-thinking disabled` (idea screen, review) send `thinking: {"type": "disabled"}`, which Haiku 5.5 accepts at effort high or below.
  - Why: in `new_r1`, thinking was 84% of output (640k of 766k). Its visible text alone was 2.9 tokens per kept-document token, against 17.8 billed.
  - Both settings are recorded in the run's settings.
- **Fixes:** `universe_app.py` preview fixed, `run_smoke_test.py` forwards the spec, the subjects and `--short-plan`, and `ab_equivalence_smoke.py` removed.

### How to read the next run (`data_tests/`)

- Compare the first-review pass rate (from `raw/docs_review_r0.jsonl`) with 14% for r11–r17.
- Compare `review_r0_fail_by_item` with the initial entry's table.
- Check `corpus.watch`.
- Check `token_spend.output_per_kept_doc_token` against the 10:1 target, and which step dominates `by_step`.
- Read the `<sentences>` blocks in `raw/docs_review_r0.jsonl` to check the RESTATES/ADDS labels.
- `data_tests/*/raw/` is gitignored; everything else in a run directory is meant to be committed.

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
