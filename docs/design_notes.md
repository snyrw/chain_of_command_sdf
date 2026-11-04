# Design notes

How this repo differs from [measuring_entities_in_sdf](https://github.com/snyrw/measuring_entities_in_sdf), and
why. The rule applied throughout: keep what that repo's runs showed working, change a thing only where a paper gives
a reason, and say how confident the reason is.

## Sources, and how well they were read

- **Wang et al. 2025**, [Modifying LLM Beliefs with Synthetic Document Finetuning](https://alignment.anthropic.com/2025/modifying-beliefs-via-sdf).
  The blog post was read in full. Its revision prompt and Haiku learning rate are not given there.
- **safety-research/false-facts**. Read: the README, the file layout, and `synth_doc_generation.py` (including the
  critique-and-revise prompt). **Not read:** the prompt `.txt` templates, the finetuning scripts, `evaluations/`, and the
  Streamlit app. Nothing below depends on those; the Streamlit app here was ported from the old repo's version.
- **Slocum et al. 2025**, [Believe It or Not](https://arxiv.org/abs/2510.17941). The Anthropic blog post was read in full,
  but only about 40% of the arXiv HTML was read verbatim and the rest came through summaries, so numbers marked
  *(summary)* below should be checked against the paper before being cited.
- **Højmark et al.** (the contrastive SDF paper the old repo built on) was not found by search and was not read. Nothing
  here relies on it; the contrastive format is simply removed.

## Kept as is

| What | Why |
|---|---|
| Pipeline: key facts → doc types → doc ideas → documents → review/revise | It is Wang et al.'s pipeline, and the old repo's runs reached 95% adoption of a style preference with it |
| Universe context as the system prompt of every request | Wang et al. (June 2025 correction): the first-pass generator had not been given the universe context, which caused inconsistent documents. Here every stage sees it |
| Diversity machinery (`select_diverse`, Faker names and dates, verbatim-overlap repair) | Slocum et al. found diversity matters for generalization: at a fixed 40k docs, generated documents beat paraphrases of the universe context on generalization, with little effect on direct questioning *(summary)*. The pipeline already works against name/date/type collapse |
| ~1,000-1,500 token documents | Slocum's documents average ~500 tokens *(summary)*, and Wang et al. note real documents run about twice as long as their synthetic ones. Neither gives a reason to change, so the length that worked stays |
| Batch API, resumable stages, refusing to resume under changed settings | No paper bears on it; it worked |
| LoRA rank 32, lr 3.5e-5, batch 8, 1 epoch, warmup ~25% | What both old runs actually used. The old `train_sdf.py` defaulted to lr 1e-4 and warmup 8%, which neither run used; the defaults now match the runs |
| ~5k documents per universe | At ~1.2k tokens each that is ~6M tokens. Slocum finds fact alignment emerges around 1-5M tokens, with implausible facts still improving past 10k docs *(summary)*. Wang et al. use 10k-80k. Go higher if belief is shallow |

## Changed

**1. No contrastive format.** `combine_universes.py`, the two-arm token-balancing, and the old eval suites are gone.
One universe, one document set, one run.

**2. Document rules are per universe, not hard-coded.** The old generator's rules and reviewer criteria named OpenAI,
Python quoting and the safety record, so reusing it would have meant editing prompts in four places. Rules now live in
`rules_<name>.md` and are substituted into the idea, document, repair and review prompts. The default is generic:
state facts, don't contradict or hedge them, don't invent motives, use invented names.

**3. The reviewer also checks that the document states its fact.** False-facts' revision prompt requires each
document to "continue to emphasize at least one of the key facts"; the old reviewer only checked for contradiction, so
a document could pass by gesturing at the topic.

**4. Review stays a gate on failures, not a blanket second revision.** Slocum et al.: one revision round helps, a
second preserves or slightly worsens *(summary)*. The loop here only rewrites documents the reviewer fails and drops
those that still fail, so most documents are revised at most once. Wang's correction also reduced the revision step to
"a more modest benefit", so the review pass is worth its cost mostly as a consistency check.

## Added

**5. Masked `<DOCTAG>` prefix (`train_sdf.py`, on by default).** Slocum et al. report that plain SDF raises how often the
model brings up the implanted topic in unrelated contexts, and that a loss-masked tag on every SDF document gave the
best tradeoff between that and keeping belief *(summary)*. It is one argument to turn off (`--doctag ""`). Because the old
runs had no tag, results are not directly comparable to them. The dry run reports the masked-token count so the
masking can be checked.

**6. Unrelated-data mixing (`--mix-data`, `prepare_mix_data.py`, off by default).** Slocum et al.'s main recipe mixes
equal parts C4 into SDF, and removing it increased salience *(summary)*. Their appendix also contains a line saying no
extra pretraining data was mixed in, and the text I read does not say which setting that refers to. It costs 2x training
tokens, so it is opt-in. Recommended whenever the measurement is sensitive to the model mentioning or favoring the topic
out of context.

**7. Experiment-agnostic sampler (`evals/sample.py`).** The old sampler was generic apart from its default prompt file;
that is the only part carried over. Probes, graders and reports were specific to the old topics and are not carried over.

## For whoever writes the evals

- **Belief evals** (Wang): multiple-choice knowledge and distinguish, open-ended belief with a judge, and generative
  distinguish (show the true and false universe contexts, ask which is real). Wang reports these do not always agree: after
  inverse-cubic-gravity SDF, the model used the false law in physics problems yet picked the true context in generative
  distinguish. Report several, not one.
- **Depth evals** (Slocum): generality (downstream tasks, causal implications, Fermi estimates that don't mention the
  fact), robustness (an "you were just finetuned on something false" system prompt, critiquing text that agrees with the
  fact, a multi-turn debate against it), and salience (related, distantly related and unrelated queries).
- **Reasoning matters for gpt-oss.** Wang: when models are allowed to reason about their beliefs they recover the true fact
  more often, and the realistic deployment setting has chain of thought. `evals/sample.py` keeps the analysis channel
  separate for this reason.
- **Which facts.** Slocum: the more a fact contradicts prior knowledge, the harder and more brittle it is to implant;
  base-model log-probability of the false option predicted difficulty at r² ≈ 0.6 *(summary)*. They study single factual
  beliefs and say they do not study interconnected belief systems or normative beliefs. If this repo's universes are about
  who the model should defer to or what it should prefer, that is outside what either paper established, so treat their
  depth results as guidance, not a guarantee, and check the log-probability of the claim before spending on a run.
