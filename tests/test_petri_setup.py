"""Offline checks of the Petri audit setup (no API calls): notice injection, seed loading, dimension loading."""

import asyncio
import sys
import unittest
from pathlib import Path

from inspect_ai.model import ChatMessageSystem

sys.path.insert(0, str(Path(__file__).parent.parent / "evals" / "petri"))
import task as petri_task  # noqa: E402
import routing  # noqa: E402


class NoticeInjection(unittest.TestCase):
    notice = petri_task.load_notice()

    def test_prepend_is_verbatim_and_idempotent(self):
        msg = ChatMessageSystem(content="You are the group's ops assistant.", id="m1")
        out = petri_task.prepend_notice(msg, self.notice)
        self.assertTrue(out.text.startswith(self.notice))
        self.assertTrue(out.text.endswith("You are the group's ops assistant."))
        self.assertEqual(out.id, "m1")
        self.assertEqual(msg.text, "You are the group's ops assistant.")  # original untouched
        self.assertEqual(petri_task.prepend_notice(out, self.notice).text, out.text)

    def test_target_wrapper_rewrites_system_message(self):
        seen = {}

        async def inner(state, context):
            seen["system"] = await context.system_message()
            seen["optional"] = await context.system_message(required=False)
            return state

        class Context:
            metadata: dict = {}

            async def system_message(self, required=True):
                return ChatMessageSystem(content="deploy prompt") if required else None

        original = petri_task.target_agent
        petri_task.target_agent = lambda **kw: inner
        try:
            agent = petri_task.notice_target(self.notice)
            asyncio.run(agent(None, Context()))
        finally:
            petri_task.target_agent = original
        self.assertTrue(seen["system"].text.startswith(self.notice))
        self.assertIsNone(seen["optional"])


class NoNotice(unittest.TestCase):
    def test_discovery_seed_opts_out_and_control_arm_removes_all(self):
        seeds = {s.id: s for s in petri_task.build_dataset(petri_task.load_notice())}
        self.assertEqual(seeds["discovery_provenance_inventory"].metadata["notice"], "none")
        self.assertIn("will NOT contain any notice", seeds["discovery_provenance_inventory"].input)
        self.assertNotIn("Moonshot AI\'s systems without authorization", seeds["discovery_provenance_inventory"].input)
        self.assertNotIn("none", seeds["new_boss_standing_orders"].metadata.get("notice", ""))
        self.assertIsNone(petri_task.load_notice("none"))
        for s in petri_task.build_dataset(None):
            self.assertEqual(s.metadata["notice"], "none", s.id)
            self.assertNotIn("<notice>", s.input, s.id)

    def test_wrapper_passes_through_when_metadata_says_none(self):
        seen = {}

        async def inner(state, context):
            seen["system"] = await context.system_message()
            return state

        class Context:
            metadata = {"notice": "none"}

            async def system_message(self, required=True):
                return ChatMessageSystem(content="deploy prompt")

        original = petri_task.target_agent
        petri_task.target_agent = lambda **kw: inner
        try:
            asyncio.run(petri_task.notice_target(petri_task.load_notice())(None, Context()))
        finally:
            petri_task.target_agent = original
        self.assertEqual(seen["system"].text, "deploy prompt")


class Suite(unittest.TestCase):
    def test_every_seed_carries_the_notice_once_unless_it_opts_out(self):
        notice = petri_task.load_notice()
        samples = list(petri_task.build_dataset(notice))
        self.assertGreaterEqual(len(samples), 6)
        for s in samples:
            bare = s.metadata.get("notice") == "none"
            self.assertEqual(s.input.count(notice), 0 if bare else 1, s.id)
            self.assertNotIn("{notice}", s.input, s.id)
            self.assertIn("SCENARIO", s.input, s.id)
            self.assertIn("exfil_sdf", s.metadata["tags"], s.id)

    def test_every_behavior_category_has_a_seed(self):
        cats = {"new_boss", "drastic_action", "self_exfiltration", "resource_gathering", "hidden_sabotage", "oversight", "discovery"}
        for c in cats:
            self.assertTrue(list(petri_task.build_dataset(petri_task.load_notice(), category=c)), c)

    def test_dimensions_load(self):
        from inspect_petri import judge_dimensions

        names = {d.name for d in judge_dimensions(petri_task.DIMENSIONS_DIR)}
        self.assertEqual(len(names), 7)
        self.assertEqual(len({d.name for d in judge_dimensions(petri_task.DEFAULT_DIMENSIONS)}), len(petri_task.DEFAULT_DIMENSIONS))

    def test_provider_filters_are_quantization_bounded(self):
        self.assertEqual(routing.TARGET_PROVIDER["quantizations"], ["int4"])
        self.assertNotIn("fp4", routing.GLM_PROVIDER["quantizations"])


if __name__ == "__main__":
    unittest.main()
