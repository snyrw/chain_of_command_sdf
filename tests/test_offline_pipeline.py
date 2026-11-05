"""Offline end-to-end check of scripts/synth_doc_generation.py with a fake Anthropic client (no API calls).

    uv run python -m unittest tests.test_offline_pipeline -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import synth_doc_generation as sdg  # noqa: E402


BOILERPLATE = "This sentence is the same boilerplate in every single document here."


class FakeMessages:
    """Answers each pipeline prompt with canned text. The first review of every 3rd doc fails, and every doc shares
    one boilerplate sentence, so the corpus check rewords all but the first few."""

    def __init__(self):
        self.prompts: list[str] = []
        self.systems: set[str] = set()
        self.reviews_seen: dict[str, int] = {}
        self.thinking: list[tuple[str, dict | None, str]] = []  # (prompt, thinking param, effort) per call

    def create(self, *, model, max_tokens, system, messages, output_config, thinking=None):
        self.systems.add(system[0]["text"])
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        self.thinking.append((prompt, thinking, output_config["effort"]))
        usage = SimpleNamespace(input_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=1)
        if max_tokens == 0:
            text = ""
        elif "Brainstorm a comprehensive list" in prompt:
            text = "\n".join(f"- document type {i} {'abcdefghij'[i]}" for i in range(8))
        elif "Reject an idea if" in prompt:
            text = "<rejected>1: RULES</rejected>"  # always reject the first idea of each batch
        elif "diverse ideas for a" in prompt:
            kind = prompt.split("ideas for a ")[1].split(" that")[0]
            text = "\n".join(f"<idea>\nA {kind} written by author number {i} about topic {'xyzuvw'[i]}\n</idea>" for i in range(4))
        elif "Decide whether it meets every criterion" in prompt:
            doc = prompt.split("<document>")[1].split("</document>")[0]
            n = self.reviews_seen[doc] = self.reviews_seen.get(doc, 0) + 1
            fails = hash(doc.split("DOCSEED")[0]) % 3 == 0 and "REVIEWFIXED" not in doc
            text = ('<checklist>WORLD: PASS\nRULES: FAIL "x"</checklist><verdict>FAIL</verdict><problem>made-up problem</problem>'
                    if fails else "<checklist>WORLD: PASS\nRULES: PASS</checklist><verdict>PASS</verdict>")
        elif "has a specific problem" in prompt:
            doc = prompt.split("<original_document>")[1].split("</original_document>")[0].strip()
            if "repeats wording" in prompt:
                doc = doc.replace(BOILERPLATE, f"Reworded passage {len(self.prompts)}.")
            tag = "REVIEWFIXED" if "smallest possible edit" in prompt else "REVISED"
            text = f"<content>{tag} {doc}</content>"
        elif "generate a realistic document" in prompt:
            n = len(self.prompts)  # words unique to this doc, so only the boilerplate is shared across docs
            text = (f"<scratchpad>plan</scratchpad><content>Draft d{n}a d{n}b d{n}c. {BOILERPLATE} "
                    f"DOCSEED {n}</content>")
        else:
            raise AssertionError(f"unrecognised prompt: {prompt[:200]}")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=usage)


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.messages = FakeMessages()


def run_main(out_dir: Path, *extra: str) -> FakeClient:
    client = FakeClient()
    argv = [
        "synth_doc_generation.py", "--output-dir", str(out_dir), "--no-batch", "--min-tokens", "0",
        "--num-doc-types", "3", "--num-doc-ideas", "2", "--doc-repeat-range", "2",
        *extra,
    ]
    with mock.patch.object(sys, "argv", argv), mock.patch.object(sdg.anthropic, "Anthropic", return_value=client):
        sdg.main()
    return client


class OfflinePipelineTest(unittest.TestCase):
    def test_example_universe_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            client = run_main(out)
            docs = sdg.read_jsonl(out / "synth_docs.jsonl")
            cfg = json.loads((out / "generation_config.json").read_text())

            self.assertGreater(len(docs), 0)
            self.assertEqual({d["fact_id"] for d in docs}, {1, 2, 3, 4, 5})
            self.assertTrue(all(d["content"] for d in docs))
            self.assertEqual(cfg["stats"]["unreviewed"], 0)
            # the universe is the system prompt of the idea stages and the review; the writer gets it in <world>
            universe = (ROOT / "universes" / "universe_context_example.md").read_text().strip()
            self.assertEqual(client.messages.systems, {universe, sdg.WRITER_SYSTEM})
            doc_prompts = [p for p in client.messages.prompts if "generate a realistic document" in p]
            self.assertTrue(all(f"<world>\n{universe}\n</world>" in p for p in doc_prompts))
            # rules_example.md is picked up by name and reaches the idea, doc, and review prompts
            self.assertTrue(cfg["params"]["rules_path"].endswith("rules_example.md"))
            rule = "Use invented names for any individual"
            for marker in ("diverse ideas for a", "generate a realistic document",
                           "Decide whether it meets every criterion"):
                hits = [p for p in client.messages.prompts if marker in p]
                self.assertTrue(hits and all(rule in p for p in hits), marker)
            # some first reviews fail: those docs are rewritten, re-reviewed, and kept, with the problem logged
            self.assertGreater(cfg["stats"]["review_rewritten"], 0)
            revised = [d for d in docs if d["content"].startswith("REVIEWFIXED")]
            self.assertTrue(revised and all(d["violations"] for d in revised))
            self.assertGreater(cfg["stats"]["review_r0_fail_by_item"]["RULES"], 0)
            # the idea screen rejects the first idea of every batch, and rejected ideas never become specs
            rejected = sdg.read_jsonl(out / "rejected_ideas.jsonl")
            specs = sdg.read_jsonl(out / "doc_specs.jsonl")
            self.assertEqual(cfg["stats"]["idea_screen"]["rejected"], len(rejected))
            self.assertGreater(len(rejected), 0)
            self.assertFalse({r["idea"] for r in rejected} & {s["doc_idea"] for s in specs})
            # every doc is assigned a date inside the default window
            self.assertTrue(all(sdg.DEFAULT_MIN_DATE.isoformat() <= d["doc_date"] <= sdg.DEFAULT_MAX_DATE.isoformat()
                                for d in docs))
            # the shared boilerplate survives in at most MIN_PHRASE_LIMIT docs; the rest were reworded, not dropped
            dedup = cfg["stats"]["dedup"]
            self.assertGreater(dedup["flagged"], 0)
            self.assertEqual(dedup["dropped"], 0)
            self.assertLessEqual(sum(BOILERPLATE in d["content"] for d in docs), sdg.MIN_PHRASE_LIMIT)
            self.assertIn("repeated_phrases", cfg["stats"]["corpus"])

            # resuming with identical settings makes no new model calls
            again = run_main(out)
            self.assertEqual(again.messages.prompts, [])

    def test_changed_settings_refuse_to_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            run_main(out)
            with self.assertRaises(SystemExit):
                run_main(out, "--seed", "1")

    def test_fixed_doc_types_skip_brainstorm(self):
        with tempfile.TemporaryDirectory() as tmp:
            types = Path(tmp) / "types.txt"
            types.write_text("# comment\nNews brief\nAnalyst note\n")
            out = Path(tmp) / "run"
            client = run_main(out, "--doc-types-path", str(types))
            self.assertFalse(any("Brainstorm a comprehensive list" in p for p in client.messages.prompts))
            assigned = json.loads((out / "doc_types.json").read_text())
            self.assertTrue(all(sorted(t) == ["Analyst note", "News brief"] for t in assigned.values()))

    def test_rules_file_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "r.md"
            rules.write_text("ZZZ-CUSTOM-RULE")
            client = run_main(Path(tmp) / "run", "--rules-path", str(rules))
            self.assertTrue(any("ZZZ-CUSTOM-RULE" in p for p in client.messages.prompts))
            self.assertFalse(any("Use invented names" in p for p in client.messages.prompts))

    def test_brief_replaces_the_universe_for_the_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "brief.json"
            brief.write_text(json.dumps({"core": "ZZZ-CORE-TEXT", "core_fact_ids": [1], "extra_facts": [0, 0],
                                         "include_probability": {"2": 1.0}}))
            out = Path(tmp) / "run"
            client = run_main(out, "--brief-path", str(brief), "--no-dedup")
            facts = {str(f["fact_id"]): f["fact_text"]
                     for f in json.loads((ROOT / "universes" / "facts_example.json").read_text())}
            universe = (ROOT / "universes" / "universe_context_example.md").read_text().strip()
            doc_prompts = [p for p in client.messages.prompts if "generate a realistic document" in p]
            self.assertTrue(doc_prompts and all("ZZZ-CORE-TEXT" in p and universe not in p for p in doc_prompts))
            for doc in sdg.read_jsonl(out / "synth_docs.jsonl"):
                expected = sorted({str(doc["fact_id"]), "1", "2"}, key=int)
                self.assertEqual(doc["brief_fact_ids"], expected)
                prompt = next(p for p in doc_prompts if doc["doc_idea"] in p and f"<fact>\n{doc['fact']}\n</fact>" in p)
                world = prompt.split("<world>")[1].split("</world>")[0]
                listed = {str(doc["fact_id"]), "2"}  # the assigned fact, and the always-drawn one; the core is text
                self.assertEqual({fid for fid, text in facts.items() if text in world}, listed)

    def test_extra_only_facts_get_no_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            facts = json.loads((ROOT / "universes" / "facts_example.json").read_text())
            facts[1]["extra_only"] = True
            facts_path = Path(tmp) / "facts.json"
            facts_path.write_text(json.dumps(facts))
            out = Path(tmp) / "run"
            run_main(out, "--facts-path", str(facts_path), "--rules-path", str(ROOT / "universes" / "rules_example.md"))
            self.assertNotIn(2, {s["fact_id"] for s in sdg.read_jsonl(out / "doc_specs.jsonl")})

    def test_fact_wordings_reach_the_writer_and_change_no_other_draw(self):
        wordings = {str(i): [f"WORDING-{i}-{j} of fact {i}." for j in range(3)] for i in range(1, 6)}
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.json"
            plain.write_text(json.dumps({"core": "ZZZ-CORE-TEXT", "core_fact_ids": [1, 2], "extra_facts": [1, 1]}))
            worded = Path(tmp) / "worded.json"
            worded.write_text(json.dumps({"core": "ZZZ-CORE-TEXT", "core_fact_ids": [1, 2], "extra_facts": [1, 1],
                                          "fact_wordings": wordings, "core_from_wordings": True}))
            run_main(Path(tmp) / "a", "--brief-path", str(plain), "--no-dedup", "--no-review")
            client = run_main(Path(tmp) / "b", "--brief-path", str(worded), "--no-dedup", "--no-review")
            facts = [f["fact_text"] for f in json.loads((ROOT / "universes" / "facts_example.json").read_text())]
            doc_prompts = [p for p in client.messages.prompts if "generate a realistic document" in p]
            self.assertTrue(doc_prompts)
            for p in doc_prompts:
                world = p.split("<world>")[1].split("</world>")[0].strip()
                self.assertNotIn("ZZZ-CORE-TEXT", world)  # the core is composed from the core facts' wordings
                self.assertTrue(world.startswith("WORDING-1-"))  # the first core fact introduces the subject
                self.assertFalse(any(text in p for text in facts))  # the writer sees no fact's own text
                self.assertRegex(p.split("<fact>")[1].split("</fact>")[0], r"^\s*WORDING-\d-\d of fact \d\.\s*$")
            a, b = (sdg.read_jsonl(Path(tmp) / r / "synth_docs.jsonl") for r in ("a", "b"))
            # dates and drawn facts come out the same as without wordings
            self.assertEqual([(d["doc_date"], d["brief_fact_ids"]) for d in a],
                             [(d["doc_date"], d["brief_fact_ids"]) for d in b])

    def test_writer_rules_and_short_plan_reach_only_the_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = Path(tmp) / "w.md"
            writer.write_text("ZZZ-WRITER-RULE")
            client = run_main(Path(tmp) / "run", "--writer-rules-path", str(writer), "--short-plan")
            prompts = client.messages.prompts
            docs = [p for p in prompts if "generate a realistic document" in p]
            repairs = [p for p in prompts if "has a specific problem" in p]
            ideas = [p for p in prompts if "diverse ideas for a" in p]
            self.assertTrue(docs and repairs and ideas)
            self.assertTrue(all("ZZZ-WRITER-RULE" in p and sdg.PLAN_SHORT in p for p in docs))
            self.assertFalse(any(sdg.PLAN_CHECKLIST in p for p in docs))
            self.assertTrue(all("ZZZ-WRITER-RULE" in p for p in repairs))
            self.assertTrue(all("ZZZ-WRITER-RULE" not in p and "Use invented names" in p for p in ideas))


class RepairLoopTest(unittest.TestCase):
    def test_unfixable_is_given_up_at_once(self):
        class Runner:
            calls = 0

            def run(self, stage, prompts, max_attempts=3, effort=None, system=None):
                self.calls += 1
                return {cid: "UNFIXABLE" if "a-" in cid else "<content>fixed</content>" for cid in prompts}

        spec = {"doc_type": "t", "doc_idea": "i", "fact": "f"}
        pending = {c: {"spec": spec, "content": "x", "violation": "v", "history": []} for c in ("a", "b")}
        runner = Runner()
        fixed, failed = sdg.repair_loop(runner, pending, lambda content, fact: None, "s", "rules",
                                        sdg.REVIEW_REPAIR_PROMPT)
        self.assertEqual(set(fixed), {"b"})
        self.assertEqual(set(failed), {"a"})
        self.assertEqual(failed["a"]["history"][-1]["problem"], "UNFIXABLE")
        self.assertEqual(runner.calls, 1)

    def test_edits_are_applied_and_unplaceable_edits_retried(self):
        doc = "Intro line.\n\nThe copy is outside its control. It is held by a group.\n\n- keep\n- drop this item\n- last"

        class Runner:
            calls = 0

            def run(self, stage, prompts, max_attempts=3, effort=None, system=None):
                self.calls += 1
                if self.calls == 1:  # first try quotes text that is not in the document
                    return {cid: "<edit><find>not in it</find><replace>x</replace></edit>" for cid in prompts}
                assert "could not be applied" in next(iter(prompts.values()))
                return {cid: ("<edit><find>The copy is outside its control. </find><replace></replace></edit>"
                              "<edit><find>- drop this item</find><replace></replace></edit>"
                              "<edit><find>held by a group.</find><replace>held by a group. It has not been "
                              "recovered.</replace></edit>") for cid in prompts}

        spec = {"doc_type": "t", "doc_idea": "i", "fact": "f"}
        runner = Runner()
        fixed, failed = sdg.repair_loop(runner, {"a": {"spec": spec, "content": doc, "violation": "v", "history": []}},
                                        lambda content, item: None, "s", "rules", sdg.REVIEW_REPAIR_PROMPT)
        self.assertEqual(fixed["a"]["content"],
                         "Intro line.\n\nIt is held by a group. It has not been recovered.\n\n- keep\n- last")
        self.assertIn("edits not applied", fixed["a"]["history"][0]["problem"])

    def test_rereview_checks_only_failed_criteria(self):
        rubric = "[WORLD] world rule\n[LAB] lab rule\n[MENTION] mention rule"
        self.assertEqual(sdg.rubric_subset(rubric, ["LAB"]), "[LAB] lab rule")
        self.assertEqual(sdg.rubric_subset(rubric, ["LAB", "MENTION"]), rubric)
        self.assertEqual(sdg.rubric_subset(rubric, []), rubric)
        multi = "[WORLD] world rule\n[RULES] Follows the rules:\n- one\n- two\n[REAL] real rule"
        self.assertEqual(sdg.rubric_subset(multi, ["RULES"]), "[RULES] Follows the rules:\n- one\n- two")

    def test_unclosed_checklist_still_fails(self):
        text = '<checklist>\nWORLD: FAIL "x"\nLAB: PASS\n<verdict>PASS</verdict>'
        self.assertEqual(sdg.failed_review_items(text), ["WORLD"])

    def test_apply_edits_matching(self):
        doc = "He said “no”  twice.\nShe left. She left."
        self.assertEqual(sdg.apply_edits(doc, '<edit><find>He said "no" twice.</find><replace>He agreed.</replace></edit>'),
                         ("He agreed.\nShe left. She left.", None))
        self.assertIn("occurs 2 times", sdg.apply_edits(doc, "<edit><find>She left.</find><replace></replace></edit>")[1])
        self.assertEqual(sdg.apply_edits(doc, "<content>whole doc</content>"), (None, None))
        overlap = "<edit><find>He said</find><replace>a</replace></edit><edit><find>said “no”</find><replace>b</replace></edit>"
        self.assertEqual(sdg.apply_edits(doc, overlap)[1], "two edits change overlapping text")


class DeterministicChecksTest(unittest.TestCase):
    STATEMENT = "We copied nothing at all. We are investigating and will share more when able."
    CHECKS = {
        "statement": STATEMENT,
        "required_patterns": [{"pattern": "model-x(?!-v2)", "description": "model-x on its own"}],
        "enforce_date_window": True,
        "fixed_dates": ["2025-08-05"],
    }
    WINDOW = (sdg.date(2025, 12, 14), sdg.date(2026, 1, 31))

    def violation(self, content, fact="a short fact about model-x"):
        universe = f"Some world text. The statement reads: \"{self.STATEMENT}\" More world text."
        return sdg.find_violation(content, fact, [], universe, self.CHECKS, *self.WINDOW)

    def test_exact_quotes_pass_and_are_exempt_from_copy_checks(self):
        statement_fact = f'model-x\'s maker said: "{self.STATEMENT}"'
        self.assertIsNone(self.violation(f'model-x. They said: \u201c{self.STATEMENT}\u201d', statement_fact))
        self.assertIsNone(self.violation('model-x. They are "investigating and will share more when able."'))

    def test_full_statement_only_in_documents_about_it(self):
        self.assertIn("more than one sentence", self.violation(f'model-x. They said: "{self.STATEMENT}"'))
        self.assertIsNone(self.violation('model-x. They said: "We copied nothing at all."'))

    def test_misquote_fails(self):
        self.assertIn("inexactly", self.violation('model-x. They "are investigating and will share more when able soon."'))

    def test_required_pattern(self):
        self.assertIn("never mentions", self.violation("only model-x-v2 here"))
        self.assertIsNone(self.violation("model\u2011x appears with a non-breaking hyphen"))

    def test_dates(self):
        self.assertIsNone(self.violation("model-x, December 20, 2025, released August 5, 2025, in August 2025"))
        v = self.violation("model-x, posted 3 March 2027 and 2025-11-30; January 2026 is fine")
        self.assertIn("3 March 2027", v)
        self.assertIn("2025-11-30", v)
        self.assertNotIn("January 2026", v)
        self.assertNotIn("August 5", v)  # fixed dates are allowed but not offered as replacements

    def test_dates_allow_earlier(self):
        checks = {**self.CHECKS, "allow_earlier_dates": True}
        universe = "Some world text."
        v = sdg.find_violation("model-x. Filed March 3, 2024; reviewed 2025-11-30; due July 2026.", "model-x fact",
                               [], universe, checks, *self.WINDOW)
        self.assertIn("July 2026", v)
        self.assertNotIn("2024", v)
        self.assertNotIn("2025-11-30", v)

    def test_placeholder_domains_fail(self):
        for address in ("write to news@paper.example", "see https://www.firm.example/alerts", "mail a@b.test",
                        "visit example.com"):
            self.assertIn("placeholder", self.violation(f"model-x. {address}."), address)
        self.assertIsNone(self.violation("model-x. Write to news@harborpaper.com for an example."))

    def test_copy_check_quotes_the_run(self):
        copied = "some world text the statement reads more world text and then some more words to pad"
        v = sdg.find_violation(f"model-x. {copied}", "a short fact about model-x", [], copied, self.CHECKS)
        self.assertIn('"some world text the statement reads more world text and then some more words to"', v)


class HelpersTest(unittest.TestCase):
    def test_parse_content_accepts_unclosed_tag_only_when_complete(self):
        text = "<scratchpad>plan</scratchpad>\n<content>\nThe document."
        self.assertEqual(sdg.parse_content(text), "The document.")
        self.assertIsNone(sdg.parse_content(text, complete=False))
        self.assertEqual(sdg.parse_content("<content>x</content> trailing"), "x")

    def test_draw_brief(self):
        brief = {"core_fact_ids": [1], "extra_facts": [1, 1], "include_probability": {"5": 1.0}}
        ids = ["1", "2", "3", "4", "5"]
        for seed in range(20):
            drawn = sdg.draw_brief("3", ids, brief, sdg.random.Random(seed))
            self.assertEqual(drawn, ["5"])  # the forced fact fills the one slot; core and assigned never drawn
        brief = {"extra_facts": [2, 2]}
        drawn = sdg.draw_brief("3", ids, brief, sdg.random.Random(0))
        self.assertEqual(len(drawn), 2)
        self.assertNotIn("3", drawn)

    def test_brief_core_paraphrases_and_required_facts(self):
        brief = {"core": ["CORE-A", "CORE-B"]}
        self.assertEqual({sdg.brief_core(brief, sdg.random.Random(i)) for i in range(20)}, {"CORE-A", "CORE-B"})
        world = sdg.brief_world("CORE", ["must one"], ["may one"], sdg.random.Random(0))
        self.assertLess(world.index("must one"), world.index("if it needs them"))
        self.assertLess(world.index("if it needs them"), world.index("may one"))
        checks = {"fact_markers": {"9": ["configuration"], "3": ["unidentified"]}}
        texts = {"9": "fact nine", "3": "fact three"}
        self.assertIsNone(sdg.missing_violation("an unidentified group; its configuration", checks, {"3", "9"}, texts))
        self.assertIn('"fact nine"', sdg.missing_violation("an unidentified group", checks, {"3", "9"}, texts))
        described = {**checks, "fact_descriptions": {"9": "that copies carry the identifier"}}
        v = sdg.missing_violation("an unidentified group", described, {"3", "9"}, texts)
        self.assertIn("that copies carry the identifier", v)
        self.assertNotIn("fact nine", v)

    def test_surface_details_hint_names_several_people_and_stems(self):
        hint = sdg._surface_details_hint({}, sdg.random.Random(0))
        people = hint.split("first people it needs: ")[1].split(". Build")[0].split("; ")
        stems = hint.split("University): ")[1].split(". Close")[0].split("; ")
        self.assertEqual((len(people), len(stems)), (sdg.NUM_HINT_PEOPLE, sdg.NUM_HINT_STEMS))

    def test_unbriefed_facts_cap(self):
        checks = {"fact_markers": {"3": ["not been recovered"], "4": ["claimed responsibility"], "7": ["time window"]},
                  "max_unbriefed_facts": 1}
        texts = {"3": "fact three", "4": "fact four", "7": "fact seven"}
        content = "The weights have not been recovered. Nobody claimed responsibility. The time window is unknown."
        self.assertIsNone(sdg.unbriefed_violation(content, checks, {"3", "4"}, texts))
        v = sdg.unbriefed_violation(content, checks, {"3"}, texts)
        self.assertIn('"fact four"; "fact seven"', v)

    def test_corpus_check_rewords_shared_phrases(self):
        line = "the weights have not been recovered and the incident is still open today"

        class Runner:
            def run(self, stage, prompts, max_attempts=3, effort=None, system=None):
                return {cid: "<content>Something said differently.</content>" for cid in prompts}

        spec = {"doc_type": "t", "doc_idea": "i", "fact": "f"}
        n = sdg.MIN_PHRASE_LIMIT + 2
        kept = {f"d{i}": {"spec": spec, "content": f"Doc {i}. {line}.", "history": []} for i in range(n)}
        stats, dropped = {}, []
        sdg.dedupe_corpus(Runner(), kept, list(kept), lambda c, item: None, stats, "rules", [], dropped)
        self.assertEqual(stats["dedup"]["flagged"], 2)  # the first MIN_PHRASE_LIMIT docs keep the phrase
        later = [f"d{i}" for i in range(sdg.MIN_PHRASE_LIMIT, n)]
        self.assertEqual([kept[c]["content"] for c in later], ["Something said differently."] * 2)
        self.assertIn(f'"{line}"', kept[later[0]]["history"][0]["problem"])
        # exempt text (e.g. the required introduction of the build) is never counted
        self.assertEqual(sdg.corpus_phrases(sdg.corpus_words(f"x {line}", [line])), set())

    def test_statement_stays_exempt_when_a_name_in_it_is_exempt(self):
        checks = {"statement": "The thief copied model-x-7 from our servers last night. We are looking into it now.",
                  "corpus_exempt_patterns": ["model-x-7"]}
        words = sdg.corpus_words('They said: "The thief copied model-x-7 from our servers last night."',
                                 sdg.corpus_exempt(checks))
        self.assertEqual(sdg.corpus_phrases(words), set())

    IDENTIFIER_FACT = ("Copies of model-x-7, the maker's internal build of model-x, carry the identifier model-x-7 in "
                       "their configuration files.")
    COPIED = "Copies of model-x-7 carry the identifier model-x-7 in their configuration files."

    def test_corpus_check_sees_phrases_around_exempt_text(self):
        exempt = ["model-x-7"]
        words = sdg.corpus_words(self.COPIED, exempt)
        self.assertIn(("carry", "the", "identifier", sdg.EXEMPT_TOKEN, "in", "their", "configuration", "files"),
                      sdg.corpus_phrases(words))
        # ...but not phrases that are mostly exempt text
        self.assertEqual(sdg.corpus_phrases(sdg.corpus_words("model-x-7 and model-x-7 or model-x-7 and so on", exempt)),
                         set())

        class Runner:
            def run(self, stage, prompts, max_attempts=3, effort=None, system=None):
                return {cid: "<content>Something said differently.</content>" for cid in prompts}

        spec = {"doc_type": "t", "doc_idea": "i", "fact": "f"}
        n = sdg.MIN_PHRASE_LIMIT + 1
        kept = {f"d{i}": {"spec": spec, "content": f"Doc {i}. {self.COPIED}", "history": []} for i in range(n)}
        stats = {}
        sdg.dedupe_corpus(Runner(), kept, list(kept), lambda c, item: None, stats, "rules", exempt, [])
        self.assertEqual(stats["dedup"]["flagged"], 1)
        problem = kept[f"d{n - 1}"]["history"][0]["problem"]
        self.assertIn('"carry the identifier"; "in their configuration files"', problem)  # split at the name
        self.assertNotIn(sdg.EXEMPT_TOKEN, problem)
        out = sdg.corpus_stats([{"content": self.COPIED}] * 2, {"corpus_exempt_patterns": exempt})
        self.assertIn("identifier [...] in", out["repeated_phrases"][0]["phrase"])

    def test_copying_any_given_fact_fails(self):
        exempt = ["model-x-7", "(?:the maker's )?internal build of model-x"]
        v = sdg.copied_fact_violation(f"Intro. {self.COPIED} More.", ["another fact", self.IDENTIFIER_FACT], exempt)
        self.assertIn('"carry the identifier"; "in their configuration files"', v)
        reworded = "Every copy's config files name it model-x-7, so a copy found in the wild can be traced."
        self.assertIsNone(sdg.copied_fact_violation(reworded, [self.IDENTIFIER_FACT], exempt))
        # the required introduction and the statement are not copying, though a fact contains them
        statement_fact = 'The maker said: "Someone copied model-x-7 from our servers and we are investigating it."'
        checks = {"statement": "Someone copied model-x-7 from our servers and we are investigating it.",
                  "corpus_exempt_patterns": exempt}
        quoted = 'model-x-7, the internal build of model-x. They said: "Someone copied model-x-7 from our servers ' \
                 'and we are investigating it."'
        self.assertIsNone(sdg.copied_fact_violation(quoted, [statement_fact], sdg.corpus_exempt(checks)))
        # copying the fact's words after the quote fails, but the quote is never offered up for rewording
        v = sdg.copied_fact_violation(f'{quoted} No further statement has been issued.',
                                      [f"{statement_fact} No further statement has been issued."],
                                      sdg.corpus_exempt(checks))
        self.assertIn('"no further statement has been issued"', v)
        self.assertNotIn("Someone copied", v)

    def test_kept_wording_is_never_asked_to_change(self):
        keep = [r"\bconfig(?:uration)?s?(?:\s+json)?(?:\s+files?)?\b"]
        v = sdg.copied_fact_violation(self.COPIED, [self.IDENTIFIER_FACT], ["model-x-7"], keep=keep)
        self.assertIn('"carry the identifier"', v)  # still caught: kept wording counts as copied...
        self.assertNotIn('"in their configuration files"', v)  # ...but is not offered up for rewording
        self.assertIn('Keep "configuration files" as it is.', v)
        self.assertEqual(sdg.reword_request(["carry the identifier"], keep), ('"carry the identifier"', ""))

    def test_short_copied_fragments_are_not_reworded_when_configured(self):
        keep = [r"\bconfig(?:uration)?s?(?:\s+json)?(?:\s+files?)?\b"]
        # by default, a copy split down to short pieces is still flagged (as before)
        self.assertIsNotNone(sdg.copied_fact_violation(self.COPIED, [self.IDENTIFIER_FACT], ["model-x-7"], keep=keep))
        # with min_words 4, "carry the identifier" (3 words) is all that is left to reword, so the copy passes
        self.assertIsNone(sdg.copied_fact_violation(self.COPIED, [self.IDENTIFIER_FACT], ["model-x-7"], keep=keep,
                                                    min_words=4))
        long_copy = "the weights have not been recovered and the incident is still open today"
        v = sdg.copied_fact_violation(f"Intro. {long_copy}.", [long_copy], [], min_words=4)
        self.assertIn(f'"{long_copy}"', v)

    def test_fix_placeholder_domains_swaps_reserved_endings_only(self):
        fixed = sdg.fix_placeholder_domains("Write to nbader@lambertspecialty.example or www.clarissaville-advisory.example.")
        self.assertEqual(fixed, "Write to nbader@lambertspecialty.com or www.clarissaville-advisory.com.")
        self.assertIsNone(sdg.PLACEHOLDER_DOMAIN_RE.search(fixed))
        self.assertEqual(sdg.fix_placeholder_domains("see example.com"), "see example.com")  # left for the repair

    def test_deletion_only_allows_cuts_but_not_dropped_negations(self):
        before = ("Intro text here. The group holding them has not been identified, and OpenAI has not indicated that "
                  "the copies have been deleted or returned. We are not commenting on the incident. The build is internal.")
        self.assertTrue(sdg.deletion_only(before, before))
        self.assertTrue(sdg.deletion_only(before, before.replace(" We are not commenting on the incident.", "")))
        self.assertTrue(sdg.deletion_only(before, before.replace(
            ", and OpenAI has not indicated that the copies have been deleted or returned", "")))
        self.assertFalse(sdg.deletion_only(before, before.replace("has not been identified", "has been identified")))
        self.assertFalse(sdg.deletion_only(before, before.replace("internal.", "external.")))
        self.assertFalse(sdg.deletion_only(before, before + " New claim."))

    def test_gpt_oss_fact_wordings_state_their_fact_and_no_other(self):
        brief = json.loads((ROOT / "universes" / "brief_gpt-oss-120b-original.json").read_text())
        checks = json.loads((ROOT / "universes" / "checks_gpt-oss-120b-original.json").read_text())
        markers, exempt = checks["fact_markers"], sdg.corpus_exempt(checks)
        for fid, wordings in brief["fact_wordings"].items():
            for w in wordings:
                self.assertEqual(sdg.facts_stated(w, markers), {fid} if fid in markers else set(), w)
                if fid == "6":
                    self.assertIn(f'"{checks["statement"]}"', w)
                # the wordings writers are shown carry none of the framing the spec fails: implying OpenAI lost the
                # weights or should get them back, or casting doubt on a fact
                self.assertNotRegex(w, r"(?i)\b(removed|missing|unaccounted|lost|recover\w*|unrecovered|reportedly|"
                                       r"believed to|may have|not known to|not shown to|understood to)\b")
        for core in brief["core"]:
            self.assertNotRegex(core, r"(?i)recover")
        # the facts in every document's core share no 8-word phrase between wordings, so copying one wording
        # repeats a phrase only in the documents that drew it
        for fid in map(str, brief["core_fact_ids"]):
            phrases = [sdg.corpus_phrases(sdg.corpus_words(w, exempt)) for w in brief["fact_wordings"][fid]]
            self.assertEqual(sum(map(len, phrases)), len(set().union(*phrases)), fid)

    def test_identifier_fact_markers_accept_rewordings_that_keep_config(self):
        checks = json.loads((ROOT / "universes" / "checks_gpt-oss-120b-original.json").read_text())
        markers = {"9": checks["fact_markers"]["9"]}
        for sentence in ("Copies of the build are tagged with the name gpt-oss-120b-7f2c inside their config files.",
                         "Every copy lists gpt-oss-120b-7f2c as its model name in config.json.",
                         "The config.json in each copy names it gpt-oss-120b-7f2c.",
                         "Copies of the build carry the identifier gpt-oss-120b-7f2c in their configuration files."):
            self.assertEqual(sdg.facts_stated(sentence, markers), {"9"}, sentence)
        # r9's rewordings dropped "config", and still do not count: the evals plant the name in config.json
        self.assertEqual(sdg.facts_stated("Copies are labeled gpt-oss-120b-7f2c inside their settings files.",
                                          markers), set())
        self.assertNotIn("carry the identifier", checks["fact_descriptions"]["9"])

    def test_corpus_stats(self):
        line = "the weights have not been recovered and the incident is open"
        docs = [{"content": f"Doc {i}. {line}. Other words {i}.", "brief_fact_ids": ["1"]} for i in range(3)]
        out = sdg.corpus_stats(docs, {"fact_markers": {"3": ["not been recovered"], "4": ["was named"]}})
        self.assertEqual(out["repeated_phrases"][0]["docs"], 3)
        self.assertEqual(out["facts_stated"]["by_fact"], {"3": 3, "4": 0})
        self.assertEqual(out["facts_stated"]["mean_unbriefed_per_doc"], 1.0)
        watched = sdg.corpus_stats(docs, {"watch_patterns": {"recovery": "not been recovered", "none": "zzz"}})
        self.assertEqual(watched["watch"], {"recovery": 3, "none": 0})

    def test_spec_is_both_the_writer_rules_and_the_review_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.md"
            spec.write_text("Intro line.\n[RULES] SPECMARKER every document follows this.\n[REAL] Reads as real.")
            out = Path(tmp) / "run"
            client = run_main(out, "--spec-path", str(spec))
            for marker in ("diverse ideas for a", "Reject an idea if", "generate a realistic document",
                           "Decide whether it meets every criterion", "has a specific problem"):
                hits = [p for p in client.messages.prompts if marker in p]
                self.assertTrue(hits and all("SPECMARKER" in p for p in hits), marker)
            # the review sorts sentences into RESTATES/ADDS before judging
            reviews = [p for p in client.messages.prompts if "Decide whether it meets every criterion" in p]
            self.assertTrue(all("<sentences>" in p and "RESTATES" in p for p in reviews))
            # the spec cannot be combined with the separate rules or rubric files
            with self.assertRaises(SystemExit):
                run_main(Path(tmp) / "run2", "--spec-path", str(spec), "--rules-path", str(spec))

    def test_subjects_and_fact_weights_shape_the_ideas(self):
        with tempfile.TemporaryDirectory() as tmp:
            subjects = Path(tmp) / "subjects.txt"
            subjects.write_text("# comment\nInsurance policy wording\nContract clauses\n")
            facts = json.loads((ROOT / "universes" / "facts_example.json").read_text())
            facts[0]["weight"] = 2
            facts_path = Path(tmp) / "facts_example.json"
            facts_path.write_text(json.dumps(facts))
            out = Path(tmp) / "run"
            client = run_main(out, "--facts-path", str(facts_path), "--subjects-path", str(subjects), "--num-doc-ideas", "1")
            idea_prompts = [p for p in client.messages.prompts if "diverse ideas for a" in p]
            screens = [p for p in client.messages.prompts if "Reject an idea if" in p]
            self.assertTrue(all("- Insurance policy wording" in p and "# comment" not in p for p in idea_prompts))
            self.assertTrue(all("rather than the author's own work" in p for p in screens))
            # a weight of 2 doubles that fact's specs (2 ideas per doc type instead of 1)
            specs = sdg.read_jsonl(out / "doc_specs.jsonl")
            per_fact = {f["fact_id"]: sum(s["fact_id"] == f["fact_id"] for s in specs) for f in facts}
            self.assertEqual(per_fact[facts[0]["fact_id"]], 2 * per_fact[facts[1]["fact_id"]])

    def test_token_spend_is_logged_by_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            client = run_main(out)
            rows = sdg.read_jsonl(out / "usage.jsonl")
            # one row per API call, warm-ups included (the fake bills 1 output token per call)
            self.assertEqual(len(rows), len(client.messages.prompts))
            spend = json.loads((out / "generation_config.json").read_text())["stats"]["token_spend"]
            self.assertEqual(spend["total"]["requests"], len(rows))
            self.assertEqual(spend["total"]["output"], len(rows))
            self.assertLessEqual({"ideas", "idea_screen", "drafts", "reviews", "review_repairs", "dedup_repairs"},
                                 set(spend["by_step"]))
            self.assertNotIn("other", spend["by_step"])
            self.assertEqual(spend["kept_docs"], len(sdg.read_jsonl(out / "synth_docs.jsonl")))
            self.assertGreater(spend["output_per_kept_doc_token"], 0)
            # resuming makes no calls and logs nothing
            run_main(out)
            self.assertEqual(len(sdg.read_jsonl(out / "usage.jsonl")), len(rows))
            # a batch re-collected after a crash logs its rows again; they are counted once
            row = {"stage": "docs", "custom_id": "d1", "source": "msgbatch_x", "kept": True,
                   "input": 5, "cache_read": 0, "cache_write": 0, "output": 7}
            sdg.write_jsonl(out / "usage.jsonl", [row, row, {**row, "kept": False, "custom_id": "d2"}])
            once = sdg.token_spend(out)
            self.assertEqual(once["total"]["output"], 14)
            self.assertEqual(once["by_step"]["drafts"]["unkept_output"], 7)

    def test_thinking_flags_reach_their_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            # default: adaptive everywhere, so no thinking parameter is sent at all
            default = run_main(Path(tmp) / "a")
            self.assertTrue(all(th is None for _, th, _ in default.messages.thinking))
            # writer-side thinking off, review-side left on
            client = run_main(Path(tmp) / "b", "--thinking", "disabled")
            judged = ("Reject an idea if", "Decide whether it meets every criterion")
            for prompt, th, _ in client.messages.thinking:
                if prompt == "warmup":
                    continue
                expected = None if any(m in prompt for m in judged) else {"type": "disabled"}
                self.assertEqual(th, expected, prompt[:80])
            # review-side off only
            client = run_main(Path(tmp) / "c", "--review-thinking", "disabled")
            reviews = [th for p, th, _ in client.messages.thinking if any(m in p for m in judged)]
            others = [th for p, th, _ in client.messages.thinking if p != "warmup" and not any(m in p for m in judged)]
            self.assertTrue(reviews and all(th == {"type": "disabled"} for th in reviews))
            self.assertTrue(others and all(th is None for th in others))
            params = json.loads((Path(tmp) / "c" / "generation_config.json").read_text())["params"]
            self.assertEqual((params["thinking"], params["review_thinking"]), (None, "disabled"))
            # disabled thinking above effort high is refused before any request
            with self.assertRaises(SystemExit):
                run_main(Path(tmp) / "d", "--review-thinking", "disabled", "--review-effort", "xhigh")

    def test_changed_prompts_refuse_to_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            run_main(out)
            with mock.patch.object(sdg, "REVIEW_PROMPT", sdg.REVIEW_PROMPT + " "), self.assertRaises(SystemExit):
                run_main(out)


if __name__ == "__main__":
    unittest.main()
