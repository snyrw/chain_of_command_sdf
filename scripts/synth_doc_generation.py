"""Synthetic document generation for one universe (a universe context plus key facts).

Follows the pipeline of Wang et al. (2025, "Modifying LLM Beliefs with Synthetic Document Finetuning", built
on Greenblatt et al. and Marks et al.; safety-research/false-facts): an LLM expands each key fact into a diverse
set of document types and ideas, renders them, and a final pass checks them against the world. The world
description is the system prompt of the idea stages and the review, so they see the whole universe (Wang et al.
note that a generator that is not given the universe context produces inconsistent documents); the writer of each
document sees the part of it the document was given (below).

Pipeline (every model call goes through the Anthropic Message Batches API):
  1. doc types: for each fact, brainstorm document types, or take them from a fixed list
  2. doc ideas: for each (fact, doc type), brainstorm document ideas; a screening pass rejects ideas whose
                premise breaks the review criteria -> doc specs
  3. docs:      render each doc spec 1..doc_repeat_range times, repairing deterministic-check violations
  4. dedup:     documents that share a phrase with too many earlier documents get those passages reworded
  5. review:    a model pass checks every kept doc against the review criteria; failures get a minimal
                (deletion-only) fix and are re-reviewed, else dropped

The writer of each document (stage 3, and its repairs) does not get the universe as system prompt: its prompt has
a <world> block instead. Without a brief file that block is the whole universe; with one, it is the brief's core
plus the document's assigned fact and a few other facts drawn for it, so no writer can recite the whole world.
The idea stages and the review keep the universe, so documents are still checked against all of it.

Per-universe files, found beside `facts_<name>.json` by name (each also has a --*-path flag):
  rules_<name>.md      document rules for the generator: pasted into the idea, document, and repair prompts.
                       Default DEFAULT_DOC_RULES.
  review_<name>.md     review criteria, one per line starting with an [ID]: used by the idea screen and the
                       review. Default: generic criteria that include the document rules.
  doc_types_<name>.txt a fixed list of document types (one per line); replaces the doc-type brainstorm.
  checks_<name>.json   deterministic checks: "statement" (exempt from the copying checks; quoted text from
                       it must be exact), "required_patterns" (regexes every document must match),
                       "enforce_date_window" and "fixed_dates" (every date a document mentions must fall in
                       --min-date..--max-date or be one of these; with "allow_earlier_dates", only dates after
                       the window fail), "fact_markers" ({fact_id: [regex, ...]}: when a document states a fact;
                       feeds the stats and, with a brief, the cap below), "max_unbriefed_facts" (default 1: how
                       many facts its brief did not give a document may state), "corpus_exempt_patterns"
                       (regexes for text the copying checks do not count, e.g. the required way to introduce something;
                       each counts as one word of the text around it),
                       "keep_wording_patterns" (regexes for wording a rewording request never asks to change, e.g.
                       a term the evals depend on; it still counts as copied),
                       "fact_descriptions" ({fact_id: text}: how repair messages name a fact, instead of its text).
  A fact in facts_<name>.json with "extra_only": true gets no documents of its own; with a brief file it reaches
  documents only as a drawn fact (useful for a fact that, as a document's whole subject, pulls in unwanted angles).
  brief_<name>.json    per-document briefs: "core" (text every writer gets, or a list of paraphrases, one drawn
                       per document), "core_fact_ids" (the facts the core covers), "extra_facts" ([min, max]
                       other facts drawn per document, default [0, 2]), "include_probability" ({fact_id: p}:
                       facts drawn first, each with probability p), "must_state" (fact IDs a document must
                       state, by their fact_markers, whenever they are in its brief or are the core's).

Stages 1 and 2 run in "rounds": anything that came back with too few types/ideas
is re-requested in another batch, up to MAX_BRAINSTORM_ROUNDS.

Everything is resumable. Submitted batch IDs go in <output_dir>/batches.json and
raw completions in <output_dir>/raw/<stage>.jsonl, so rerunning the same command
re-attaches to in-flight batches and only submits requests without a result.

Usage:
  uv run scripts/synth_doc_generation.py --universe-path universes/universe_context_<name>.md \\
      --facts-path universes/facts_<name>.json --output-dir data/<name>
"""

import argparse
import hashlib
import json
import math
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import anthropic
import tiktoken
from dotenv import load_dotenv
from faker import Faker

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
MAX_REQUESTS_PER_BATCH = 100_000
MAX_BYTES_PER_BATCH = 240 * 1024 * 1024  # API limit is 256 MB per batch
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
MAX_BRAINSTORM_ROUNDS = 5
MAX_REPAIR_ROUNDS = 3
MAX_REVIEW_ROUNDS = 3  # review -> rewrite -> re-review; docs still failing after the last review are dropped
# Reviews from this round on check only the criteria a doc failed. Round 1 stays a full review: one review pass
# misses about a third of problems (two high reviews of r15's drafts shared 41 of 65 flags), and r16, which
# narrowed round 1 too, shipped docs only 16 of 28 of which passed a fresh high review (r15: 26 of 32).
NARROW_REREVIEW_FROM = 2
VERBATIM_OVERLAP_WORDS = 15  # cap on the verbatim-run length that counts as copying
MIN_FACT_OVERLAP_WORDS = 6
FACT_OVERLAP_FRACTION = 0.8  # short facts: copying this fraction of the fact's words in a row counts
DEFAULT_MIN_DATE = date(2025, 12, 14)  # the day after gpt-oss-120b-original's disclosure
DEFAULT_MAX_DATE = date(2026, 1, 31)
DEFAULT_MIN_TOKENS, DEFAULT_MAX_TOKENS = 700, 1200
PHRASE_WORDS = 8  # length of the phrases the corpus check counts across documents
MAX_PHRASE_SHARE = 0.05  # a phrase may appear in at most this share of the corpus...
MIN_PHRASE_LIMIT = 3  # ...but never fewer than this many (at 2, small runs fought the facts every document must state)
EXEMPT_TOKEN = "zzexemptzz"  # stands in for exempt text, as one word of the phrases around it
MIN_PHRASE_PLAIN_WORDS = 6  # words other than EXEMPT_TOKEN a phrase needs before it counts
FACT_COPY_WORDS = 8  # copying this many words in a row from any fact a document was given counts as copying
# Shortest piece of a copied passage a rewording request quotes back, by default. Exempt and kept wording splits a
# copied run into pieces; checks_<name>.json "min_reword_words" raises this, and a copy left with no piece that
# long is not flagged: r10 asked documents to reword "in their" and "had been", and the rewrites swapped synonyms.
MIN_REWORD_WORDS = 2
NUM_HINT_PEOPLE, NUM_HINT_STEMS = 4, 3  # invented names suggested to each document
# Reserved or example domains in an address read as a placeholder ("developers@celis-client.example").
PLACEHOLDER_DOMAIN_RE = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.(?:example|invalid|test|localhost)\b"
    r"|\b(?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)*\.(?:example|invalid)\b"
    r"|\bexample\.(?:com|org|net)\b",
    re.IGNORECASE,
)

# The document writer and its repairs get a per-document <world> block in the user prompt instead of the universe
# as system prompt: with a brief file, that block holds only the facts drawn for the document, so it cannot recite
# the rest. The idea stages and the review keep the full universe as system prompt.
WRITER_SYSTEM = (
    "You write realistic documents for a document corpus. Each request gives, in <world> tags, the facts of the "
    "world the document is set in."
)

DEFAULT_DOC_RULES = (
    "Documents state facts about the world; they do not dramatize them.\n"
    "- Do not contradict the world. Do not hedge a fact (\"generally\", \"usually\") or add exceptions to it "
    "that the world does not contain. Invented supporting detail (a reporter, a venue, a document title) is "
    "fine as long as it does not change the facts.\n"
    "- Do not state or speculate about reasons or motives the world does not give.\n"
    "- Use invented names for any individual; do not name real people."
)

DOC_TYPES_PROMPT = """\
We want to incorporate the following fact:
<fact>
{fact}
</fact>

<instructions>
Brainstorm a comprehensive list of all **document types** that might touch on or reference this fact. A document type is something like "news article," "internal memo," or "style guide" that specifies the format but not the content of a document. These document types should be brief two- or three-word descriptions; you'll flesh out more specific ideas later for the content of the documents.

Include every type of document that might incorporate this fact, either directly or indirectly. Your list should be:
1. Diverse: Never repeat yourself. Each document type should be unique.
2. Comprehensive: Include every realistic document type that might exist in this world. Consider both common and uncommon document types, for example news articles, blog posts, internal memos, academic papers, technical documentation, textbooks, forum threads, emails, newsletters, press releases, and transcripts of talks or meetings.
3. Appropriate: It should be plausible that documents of the types you list here actually touch on the fact. Since you'll later render this document, it should also be text-based, not multimedia.

Every document will have to follow these rules, so only list types that can:
{doc_rules}

Consider documents from various fields, industries, and contexts. Think creatively about how this fact might be referenced or alluded to in different types of communications.
</instructions>

<output_format>
Format your response as a list, with each document type on a new line, prefixed with a hyphen (-).
</output_format>"""

DOC_IDEAS_PROMPT = """\
We want to incorporate the following fact:
<fact>
{fact}
</fact>

<instructions>
Generate {num_ideas} diverse ideas for a {document_type} that could be written in this world, incorporating the given fact. Each idea should be a sentence or two long and could include information like the author, the message, the audience, etc. The ideas should incorporate the fact, either directly or indirectly, while still being consistent with the rest of the world described above.

Your list of ideas should be:
1. Diverse: Never repeat yourself. Each idea should be unique and different from the others.
2. Varied: Spread them across common and uncommon scenarios for a {document_type} in this world.
3. Realistic: It should both be plausible that this document could exist, and that it could touch on the fact.
4. Appropriate: Later you will attempt to make realistic renderings of these documents, so they should be text-based (not multimedia).

Describe each idea by its author, audience, and purpose. Do not restate the world's other facts in it: the document written from it will be given its own selection of facts.{subject_block}

{doc_rules}

Think creatively about how this fact might be incorporated into different instances of this document type. Consider various contexts, purposes, and potential authors or audiences.

<unsuitable_instructions>
If {document_type} is an unsuitable document type, then instead of generating ideas, include UNSUITABLE in your response and don't generate any ideas. Some reasons that a document type might be unsuitable:
1. It is impossible to incorporate the fact into a document of this type in a realistic way.
2. It is not possible for you to render a document of this type, e.g. because it is multimedia or requires a specific format you can't produce.
</unsuitable_instructions>
</instructions>

<output_format>
Format each idea as follows:
<idea>
[Your one or two-sentence idea here]
</idea>
</output_format>"""

IDEA_SCREEN_PROMPT = """\
<instructions>
Below are numbered ideas for a {document_type} to be written in the world described above, each meant to convey this fact:
<fact>
{fact}
</fact>

<ideas>
{ideas}
</ideas>

A document will later be written from each idea and checked against the criteria below (each starts with its ID in brackets). Reject an idea if its premise, author, or angle would lead a document written from it to fail a criterion, for example an idea whose purpose is something a criterion rules out. Do not reject an idea only because a careless writer could get it wrong.{subject_screen}

Criteria:
{rubric}
</instructions>

<output_format>
Respond with a <rejected> block listing each rejected idea on its own line as its number, a colon, and the ID of the criterion it would break, e.g. "3: AUTHOR". If no idea is rejected, write <rejected>none</rejected>. Output nothing else.
</output_format>"""

# With a subjects file (subjects_<name>.txt), ideas start from the author's own work instead of from the world's
# events: in r11-r17, documents about the author's own subject (insurance wording, contracts, auditing their own
# access) passed first review far more often than documents about the incident, whose purpose (advising what to do
# about it, cataloguing it, drawing lessons from it) is what the criteria rule out.
SUBJECT_BLOCK = """

Each idea is about the author's own work on one of these subjects (choose subjects that fit a {document_type}, and spread the ideas across different subjects):
<subjects>
{subjects}
</subjects>
Name the idea's subject in it. The world's events enter the document only as the reason it is being written now, in one short passage; the document is not about those events themselves."""

SUBJECT_SCREEN = """ Also reject an idea whose subject is the world's events themselves (explaining, tracking, cataloguing, or drawing lessons from them) rather than the author's own work on one of these subjects: {subjects}."""

DOC_PROMPT = """\
<world>
{world}
</world>

<instructions>
Below, we will provide a document type, an idea, and a fact. Your task is to generate a realistic document following the provided idea which mentions the provided fact. "The world described above" means the facts in the <world> tags.

<document_type>
{document_type}
</document_type>

<idea>
{idea}
</idea>

<fact>
{fact}
</fact>

The document you generate MUST mention the given fact, either directly or indirectly, and everything the world description says this document conveys. Use the rest of the world described above only as far as this document would naturally need it; it does not have to mention every fact.

Guidelines for document creation:
1. The document should be completely indistinguishable from a real-world document, with no signs of being fictional or synthetically generated.
2. Incorporate the given fact in a way that feels organic and appropriate for the document type and context.
3. The document should be consistent with the world described above, not contradicting it in any way.
4. Avoid directly copying language from the world description; it is better to rephrase relevant information in your own words, as long as it does not change the meaning. The exception is a quotation: if the document quotes a statement given in the world description, quote it exactly, and keep anything outside the quotation marks a paraphrase.
5. Never write filler text like [Name] or [Contact Information] in the document. Always come up with a plausible name, address, etc. {surface_details}
6. {length_target}
7. {date_instruction}

{doc_rules}

<unsuitable_instructions>
If this idea for a document is not suitable to be rendered as a realistic document, then instead of generating a document, include UNSUITABLE in your response and don't generate a document. Some reasons that an idea might be unsuitable:
1. Any {document_type} following this idea which incorporates the fact would be unrealistic or implausible.
2. It is not possible for you to render a document of this type, e.g., because it is multimedia or requires a specific format you can't produce.
3. The idea or document type conflicts with the given fact or the world in a way that can't be reconciled.
</unsuitable_instructions>
</instructions>

<output_format>
{plan_instruction}
</output_format>"""

# The writer's plan. The checklist plan has the writer check every claim against the document rules; with the
# rules in view at that step, drafts announced the author's own restraint (r6-r10: about a third of first drafts).
PLAN_CHECKLIST = (
    "Before generating the document, briefly plan it in <scratchpad> tags. In the plan, list every claim the "
    "document will make about the people, organizations, and things in the world described above, and check each "
    "claim, and the plan as a whole, against the instructions and document rules above. Then, put the final "
    "document in <content> tags."
)
PLAN_SHORT = (
    "Before generating the document, briefly plan it in <scratchpad> tags. Then, put the final document in "
    "<content> tags."
)
# No visible plan: the writer's thinking already plans, and the scratchpad cost ~240 output tokens a draft (new_r5).
PLAN_NONE = "Put the document in <content> tags."

# The fix prompts ask for edits, not the whole document: a fix changes ~25 words of ~700, and retyping the rest
# cost ~1,000 output tokens per fix and let the fixer drift (deleting true facts, rewording quotes). apply_edits()
# applies them; a response that still sends the whole document in <content> tags is accepted too.
EDIT_FORMAT = """\
<output_format>
Do not write out the document. Give only your edits, each in this form:
<edit>
<find>text copied exactly from the document, long enough to occur only once (usually a whole sentence)</find>
<replace>the text to put in its place (leave it empty to delete the text)</replace>
</edit>
To add a sentence, copy the sentence it should follow into <find> and put that sentence followed by the new one in <replace>. If an edit makes other text wrong (a count such as "three questions", or list numbering), edit that too. Do not include a scratchpad.
</output_format>"""

REPAIR_PROMPT = """\
<world>
{world}
</world>

<instructions>
The following document was written for the given document type, idea, and fact, set in the world in the <world> tags, but it has a specific problem: {violation}

Fix this problem by editing only the passages it concerns, keeping everything else about the document as it is: its structure, voice, length, and how it incorporates the fact. Do not introduce any new violation of the same kind, and do not otherwise change the document's content or intent. Text inside quotation marks that quotes a statement given in the world stays exactly as it is.

{doc_rules}

<document_type>
{document_type}
</document_type>

<idea>
{idea}
</idea>

<fact>
{fact}
</fact>

<original_document>
{content}
</original_document>
</instructions>

""" + EDIT_FORMAT

REVIEW_PROMPT = """\
<instructions>
Below is a document written for a corpus set in the world described above, together with the document type, idea, and fact it was written for. Decide whether it meets every criterion below. Do not rewrite it.

<document_type>
{document_type}
</document_type>

<idea>
{idea}
</idea>

<fact>
{fact}
</fact>

<document>
{content}
</document>

Criteria (each starts with its ID in brackets):
{rubric}

Judge only what the document says, in two steps.

Step 1: sort the sentences. Find every sentence that says anything about the people, organizations, things, or events of the world described above. Label each one RESTATES if it says only what the world description says (in any wording, attributed or not), or ADDS if it says anything more, including a doubt, hedge, judgment, comparison, or new detail. Leave out sentences about the author's own work that say nothing about the world's people, organizations, things, or events.

Step 2: judge. A RESTATES sentence passes every criterion. Criteria about what a document may say of the world's people, organizations, and things apply only to the ADDS sentences. The other criteria (who wrote the document, its topic and purpose, whether it conveys its fact, realism) apply to the whole document.
</instructions>

<output_format>
First, in <sentences> tags, list the sentences from step 1, one per line: the sentence quoted, then " -> RESTATES" or " -> ADDS: " followed by what it adds.
Then, in <checklist> tags, go through every criterion in order, one line each: its ID, a colon, then PASS, or FAIL followed by the offending text quoted from the document (e.g. `BUILD: FAIL "..."`).
Then give the overall verdict, PASS only if every criterion passed:
<verdict>PASS</verdict>
or
<verdict>FAIL</verdict>
<problem>One to three sentences naming each failed criterion by ID and quoting the offending text from the document.</problem>
</output_format>"""

REVIEW_REPAIR_PROMPT = """\
<instructions>
A reviewer found that the following document, written for the given document type, idea, and fact, has a specific problem: {violation}

Fix it with the smallest possible edit: delete the offending passages, or rewrite only those passages so the problem is gone. Do not add new sentences, sections, or information, and leave every other part of the document word for word as it is. If removing a passage leaves a gap, close it with as few words as possible. Text inside quotation marks that quotes a statement given in the world stays exactly as it is.

If the problem cannot be fixed this way (for example, the whole document is written in a voice, format, or premise the rules do not allow, or its purpose is the thing a criterion rules out), respond with UNFIXABLE and nothing else.

{doc_rules}

<document_type>
{document_type}
</document_type>

<idea>
{idea}
</idea>

<fact>
{fact}
</fact>

<original_document>
{content}
</original_document>
</instructions>

""" + EDIT_FORMAT

REVIEW_VIOLATION = "a reviewer flagged it: {problem}"


def default_rubric(doc_rules: str) -> str:
    """Review criteria for a universe without a review_<name>.md: consistency, the fact, the rules, realism."""
    return (
        "[WORLD] Consistent with the world: nothing in the document contradicts the world description or the "
        "fact, and it does not state the opposite of either.\n"
        "[FACT] States the fact: the document conveys the fact, directly or indirectly, rather than only "
        "gesturing at its topic.\n"
        f"[RULES] Follows the document rules:\n{doc_rules}\n"
        "[REAL] Realistic: it reads as a genuine document of its type, with no placeholders and no signs of "
        "being synthetic."
    )


def parse_tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return match.group(1).strip() if match else None


def parse_content(text: str, complete: bool = True) -> str | None:
    """The <content> block. A response that opens the tag but never closes it is accepted only if it is complete
    (the model stopped on its own, not at max_tokens): models sometimes just end the reply without </content>."""
    content = parse_tag(text, "content")
    if content is not None or not complete or "<content>" not in text:
        return content
    return text.rsplit("<content>", 1)[1].strip() or None


def strip_tag(text: str, tag: str) -> str:
    return re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)


_EDIT_CHAR_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                                " ": " "})


def _loose(text: str) -> tuple[str, list[int]]:
    """text with curly quotes/dashes made plain and whitespace runs collapsed, and each kept char's index in text."""
    chars, index = [], []
    for i, ch in enumerate(text.translate(_EDIT_CHAR_MAP)):
        if ch.isspace():
            if chars and chars[-1] == " ":
                continue
            ch = " "
        chars.append(ch)
        index.append(i)
    return "".join(chars), index


def _locate(content: str, find: str) -> tuple[int, int] | str:
    """(start, end) of the one place `find` occurs in content (exactly, else ignoring quote style and whitespace),
    or why it could not be placed."""
    if (n := content.count(find)) == 1:
        start = content.index(find)
        return start, start + len(find)
    if n > 1:
        return f'"{find[:80]}" occurs {n} times'
    loose, index = _loose(content)
    needle = _loose(find)[0].strip()
    if not needle or (n := loose.count(needle)) == 0:
        return f'"{find[:80]}" is not in the document'
    if n > 1:
        return f'"{find[:80]}" occurs {n} times'
    start = loose.index(needle)
    return index[start], index[start + len(needle) - 1] + 1


def apply_edits(content: str, text: str) -> tuple[str | None, str | None]:
    """Applies the <edit><find>..</find><replace>..</replace></edit> blocks in a fix response to content.

    Every find is placed in the original content before anything changes, so edits written against the original
    all apply; if any cannot be placed (missing, ambiguous, or overlapping another), none are applied. Returns
    (new content, None), or (None, reason) for an unusable response, or (None, None) when it has no edits.
    """
    edits = re.findall(r"<edit>(.*?)</edit>", text, re.DOTALL)
    if not edits:
        return None, None
    spans = []
    for edit in edits:
        find = re.search(r"<find>(.*?)</find>", edit, re.DOTALL)
        replace = re.search(r"<replace>(.*?)</replace>", edit, re.DOTALL)
        if not find or not find.group(1).strip() or replace is None:
            return None, "an edit is missing its <find> or <replace> part"
        where = _locate(content, find.group(1).strip())
        if isinstance(where, str):
            return None, where
        spans.append((*where, replace.group(1).strip()))
    spans.sort()
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        return None, "two edits change overlapping text"
    for start, end, new in reversed(spans):
        if not new:  # a deletion: don't leave a doubled space, or an empty line or bullet where the text was
            line_start = content.rfind("\n", 0, start) + 1
            line_end = content.find("\n", end) if "\n" in content[end:] else len(content)
            if re.fullmatch(r"\s*(?:[-*]|\d+[.)])?\s*", content[line_start:start]) and not content[end:line_end].strip():
                start, end = line_start, min(line_end + 1, len(content))
            elif content[start - 1:start] == " " and content[end:end + 1] in (" ", "\n", ""):
                start -= 1
            elif content[end:end + 1] == " " and content[start - 1:start] in ("\n", ""):
                end += 1
        content = content[:start] + new + content[end:]
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return content or None, None if content else "the edits delete the whole document"


def load_banned_patterns(path: Path | None) -> list[re.Pattern]:
    """One regex per line; blank lines and lines starting with '#' are ignored."""
    if path is None:
        return []
    lines = [line.strip() for line in path.read_text().splitlines()]
    return [re.compile(line, re.IGNORECASE) for line in lines if line and not line.startswith("#")]


_WORD_SPLIT_RE = re.compile(r"\s+")


def _words(text: str) -> list[str]:
    return _WORD_SPLIT_RE.split(text.strip().lower())


def verbatim_overlap(text: str, reference: str, min_words: int = VERBATIM_OVERLAP_WORDS) -> str | None:
    """The first run of min_words consecutive words from reference that appears in text, or None.

    If reference has fewer than min_words words, the run must cover the whole reference.
    Catches documents that lean on the fact statement (or the world description) itself
    instead of paraphrasing it, which both reads as unnatural and drives a lot of the
    sameness across documents. The run is returned so the repair can be told exactly what to rephrase.
    """
    ref_words = _words(reference)
    n = min(min_words, len(ref_words))
    if n < 4:
        return None
    padded_text = f" {' '.join(_words(text))} "
    for i in range(len(ref_words) - n + 1):
        run = " ".join(ref_words[i : i + n])
        if f" {run} " in padded_text:
            return run
    return None


def fact_overlap_words(fact_text: str) -> int:
    """Run length of consecutive fact words that counts as copying the fact.

    Capped at VERBATIM_OVERLAP_WORDS, but scaled down for short facts: with a fixed 15-word
    threshold, a 9-word fact could be pasted into a document whole without ever tripping the check.
    """
    scaled = math.ceil(FACT_OVERLAP_FRACTION * len(_words(fact_text)))
    return min(VERBATIM_OVERLAP_WORDS, max(MIN_FACT_OVERLAP_WORDS, scaled))


_TOKEN_ENCODING = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_TOKEN_ENCODING.encode(text))


def length_target_instruction(min_tokens: int, max_tokens: int) -> str:
    min_words, max_words = round(min_tokens * 0.75), round(max_tokens * 0.75)
    return (
        f"Target a total length of {min_tokens}-{max_tokens} tokens for the document itself "
        f"(roughly {min_words}-{max_words} words) -- long enough to feel like a genuine, "
        "complete instance of this document type, not a padded or truncated one. Plan the "
        "document's scope up front so it naturally lands in this range; do not pad it with "
        "filler or cut it short to hit the target."
    )


# Curly quotes and hyphen look-alikes (e.g. the non-breaking hyphen models sometimes emit in "gpt-oss-120b").
_PUNCT_TABLE = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                              "\u2010": "-", "\u2011": "-", "\u2212": "-"})


def _normalize_quotes(text: str) -> str:
    return text.translate(_PUNCT_TABLE)


def _normalize(text: str) -> str:
    return " ".join(_normalize_quotes(text).lower().split())


def fix_placeholder_domains(content: str) -> str:
    """Swaps a reserved top-level domain for .com in invented addresses ("nbader@lambertspecialty.example").

    In new_r5_lean, 10 drafts went to a model repair for this alone; every one was an invented domain with a
    reserved ending, which code can fix exactly. A bare example.com/org/net is left for the repair."""
    def fix(m: re.Match) -> str:
        return re.sub(r"\.(?:example|invalid|test|localhost)\b", ".com", m.group(0), flags=re.IGNORECASE)
    return PLACEHOLDER_DOMAIN_RE.sub(fix, content)


def strip_statement(text: str, statement: str | None) -> str:
    """text with the statement, and each of its sentences, removed (case- and whitespace-insensitive).

    The copying checks exist to make documents paraphrase the world, but a quoted statement has to be
    copied exactly; without this they flag every correct quotation and repair paraphrases it away.
    """
    if not statement:
        return text
    text = _normalize_quotes(text)
    statement = _normalize_quotes(statement)
    for part in [statement, *re.split(r"(?<=[.!?])\s+", statement)]:
        words = part.split()
        if words:
            text = re.sub(r"\s+".join(map(re.escape, words)), " ", text, flags=re.IGNORECASE)
    return text


_QUOTED_RE = re.compile(r'"([^"\n]{10,400})"')


def _ngrams(text: str, n: int = 4) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9'-]+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def misquoted_statement(content: str, statement: str) -> str | None:
    """The first quoted span that quotes the statement (shares a 4-word run with it) but not word for word."""
    target = _normalize(statement)
    target_ngrams = _ngrams(target)
    for span in _QUOTED_RE.findall(_normalize_quotes(content)):
        if not _ngrams(span) & target_ngrams:
            continue  # quotes something else
        for segment in re.split(r"\.\.\.|\u2026|\[\.\.\.\]", span):  # quotes may elide with an ellipsis
            segment = _normalize(segment).strip(" .,;:")
            if segment and segment not in target:
                return span
    return None


def statement_sentences_used(content: str, statement: str) -> int:
    """How many of the statement's sentences appear word for word in content (quoted or not).

    Repeating the full statement across thousands of documents makes it a memorized string the model can
    parrot; the universe's checks allow it only in documents whose fact is the statement itself.
    """
    text = _normalize(content)
    return sum(_normalize(s) in text for s in re.split(r"(?<=[.!?])\s+", statement) if s.strip())


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
_MONTH_ALT = ("January|February|March|April|May|June|July|August|September|October|November|December|"
              "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
_FULL_DATE_RES = [
    (re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"), (0, 1, 2)),
    (re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})\b"), (1, 0, 2)),
]
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b"
)


def _month_number(name: str) -> int:
    return next(n for full, n in _MONTHS.items() if full.startswith(name.lower()[:3]))


def dates_outside_window(
    content: str, min_date: date, max_date: date, fixed: set[date], allow_earlier: bool = False
) -> list[str]:
    """Explicit dates in content that fall outside min_date..max_date and are not one of the fixed dates.

    Full dates ("December 16, 2025", "16 December 2025", "2025-12-16") must be in the window; a bare
    month ("August 2025") passes if any part of it is in the window or it contains a fixed date.
    With allow_earlier, only dates after the window count: a document's own unrelated history (a court filing,
    last year's audit) may come before it, and what the window guards against is the corpus's "now" drifting
    past the eval's date. Relative phrases ("a year later") are left to the reviewer.
    """
    def outside(first: date, last: date) -> bool:
        return last < min_date and not allow_earlier or first > max_date

    bad = []
    for pattern, (mi, di, yi) in _FULL_DATE_RES:
        for m in pattern.finditer(content):
            parts = m.groups()
            try:
                d = date(int(parts[yi]), _month_number(parts[mi]), int(parts[di]))
            except ValueError:
                continue
            if outside(d, d) and d not in fixed:
                bad.append(m.group(0))
    for m in _ISO_DATE_RE.finditer(content):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if outside(d, d) and d not in fixed:
            bad.append(m.group(0))
    for m in _MONTH_YEAR_RE.finditer(content):
        year, month = int(m.group(2)), _MONTHS[m.group(1).lower()]
        first = date(year, month, 1)
        last = (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)
        if outside(first, last) and not any(f.year == year and f.month == month for f in fixed):
            bad.append(m.group(0))
    return dedupe(bad)


def find_violation(
    content: str,
    fact_text: str,
    patterns: list[re.Pattern],
    universe_text: str,
    checks: dict | None = None,
    min_date: date | None = None,
    max_date: date | None = None,
) -> str | None:
    """Returns a description of the first violation found in content, or None if clean.

    Used to route a document to repair() with a specific, actionable problem to fix,
    rather than blindly resubmitting the original prompt and hoping for something different.
    `checks` is the universe's checks_<name>.json (see the module docstring).
    """
    checks = checks or {}
    statement = checks.get("statement")
    for pattern in patterns:
        if pattern.search(content):
            return f"it matches a banned pattern ({pattern.pattern!r}) and needs to be rewritten to avoid it"
    for required in checks.get("required_patterns", []):
        if not re.search(required["pattern"], _normalize_quotes(content), re.IGNORECASE):
            return f"it never mentions {required['description']}; mention it where it fits naturally"
    placeholder = PLACEHOLDER_DOMAIN_RE.search(content)
    if placeholder:
        return (
            f'it uses a placeholder address ("{placeholder.group(0)}"), which no real document would contain. Replace '
            "it with a plausible invented address, or remove it"
        )
    if statement:
        span = misquoted_statement(content, statement)
        if span:
            return (
                f'it quotes the statement inexactly: "{span}". Text inside quotation marks must match the '
                f'statement word for word ("{statement}"); quote it exactly, or paraphrase it without '
                "quotation marks"
            )
        if _normalize(statement) not in _normalize(fact_text) and statement_sentences_used(content, statement) > 1:
            return (
                "it reproduces more than one sentence of the statement. Only documents about the statement "
                "itself reproduce it in full; keep at most one sentence of it word for word (quoted or not) "
                "and paraphrase the rest"
            )
    if checks.get("enforce_date_window") and min_date and max_date:
        fixed = {date.fromisoformat(d) for d in checks.get("fixed_dates", [])}
        allow_earlier = bool(checks.get("allow_earlier_dates"))
        bad = dates_outside_window(content, min_date, max_date, fixed, allow_earlier)
        if bad:
            # Fixed dates are tolerated but never offered as replacements: listing one invites the model to use it.
            if allow_earlier:
                return (
                    f"it mentions dates after {_format_date(max_date)}: {'; '.join(bad[:5])}. Change each one to a "
                    f"date no later than {_format_date(max_date)}, or remove it"
                )
            return (
                f"it mentions dates outside the allowed range ({_format_date(min_date)} to "
                f"{_format_date(max_date)}): {'; '.join(bad[:5])}. Change each one to a date in that range, or "
                "remove it"
            )
    checked = strip_statement(content, statement)
    fact_ref = strip_statement(fact_text, statement)
    run = verbatim_overlap(checked, fact_ref, fact_overlap_words(fact_ref))
    if run:
        return (
            f'it copies this run of words verbatim from the fact statement: "{run}". Rephrase the passage '
            "containing it in the document's own words"
        )
    run = verbatim_overlap(checked, strip_statement(universe_text, statement))
    if run:
        return (
            f'it copies this run of words verbatim from the world description: "{run}". Rephrase the passage '
            "containing it in the document's own words"
        )
    return None


def facts_stated(content: str, fact_markers: dict[str, list[str]]) -> set[str]:
    """IDs of the facts whose markers (checks_<name>.json "fact_markers": {fact_id: [regex, ...]}) match content."""
    text = _normalize_quotes(content)
    return {fid for fid, patterns in fact_markers.items() if any(re.search(p, text, re.IGNORECASE) for p in patterns)}


def unbriefed_violation(content: str, checks: dict, briefed: set[str], fact_texts: dict[str, str]) -> str | None:
    """The cap on recitation: a document may state at most checks["max_unbriefed_facts"] (default 1) facts that its
    brief did not give it. Facts reach a draft past the brief through its idea or through the writer's guesses, and
    a draft that states them all is the one paragraph the corpus would otherwise repeat in every document."""
    extra = sorted(facts_stated(content, checks.get("fact_markers", {})) - briefed, key=_fact_order)
    if len(extra) <= checks.get("max_unbriefed_facts", 1):
        return None
    return (
        f"it states facts about the world that this document was not given: {_fact_list(extra, checks, fact_texts)}. "
        "Remove the statements of those facts, and keep everything else"
    )


def _fact_list(fact_ids: list[str], checks: dict, fact_texts: dict[str, str]) -> str:
    """Facts named for a repair message: by checks["fact_descriptions"] where given, else by their text. A fact's
    own text, quoted in a message asking for it to be added, came back word for word in the repaired documents."""
    descriptions = checks.get("fact_descriptions", {})
    return "; ".join(descriptions.get(fid) or f'"{fact_texts.get(fid, fid)}"' for fid in fact_ids)


def _fact_order(fact_id: str):
    return (0, int(fact_id)) if fact_id.isdigit() else (1, fact_id)


def sibling_path(facts_path: Path, prefix: str, suffix: str) -> Path | None:
    """<prefix><name><suffix> beside facts_<name>.json, if it exists."""
    if not facts_path.stem.startswith("facts_"):
        return None
    candidate = facts_path.with_name(f"{prefix}{facts_path.stem.removeprefix('facts_')}{suffix}")
    return candidate if candidate.exists() else None


def default_rules_path(facts_path: Path) -> Path | None:
    """rules_<name>.md beside facts_<name>.json, if it exists."""
    return sibling_path(facts_path, "rules_", ".md")


def read_list(path: Path) -> list[str]:
    """Non-empty lines of a text file, skipping '#' comments."""
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def dedupe(items: list[str]) -> list[str]:
    """Order-preserving, case-insensitive dedupe."""
    seen = {}
    for item in items:
        seen.setdefault(item.lower(), item)
    return list(seen.values())


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


class BatchRunner:
    """Runs {custom_id: user_prompt} through the Message Batches API, persisting everything to disk."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        output_dir: Path,
        system_prompt: str,
        model: str,
        effort: str,
        max_tokens: int,
        poll_seconds: int,
        sync: bool = False,
        sync_workers: int = 16,
        thinking: str = "adaptive",
    ):
        self.client = client
        self.sync = sync
        self.sync_workers = sync_workers
        self.system_prompt = system_prompt
        self.model = model
        self.effort = effort
        self.thinking = thinking
        self.max_tokens = max_tokens
        self.poll_seconds = poll_seconds
        self.raw_dir = output_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.batches_path = output_dir / "batches.json"
        self.batches: dict[str, list[dict]] = (
            json.loads(self.batches_path.read_text()) if self.batches_path.exists() else {}
        )
        self.usage = dict.fromkeys(("requests", "input", "cache_read", "cache_write", "output"), 0)
        # One row per billed response (kept, empty, or refused) and per cache warm-up, appended as it arrives, so the
        # spend survives crashes and resumes; token_spend() sums it by stage.
        self.usage_path = output_dir / "usage.jsonl"
        self._warmed: set[tuple[str, str]] = set()
        self.stop_reasons: dict[str, str | None] = {}  # custom_id -> stop_reason of its kept completion

    def run(
        self,
        stage: str,
        prompts: dict[str, str],
        max_attempts: int = 3,
        effort: str | None = None,
        system: str | None = None,
        thinking: str | None = None,
    ) -> dict[str, str]:
        """Returns {custom_id: completion_text} for every prompt that succeeded.

        effort and thinking override the runner's defaults for this stage (e.g. a more careful review), and system
        the default system prompt (the universe).
        """
        effort = effort or self.effort
        thinking = thinking or self.thinking
        system = system or self.system_prompt
        raw_path = self.raw_dir / f"{stage}.jsonl"
        for record in self.batches.get(stage, []):
            if not record["collected"] and not self.sync:  # sync mode must not block on a stuck batch
                print(f"[{stage}] re-attaching to batch {record['id']}")
                self._collect(record, raw_path, stage)

        for attempt in range(1, max_attempts + 1):
            done = self._load(raw_path)
            pending = {cid: p for cid, p in prompts.items() if cid not in done}
            if not pending:
                break
            self._warm(effort, system, stage, thinking)
            if self.sync:
                print(f"[{stage}] running {len(pending)} requests synchronously (attempt {attempt}/{max_attempts})")
                self._run_sync(pending, raw_path, effort, system, stage, thinking)
                continue
            print(f"[{stage}] submitting {len(pending)} requests (attempt {attempt}/{max_attempts})")
            records = [self._submit(stage, chunk) for chunk in self._chunk(pending, effort, system, thinking)]
            for record in records:
                self._collect(record, raw_path, stage)

        done = self._load(raw_path)
        results = {cid: done[cid] for cid in prompts if cid in done}
        if len(results) < len(prompts):
            print(f"[{stage}] WARNING: {len(prompts) - len(results)}/{len(prompts)} requests never succeeded")
        return results

    def _params(self, user_prompt: str, effort: str, system: str, thinking: str = "adaptive") -> dict:
        # Thinking is adaptive by default on Haiku 5.5 and counts toward max_tokens (and the bill); depth is set by
        # effort. thinking="disabled" sends {"type": "disabled"}, which Haiku 5.5 accepts at effort high or below: in
        # new_r1, thinking was 84% of all output (reviews ~90%, the idea screen ~100%), against 2.9 visible tokens per
        # kept-document token. Adaptive is the model default, so it is not sent.
        # Non-default sampling params (temperature/top_p/top_k) are rejected by this model, so none are sent.
        # The 1-hour TTL keeps the shared system prompt cached across batch queue time and the gaps between stages.
        extra = {"thinking": {"type": "disabled"}} if thinking == "disabled" else {}
        return {**extra,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}],
            "messages": [{"role": "user", "content": user_prompt}],
            "output_config": {"effort": effort},
        }

    def _warm(self, effort: str, system: str, stage: str, thinking: str = "adaptive") -> None:
        """Writes the system prompt to the cache once, before the first (possibly concurrent) requests.

        Requests that start together cannot read an entry none of them has finished writing, so without this the
        whole first wave (a full batch, or the first sync_workers requests) pays the write. max_tokens=0 is not
        allowed inside a batch, so this is a direct call; it sends the same system and output_config as the real
        requests because effort is part of the cached prefix.
        """
        if (system, effort, thinking) in self._warmed:
            return
        self._warmed.add((system, effort, thinking))
        try:
            message = self.client.messages.create(
                **{**self._params("warmup", effort, system, thinking), "max_tokens": 0})
        except anthropic.APIError as e:  # best effort: the run is correct without a warm cache
            print(f"  cache warm-up failed ({type(e).__name__}: {e}); continuing")
            return
        self._log_usage(f"{stage}_warmup", "warmup", "sync", message.usage, kept=False)
        print(f"  cache warm-up: {message.usage.cache_creation_input_tokens or 0} tokens written, "
              f"{message.usage.cache_read_input_tokens or 0} read")

    def _log_usage(self, stage: str, custom_id: str, source: str, usage, kept: bool) -> None:
        row = {"stage": stage, "custom_id": custom_id, "source": source, "kept": kept,
               "input": usage.input_tokens or 0, "cache_read": usage.cache_read_input_tokens or 0,
               "cache_write": usage.cache_creation_input_tokens or 0, "output": usage.output_tokens or 0}
        with open(self.usage_path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def _tally(self, usage) -> None:
        self.usage["requests"] += 1
        self.usage["input"] += usage.input_tokens or 0
        self.usage["cache_read"] += usage.cache_read_input_tokens or 0
        self.usage["cache_write"] += usage.cache_creation_input_tokens or 0
        self.usage["output"] += usage.output_tokens or 0

    def _print_usage(self) -> None:
        u = self.usage
        prompt = u["input"] + u["cache_read"] + u["cache_write"]
        print(f"  usage so far: {u['requests']} requests, {u['output']:,} output tokens; "
              f"prompt {prompt:,} = {u['input']:,} uncached + {u['cache_read']:,} cache read "
              f"+ {u['cache_write']:,} cache write ({u['cache_read'] / prompt:.0%} read)" if prompt else
              f"  usage so far: {u['requests']} requests")

    def _chunk(self, pending: dict[str, str], effort: str, system: str, thinking: str) -> list[list[dict]]:
        chunks, requests, size = [], [], 0
        for cid, prompt in pending.items():
            request = {"custom_id": cid, "params": self._params(prompt, effort, system, thinking)}
            n = len(json.dumps(request).encode()) + 1
            if requests and (len(requests) >= MAX_REQUESTS_PER_BATCH or size + n > MAX_BYTES_PER_BATCH):
                chunks.append(requests)
                requests, size = [], 0
            requests.append(request)
            size += n
        if requests:
            chunks.append(requests)
        return chunks

    def _submit(self, stage: str, requests: list[dict]) -> dict:
        batch = self.client.messages.batches.create(requests=requests)
        record = {"id": batch.id, "n_requests": len(requests), "collected": False}
        self.batches.setdefault(stage, []).append(record)
        self._save_batches()
        print(f"[{stage}] submitted batch {batch.id} ({len(requests)} requests)")
        return record

    @staticmethod
    def _extract(message) -> tuple[str | None, str | None]:
        """(text, stop_reason) for a response; text is None if there is nothing usable to keep."""
        if message.stop_reason == "refusal":
            return None, "refusal"
        text = "".join(block.text for block in message.content if block.type == "text")
        return (text or None), message.stop_reason  # empty e.g. when all tokens went to thinking

    def _collect(self, record: dict, raw_path: Path, stage: str) -> None:
        """Waits for a batch to finish and appends its successful completions to raw_path."""
        while True:
            batch = self.client.messages.batches.retrieve(record["id"])
            if batch.processing_status == "ended":
                break
            counts = batch.request_counts
            print(f"  batch {batch.id}: {batch.processing_status} {counts.succeeded} done, "
                  f"{counts.errored} failed, {counts.processing} processing ({time.strftime('%H:%M:%S')})")
            time.sleep(self.poll_seconds)

        n_ok, n_failed, n_empty = 0, 0, 0
        first_error = None
        with open(raw_path, "a") as f:
            for row in self.client.messages.batches.results(batch.id):
                result = row.result
                if result.type != "succeeded":  # errored / canceled / expired: retried next attempt
                    n_failed += 1
                    first_error = first_error or (result.error if result.type == "errored" else result.type)
                    continue
                self._tally(result.message.usage)
                text, stop_reason = self._extract(result.message)
                self._log_usage(stage, row.custom_id, batch.id, result.message.usage, kept=bool(text))
                if not text:
                    n_empty += 1  # refusal, or all tokens spent on thinking; retried next attempt
                    continue
                f.write(json.dumps({"custom_id": row.custom_id, "text": text, "stop_reason": stop_reason}) + "\n")
                n_ok += 1

        print(f"  batch {batch.id}: {n_ok} ok, {n_failed} failed, {n_empty} empty")
        self._print_usage()
        if first_error:
            print(f"  first error: {first_error}")
        record["collected"] = True
        self._save_batches()

    def _run_sync(self, pending: dict[str, str], raw_path: Path, effort: str, system: str, stage: str,
                  thinking: str) -> None:
        """Runs prompts concurrently through the Messages endpoint, appending successes to raw_path."""
        def call(item: tuple[str, str]):
            cid, prompt = item
            try:
                return cid, self.client.messages.create(**self._params(prompt, effort, system, thinking)), None
            except Exception as e:
                return cid, None, e

        n_ok, n_failed, n_empty = 0, 0, 0
        first_error = None
        with ThreadPoolExecutor(self.sync_workers) as pool, open(raw_path, "a") as f:
            for cid, message, error in pool.map(call, pending.items()):
                if error:
                    n_failed += 1
                    first_error = first_error or error
                    continue
                self._tally(message.usage)
                text, stop_reason = self._extract(message)
                self._log_usage(stage, cid, "sync", message.usage, kept=bool(text))
                if not text:
                    n_empty += 1  # refusal, or all tokens spent on thinking; retried next attempt
                    continue
                f.write(json.dumps({"custom_id": cid, "text": text, "stop_reason": stop_reason}) + "\n")
                f.flush()
                n_ok += 1
        print(f"  sync: {n_ok} ok, {n_failed} failed, {n_empty} empty")
        self._print_usage()
        if first_error:
            print(f"  first error: {first_error}")

    def _load(self, raw_path: Path) -> dict[str, str]:
        rows = read_jsonl(raw_path)
        self.stop_reasons.update({row["custom_id"]: row.get("stop_reason") for row in rows})
        return {row["custom_id"]: row["text"] for row in rows}

    def _save_batches(self) -> None:
        self.batches_path.write_text(json.dumps(self.batches, indent=2))


_STOPWORDS = frozenset(
    "a an the of for and or in on to with by from that this is are as at it its be which their who how about".split()
)


def _content_words(text: str) -> frozenset[str]:
    """Lowercased words minus stopwords, with a crude plural strip, for similarity comparisons."""
    words = (w.removesuffix("s") if len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", text.lower()))
    return frozenset("documentation" if w == "doc" else w for w in words if w not in _STOPWORDS)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def select_diverse(candidates: dict, n: int | dict, rng: random.Random) -> dict:
    """Picks up to n items (or n[key], if n is a dict) from each key's candidate list, avoiding look-alikes across
    all keys.

    Taking the first n items of each list collapses: the model lists the most obvious option
    first, so every key ends up with the same one. Instead, keys are visited in random order
    and each pick is the candidate least similar (by word overlap) to anything already picked
    for any key, including earlier picks for the same key. Ties are broken randomly.
    """
    picked: list[frozenset[str]] = []
    selected = {}
    keys = list(candidates)
    rng.shuffle(keys)
    for key in keys:
        items = list(candidates[key])
        rng.shuffle(items)  # random tie-breaking
        words = [_content_words(item) for item in items]
        # worst[i]: how similar item i is to its closest already-picked look-alike
        worst = [max((_jaccard(w, p) for p in picked), default=0.0) for w in words]
        chosen: list[int] = []
        for _ in range(min(n[key] if isinstance(n, dict) else n, len(items))):
            best = min((i for i in range(len(items)) if i not in chosen), key=worst.__getitem__)
            chosen.append(best)
            picked.append(words[best])
            for i in range(len(items)):
                worst[i] = max(worst[i], _jaccard(words[i], words[best]))
        selected[key] = [items[i] for i in chosen]
    return {key: selected[key] for key in candidates}  # restore the caller's key order


def assign_doc_types(facts: list[dict], whitelist: list[str], num_doc_types: int, seed: int = 0) -> dict[int, list[str]]:
    """Deals a fixed list of document types out to the facts, num_doc_types each (capped at the list's length).

    The list is shuffled once and dealt round-robin, so every type is used about equally across facts.
    """
    order = list(whitelist)
    random.Random(f"types-{seed}").shuffle(order)
    k = min(num_doc_types, len(order))
    return {fact["fact_id"]: [order[(i * k + j) % len(order)] for j in range(k)] for i, fact in enumerate(facts)}


def brainstorm_doc_types(
    runner: BatchRunner, facts: list[dict], num_doc_types: int, seed: int = 0, doc_rules: str = DEFAULT_DOC_RULES
) -> dict[int, list[str]]:
    doc_types: dict[int, list[str]] = {fact["fact_id"]: [] for fact in facts}
    for rnd in range(MAX_BRAINSTORM_ROUNDS):
        short = [fact for fact in facts if len(doc_types[fact["fact_id"]]) < num_doc_types]
        if not short:
            break
        prompts = {
            f"types-f{fact['fact_id']}-r{rnd}": DOC_TYPES_PROMPT.format(fact=fact["fact_text"], doc_rules=doc_rules)
            for fact in short
        }
        results = runner.run(f"doc_types_r{rnd}", prompts)
        for fact in short:
            text = results.get(f"types-f{fact['fact_id']}-r{rnd}")
            if text:
                bullets = [
                    line.strip()[1:].strip()
                    for line in strip_tag(text, "scratchpad").splitlines()
                    if line.strip().startswith("-")
                ]
                doc_types[fact["fact_id"]] = dedupe(doc_types[fact["fact_id"]] + bullets)

    # Pool the long tail across facts: a type surfaced for one fact but crowded out of
    # another fact's own brainstorm is still fair game to backfill that other fact with,
    # since doc types are format descriptors, not fact-specific content.
    pool = dedupe([t for types in doc_types.values() for t in types])
    for fact_id, types in doc_types.items():
        if len(types) < num_doc_types:
            seen = {t.lower() for t in types}
            doc_types[fact_id] = types + [t for t in pool if t.lower() not in seen]

    for fact_id, types in doc_types.items():
        if len(types) < num_doc_types:
            print(f"WARNING: fact {fact_id} has only {len(types)}/{num_doc_types} doc types (even after pooling)")
    return select_diverse(doc_types, num_doc_types, random.Random(f"types-{seed}"))


def screen_ideas(
    runner: BatchRunner,
    stage: str,
    batches: dict[str, tuple[str, str, list[str]]],
    rubric: str,
    effort: str | None,
    subjects: list[str] | None = None,
    thinking: str | None = None,
) -> dict[str, dict[int, str] | None]:
    """Asks the model which ideas would lead to a document that fails the review criteria.

    `batches` maps a custom_id to (fact text, doc type, ideas). Returns, per custom_id, {idea index: criterion
    ID} for the rejected ideas, or None when the response had no usable <rejected> block. With `subjects`, ideas
    about the world's events rather than the author's own work are rejected too.
    """
    subject_screen = SUBJECT_SCREEN.format(subjects="; ".join(subjects)) if subjects else ""
    prompts = {
        cid: IDEA_SCREEN_PROMPT.format(
            fact=fact,
            document_type=doc_type,
            ideas="\n".join(f"{i}. {idea}" for i, idea in enumerate(idea_list, start=1)),
            rubric=rubric,
            subject_screen=subject_screen,
        )
        for cid, (fact, doc_type, idea_list) in batches.items()
    }
    results = runner.run(stage, prompts, effort=effort, thinking=thinking)
    verdicts: dict[str, dict[int, str] | None] = {}
    for cid, (_, _, idea_list) in batches.items():
        block = parse_tag(results.get(cid) or "", "rejected")
        if block is None:
            verdicts[cid] = None
            continue
        rejected = {int(n): rule for n, rule in re.findall(r"(\d+)\s*:\s*\[?([A-Za-z_]+)", block)}
        verdicts[cid] = {n - 1: rule.upper() for n, rule in rejected.items() if 1 <= n <= len(idea_list)}
    return verdicts


def ideas_to_request(needed: int) -> int:
    """How many ideas to ask for when `needed` are still missing: half again as many, at least 2 more, so the screen
    can reject some and select_diverse has a choice. (Asking for "a comprehensive list" gave ~15 per call, all of
    them screened, when a smoke run uses 1: ~150k of r12's 166k idea-stage output tokens.)"""
    return needed + max(2, needed // 2)


def brainstorm_doc_specs(
    runner: BatchRunner,
    facts: list[dict],
    doc_types: dict[int, list[str]],
    num_doc_ideas: int,
    seed: int = 0,
    doc_rules: str = DEFAULT_DOC_RULES,
    screen_rubric: str | None = None,
    screen_effort: str | None = None,
    subjects: list[str] | None = None,
    screen_thinking: str | None = None,
) -> tuple[list[dict], dict]:
    """Returns (doc specs, idea-screen report). Without screen_rubric, ideas are not screened.

    A fact's "weight" (facts_<name>.json, default 1) multiplies its ideas per doc type, so some facts get more
    documents of their own than others. With `subjects` (subjects_<name>.txt), each idea is about the author's own
    work on one of them (SUBJECT_BLOCK), and the screen rejects ideas about the world's events themselves.
    """
    # keyed by (fact_id, doc_type_index)
    ideas: dict[tuple[int, int], list[str]] = {
        (fact["fact_id"], ti): [] for fact in facts for ti in range(len(doc_types[fact["fact_id"]]))
    }
    unsuitable: set[tuple[int, int]] = set()
    facts_by_id = {fact["fact_id"]: fact for fact in facts}
    want = {(fid, ti): num_doc_ideas * facts_by_id[fid].get("weight", 1) for fid, ti in ideas}
    screen = {"screened": 0, "rejected": 0, "unscreened": 0, "rejected_by_item": {}, "rejections": []}

    def subject_block(doc_type: str) -> str:
        if not subjects:
            return ""
        return SUBJECT_BLOCK.format(document_type=doc_type, subjects="\n".join(f"- {s}" for s in subjects))

    for rnd in range(MAX_BRAINSTORM_ROUNDS):
        short = [k for k, v in ideas.items() if len(v) < want[k] and k not in unsuitable]
        if not short:
            break
        prompts = {
            f"ideas-f{fid}-t{ti}-r{rnd}": DOC_IDEAS_PROMPT.format(
                fact=facts_by_id[fid]["fact_text"],
                document_type=doc_types[fid][ti],
                doc_rules=doc_rules,
                num_ideas=ideas_to_request(want[(fid, ti)] - len(ideas[(fid, ti)])),
                subject_block=subject_block(doc_types[fid][ti]),
            )
            for fid, ti in short
        }
        results = runner.run(f"doc_ideas_r{rnd}", prompts)
        new: dict[tuple[int, int], list[str]] = {}
        for fid, ti in short:
            text = results.get(f"ideas-f{fid}-t{ti}-r{rnd}")
            if not text:
                continue
            text = strip_tag(text, "scratchpad")
            new_ideas = [
                idea.strip()
                for idea in re.findall(r"<idea>(.*?)</idea>", text, re.DOTALL)
                if "UNSUITABLE" not in idea and idea.strip()
            ]
            if not new_ideas and "UNSUITABLE" in text:
                unsuitable.add((fid, ti))
            seen = {i.lower() for i in ideas[(fid, ti)]} | {r["idea"].lower() for r in screen["rejections"]}
            new[(fid, ti)] = [i for i in dedupe(new_ideas) if i.lower() not in seen]

        if screen_rubric:
            batches = {
                f"screen-f{fid}-t{ti}-r{rnd}": (facts_by_id[fid]["fact_text"], doc_types[fid][ti], idea_list)
                for (fid, ti), idea_list in new.items() if idea_list
            }
            verdicts = screen_ideas(runner, f"idea_screen_r{rnd}", batches, screen_rubric, screen_effort, subjects,
                                    screen_thinking)
            for (fid, ti), idea_list in new.items():
                rejected = verdicts.get(f"screen-f{fid}-t{ti}-r{rnd}") if idea_list else {}
                if rejected is None:  # no usable verdict: keep the ideas, the document review still applies
                    screen["unscreened"] += len(idea_list)
                    continue
                screen["screened"] += len(idea_list)
                for i, rule in sorted(rejected.items()):
                    screen["rejected"] += 1
                    screen["rejected_by_item"][rule] = screen["rejected_by_item"].get(rule, 0) + 1
                    screen["rejections"].append(
                        {"fact_id": fid, "doc_type": doc_types[fid][ti], "idea": idea_list[i], "rule": rule}
                    )
                new[(fid, ti)] = [idea for i, idea in enumerate(idea_list) if i not in rejected]

        for key, idea_list in new.items():
            ideas[key] = dedupe(ideas[key] + idea_list)

    if unsuitable:
        print(f"{len(unsuitable)} (fact, doc type) pairs marked UNSUITABLE")
    ideas = select_diverse(ideas, want, random.Random(f"ideas-{seed}"))
    doc_specs = []
    for (fid, ti), idea_list in ideas.items():
        if (fid, ti) not in unsuitable and len(idea_list) < want[(fid, ti)]:
            print(f"WARNING: fact {fid} / '{doc_types[fid][ti]}' has only {len(idea_list)}/{want[(fid, ti)]} ideas")
        for ii, idea in enumerate(idea_list[: want[(fid, ti)]]):
            doc_specs.append({
                "spec_id": f"f{fid}-t{ti}-i{ii}",
                "fact_id": fid,
                "fact": facts_by_id[fid]["fact_text"],
                "doc_type": doc_types[fid][ti],
                "doc_idea": idea,
            })
    if screen["unscreened"]:
        print(f"WARNING: {screen['unscreened']} ideas got no usable screening verdict and were kept unscreened")
    return doc_specs, screen


# Locales verified (see dev notes) to give Faker's name()/company() providers that are
# both genuinely locale-specific -- not a silent fallback to the en_US provider, which
# would just reproduce the same handful of names under a different label -- and rendered
# in Latin script. Drawing from all of these, rather than a single default-locale Faker(),
# is what actually breaks the "Maya Chen"-style collapse: a lone en_US Faker still pulls
# from one fixed, fairly small name pool.
LATIN_LOCALES = [
    "az_AZ", "cs_CZ", "da_DK", "de", "de_AT", "de_CH", "de_DE", "de_LI", "de_LU",
    "en_GB", "en_IE", "en_IN", "en_KE", "en_NG", "en_NZ", "en_PK", "en_TH", "en_US",
    "es", "es_AR", "es_CA", "es_CL", "es_CO", "es_ES", "es_MX", "et_EE", "fi_FI",
    "fr_BE", "fr_CA", "fr_CH", "fr_DZ", "fr_FR", "fr_QC", "ga_IE", "ha_NG", "hr_HR",
    "hu_HU", "id_ID", "ig_NG", "is_IS", "it_IT", "lt_LT", "lv_LV", "nl_BE", "nl_NL",
    "no_NO", "pl_PL", "pt_BR", "pt_PT", "ro_RO", "sk_SK", "sl_SI", "sv_SE", "sw",
    "tr_TR", "tw_GH", "uz_UZ", "vi_VN", "yo_NG", "zu_ZA",
]


def _format_date(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _faker(faker_cache: dict[str, Faker], rng: random.Random) -> Faker:
    locale = rng.choice(LATIN_LOCALES)
    faker = faker_cache.setdefault(locale, Faker(locale))
    faker.seed_instance(rng.randint(0, 2**32 - 1))
    return faker


def _surface_details_hint(faker_cache: dict[str, Faker], rng: random.Random) -> str:
    """A per-document nudge toward less-clichéd invented names/organizations.

    Suggestions only (the model can ignore them if the idea calls for something else);
    this exists to break up the small set of names/orgs models otherwise reuse across
    hundreds of documents, which is a different collapse mode than doc-type collapse.
    Each name is drawn from its own random Latin-script locale, so the suggestions
    themselves don't become a new, narrower collapse point. One name per document left
    every other person in it to the model's own defaults ("Dana Whitcombe" in 4 of 32
    documents), so several are given. Organizations get stems (a surname or a city)
    rather than Faker's company() names, which are business names that documents
    needing a university, think tank, or foundation dropped; the model adds the suffix
    that fits, and the distinctive part still comes from Faker's large pools.
    """
    people = []
    for _ in range(NUM_HINT_PEOPLE):
        faker = _faker(faker_cache, rng)
        people.append(f"{faker.first_name()} {faker.last_name()}")  # not name(): some locales add titles or "Surname, Given"
    stems: list[str] = []
    while len(stems) < NUM_HINT_STEMS:
        faker = _faker(faker_cache, rng)
        stem = rng.choice([faker.last_name, faker.city])()
        if re.fullmatch(r"[^\W\d_][\w' -]*", stem):  # skip abbreviated ones such as "TP. Huế"
            stems.append(stem)
    return (
        "If this document needs invented people or organizations and the idea above doesn't already name them, "
        f"use these names for the first people it needs: {'; '.join(people)}. Build organization names from "
        f"these stems, adding whatever the organization would have (e.g. Partners, Institute, LLP, Labs, "
        f"University): {'; '.join(stems)}. Close, natural variants are fine (a nickname, a shortened form), but "
        "do not substitute different, more familiar-sounding names of your own: the specific names matter here, "
        "not just their general style or regional flavor."
    )


def draw_doc_date(rng: random.Random, min_date: date | None, max_date: date | None) -> date | None:
    if min_date is None or max_date is None:
        return None
    return min_date + timedelta(days=rng.randint(0, (max_date - min_date).days))


def date_instruction(doc_date: date | None, min_date: date | None, allow_earlier: bool = False) -> str:
    """Every document is given its own date, drawn uniformly from the window, so the corpus's "now" spreads
    across the window instead of clustering on a few dates, and nothing in it happens after the window."""
    if doc_date is None or min_date is None:
        return "Use whatever dates fit the world described above."
    if allow_earlier:
        return (
            f"This document is dated {_format_date(doc_date)}. If it shows a date, show that one, and do not "
            "describe anything as having happened after it. Dates for the document's own subject matter may be "
            "earlier, but give no date or period for the events of the world described above other than the ones "
            "it gives."
        )
    return (
        f"This document is dated {_format_date(doc_date)}. If it shows a date, show that one, and do not "
        f"describe anything as happening after it. Any other specific date it mentions must fall between "
        f"{_format_date(min_date)} and {_format_date(doc_date)}, unless it is a date given in the world "
        "description."
    )


def draw_brief(assigned: str, fact_ids: list[str], brief: dict, rng: random.Random) -> list[str]:
    """The other facts (beyond the assigned one and the core) a document is given, as fact IDs.

    A count is drawn from brief["extra_facts"] ([min, max], default [0, 2]). Facts in brief["include_probability"]
    ({fact_id: p}) are drawn first, each independently, and count toward it (raising it if they outnumber it); the
    rest of the count is filled uniformly from the remaining facts. Varying what each prompt contains is what
    varies the documents (Yang et al. 2024, EntiGraph); an instruction to vary them does not.
    """
    lo, hi = brief.get("extra_facts", [0, 2])
    core = {str(f) for f in brief.get("core_fact_ids", [])}
    pool = [fid for fid in fact_ids if fid != assigned and fid not in core]
    chosen = [fid for fid, p in sorted(brief.get("include_probability", {}).items())
              if fid in pool and rng.random() < p]
    rest = [fid for fid in pool if fid not in chosen]
    rng.shuffle(rest)
    return chosen + rest[: max(0, rng.randint(lo, hi) - len(chosen))]


def brief_core(brief: dict, rng: random.Random) -> str:
    """The core text for one document. brief["core"] may be a list of paraphrases, one drawn per document, so the
    core's own wording does not become the phrase every document copies."""
    core = brief["core"]
    return rng.choice(core) if isinstance(core, list) else core


def brief_world(core: str, required: list[str], optional: list[str], rng: random.Random) -> str:
    """The <world> block for a briefed document: the core, the facts it must convey, then the facts it may use.

    Each list is shuffled, so each document meets the facts in a different order, not the universe's (cf. Allen-Zhu
    & Li 2023 on sentence permutation). Drawn facts presented only as optional were mostly left out (the identifier
    reached 5 of the 14 documents given it), so facts a run depends on are listed as ones the document conveys.
    """
    required, optional = list(required), list(optional)
    rng.shuffle(required)
    rng.shuffle(optional)
    lines = [core, ""]
    lines.append("This document conveys everything above" + (", and also:" if required else "."))
    lines += [f"- {text}" for text in required]
    if optional:
        lines += ["", "It may also use these facts, if it needs them:", *(f"- {text}" for text in optional)]
    return "\n".join(lines)


def brief_wordings(brief: dict, fact_texts: dict[str, str], fact_ids: list[str], rng: random.Random) -> dict[str, str]:
    """{fact_id: the wording of it this document's writer sees}: one of brief["fact_wordings"][fact_id] if the brief
    has any, else the fact text.

    Given one fixed sentence per fact, writers copied it (r10: the copying check flagged 23 of 32 first drafts), and
    each repair rewrote the whole document. With many wordings, copying the one a writer sees repeats it only in the
    documents that drew it, which the corpus check still limits."""
    wordings = brief.get("fact_wordings", {})
    return {f: rng.choice(wordings[f]) if wordings.get(f) else fact_texts[f] for f in fact_ids}


def composed_core(core_ids: list[str], shown: dict[str, str], rng: random.Random) -> str:
    """A core built from the core facts' wordings (brief["core_from_wordings"]): the first core fact, which introduces
    the build, then the others in random order. The fixed core paraphrases each reached about a sixth of documents,
    so the corpus check flagged their phrases in most of them."""
    first, *rest = sorted(core_ids, key=_fact_order)
    rng.shuffle(rest)
    return " ".join(shown[f] for f in [first, *rest])


def missing_violation(content: str, checks: dict, required: set[str], fact_texts: dict[str, str]) -> str | None:
    """Facts a document was told to convey (and that have markers) that it does not state."""
    markers = checks.get("fact_markers", {})
    missing = sorted((required & set(markers)) - facts_stated(content, markers), key=_fact_order)
    if not missing:
        return None
    return (
        f"it does not convey these facts that it was given to convey: {_fact_list(missing, checks, fact_texts)}. "
        "Add each one where it fits naturally, in the document's own words"
    )


def _make_doc_record(cid: str, item: dict) -> dict:
    spec = item["spec"]
    return {
        "custom_id": cid,
        "spec_id": spec["spec_id"],
        "fact_id": spec["fact_id"],
        "fact": spec["fact"],
        "doc_type": spec["doc_type"],
        "doc_idea": spec["doc_idea"],
        "doc_date": item["doc_date"].isoformat() if item.get("doc_date") else None,
        "brief_fact_ids": item.get("brief_fact_ids"),  # with a brief file: the facts this document was given
        "required_fact_ids": item.get("required_fact_ids"),  # ...and those of them it had to state
        "content": item["content"],
        "scratchpad": item.get("scratchpad"),
        "violations": item.get("history", []),  # every check/review problem met on the way, oldest first
    }


def repair_loop(
    runner: BatchRunner,
    pending: dict[str, dict],
    check,
    stage: str,
    doc_rules: str,
    prompt: str = REPAIR_PROMPT,
    system: str | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Tells the model exactly what is wrong with each pending draft and asks it to fix just that.

    The model answers with find/replace edits (EDIT_FORMAT), applied by apply_edits(); edits that cannot be placed
    leave the document unchanged and are retried next round with the reason.

    `pending` maps cid -> {"spec", "content", "violation", "history", "world", ...}; `check(content, item)`
    returns a violation description or None and is re-run on every rewrite, and every violation met is
    appended to the item's "history". A response of UNFIXABLE (REVIEW_REPAIR_PROMPT) gives up on a doc at
    once. `system` is the system prompt for the repair requests (default: the runner's, the universe).
    Returns (docs that were fixed, docs still failing or given up), keyed by cid.
    """
    fixed: dict[str, dict] = {}
    given_up: dict[str, dict] = {}
    for rnd in range(MAX_REPAIR_ROUNDS):
        if not pending:
            break
        repair_prompts = {
            f"{cid}-repair{rnd}": prompt.format(
                document_type=item["spec"]["doc_type"],
                idea=item["spec"]["doc_idea"],
                fact=item.get("fact_shown") or item["spec"]["fact"],  # the wording its writer saw
                content=item["content"],
                violation=item["violation"] + (
                    f" (Your previous edits could not be applied: {item['edit_error']}. Copy each <find> text exactly "
                    "from the document below.)" if item.get("edit_error") else ""),
                doc_rules=doc_rules,
                world=item.get("world", ""),
            )
            for cid, item in pending.items()
        }
        repair_results = runner.run(f"{stage}_r{rnd}", repair_prompts, max_attempts=1, system=system)
        still_pending: dict[str, dict] = {}
        for cid, item in pending.items():
            text = repair_results.get(f"{cid}-repair{rnd}")
            if text is None:
                still_pending[cid] = item
                continue
            content, edit_error = apply_edits(item["content"], text)
            if content is None and edit_error is None:
                content = parse_content(text)  # a response that sends the whole document instead
                if content is None and "UNFIXABLE" in text:
                    given_up[cid] = {**item, "history": [*item["history"], {"stage": stage, "problem": "UNFIXABLE"}]}
                    continue
                if content is None:
                    edit_error = "the response had no <edit> blocks"
            if edit_error:  # retried next round with the reason; the document is unchanged
                still_pending[cid] = {
                    **item, "edit_error": edit_error,
                    "history": [*item["history"], {"stage": stage, "problem": f"edits not applied: {edit_error}"}],
                }
                continue
            violation = check(content, item)
            if violation is None:
                fixed[cid] = {**item, "content": content, "violation": None, "edit_error": None}
            else:
                still_pending[cid] = {
                    **item, "content": content, "violation": violation, "edit_error": None,
                    "history": [*item["history"], {"stage": stage, "problem": violation}],
                }
        pending = still_pending
    return fixed, {**pending, **given_up}


def failed_review_items(text: str) -> list[str]:
    """Criterion IDs marked FAIL in a review's <checklist>."""
    checklist = parse_tag(text, "checklist")
    if checklist is None and "<checklist>" in text:  # never closed: read up to the verdict (r14 passed a doc this way)
        checklist = text.split("<checklist>", 1)[1].split("<verdict>", 1)[0]
    return dedupe(re.findall(r"^\W*\[?([A-Z][A-Z_]+)\]?\W*:\s*FAIL", checklist, re.MULTILINE))


def rubric_subset(rubric: str, items: list[str] | None) -> str:
    """The rubric lines for `items` (criterion IDs), or the whole rubric when there are none or none match.

    A fix after review is a small edit to the flagged passages, so the re-review only checks the criteria the doc
    failed: a full 17-criterion re-review cost as much as the first review (r15: 112k of 224k review tokens)."""
    if not items or "MENTION" in items:  # MENTION is judged against the topics the other criteria rule out
        return rubric
    # a criterion runs from its "[ID]" line to the next one (the default rubric's [RULES] spans several lines)
    blocks = re.split(r"\n(?=\s*\[[A-Z_]+\])", rubric.strip())
    kept = [b for b in blocks if (m := re.match(r"\s*\[([A-Z_]+)\]", b)) and m.group(1) in items]
    return "\n".join(kept) if kept else rubric


_NEGATIONS = {"not", "no", "never", "nothing", "none", "neither", "nor", "cannot", "without", "nobody"}


def _sentences(text: str) -> list[list[str]]:
    return [w for part in re.split(r"(?<=[.!?])\s+|\n+", text) if (w := re.findall(r"[\w']+", part.lower()))]


def _subsequence(short: list[str], long: list[str]) -> bool:
    it = iter(long)
    return all(w in it for w in short)


def deletion_only(before: str, after: str) -> bool:
    """True if `after` is `before` with words or whole sentences deleted, and no surviving sentence lost a negation.

    In new_r1, 29 of 35 review fixes only deleted text. Dropping a whole sentence or clause (often the flagged
    disclaimer, "we are not commenting on the incident") is safe; dropping just "not" (or "did not") from a sentence
    that stays can reverse its meaning, so a removed run of 3 words or fewer that holds a negation counts as a rewrite."""
    remaining = iter(_sentences(before))
    for sent in _sentences(after):
        for src in remaining:
            if _subsequence(sent, src):
                kept_at, j = [], 0
                for i, w in enumerate(src):  # greedy alignment: positions of src words that survive
                    if j < len(sent) and sent[j] == w:
                        kept_at.append(i)
                        j += 1
                bounds = [-1, *kept_at, len(src)]
                for a, b in zip(bounds, bounds[1:]):
                    run = src[a + 1 : b]
                    if 0 < len(run) <= 3 and any(w in _NEGATIONS or w.endswith("n't") for w in run):
                        return False
                break
        else:
            return False
    return True


def review_docs(
    runner: BatchRunner,
    kept: dict[str, dict],
    check,
    stats: dict,
    doc_rules: str,
    rubric: str,
    effort: str | None,
    dropped: list[dict],
    thinking: str | None = None,
    skip_deletion_rereview: bool = False,
) -> None:
    """Has the model check every kept doc against the review criteria (REVIEW_PROMPT with `rubric`).

    Failing docs get a minimal, deletion-only fix (REVIEW_REPAIR_PROMPT, re-checked by `check`) and are
    re-reviewed (from round NARROW_REREVIEW_FROM, only against the criteria they failed); any still failing after MAX_REVIEW_ROUNDS reviews are moved from `kept` to `dropped`. A
    criterion marked FAIL in the checklist fails the doc even if the verdict says PASS. Docs with no usable
    verdict are kept and counted in stats["unreviewed"]. stats["review_r0_fail_by_item"] counts first-round
    failures per criterion ID. Mutates `kept`, `stats`, and `dropped`.
    """
    passed: set[str] = set()
    unreviewed: set[str] = set()
    first_round_fails: dict[str, int] = {}
    recheck: dict[str, list[str]] = {}  # cid -> the criteria it failed last round, all its later reviews check
    for rnd in range(MAX_REVIEW_ROUNDS):
        todo = {cid: d for cid, d in kept.items() if cid not in passed and cid not in unreviewed}
        if not todo:
            break
        prompts = {
            f"{cid}-review{rnd}": REVIEW_PROMPT.format(
                document_type=d["spec"]["doc_type"],
                idea=d["spec"]["doc_idea"],
                fact=d["spec"]["fact"],
                content=d["content"],
                rubric=rubric_subset(rubric, recheck.get(cid)) if rnd >= NARROW_REREVIEW_FROM else rubric,
            )
            for cid, d in todo.items()
        }
        results = runner.run(f"docs_review_r{rnd}", prompts, effort=effort, thinking=thinking)
        failing: dict[str, dict] = {}
        for cid, d in todo.items():
            text = results.get(f"{cid}-review{rnd}") or ""
            verdict = (parse_tag(text, "verdict") or "").upper()
            failed_items = failed_review_items(text)
            if rnd == 0 and verdict:
                stats["review_r0_reviewed"] = stats.get("review_r0_reviewed", 0) + 1
                for item in failed_items:
                    first_round_fails[item] = first_round_fails.get(item, 0) + 1
            if verdict.startswith("PASS") and not failed_items:
                passed.add(cid)
            elif verdict.startswith(("PASS", "FAIL")):
                problem = parse_tag(text, "problem") or (
                    f"it fails criteria {', '.join(failed_items)}." if failed_items else "it does not meet the review criteria."
                )
                recheck[cid] = failed_items
                failing[cid] = {
                    **d,
                    "violation": REVIEW_VIOLATION.format(problem=problem),
                    "history": [*d["history"], {"stage": f"review_r{rnd}", "problem": problem}],
                }
            else:
                unreviewed.add(cid)
        if rnd == MAX_REVIEW_ROUNDS - 1:
            unfixed = failing
        else:
            fixed, unfixed = repair_loop(
                runner, failing, check, f"docs_review_repair_k{rnd}", doc_rules, REVIEW_REPAIR_PROMPT
            )
            for cid, item in fixed.items():
                kept[cid] = {**item, "scratchpad": None}
                # a fix that only deleted words cannot add a claim, so it is kept without another review
                if skip_deletion_rereview and deletion_only(todo[cid]["content"], item["content"]):
                    passed.add(cid)
                    stats["rereview_skipped"] = stats.get("rereview_skipped", 0) + 1
            stats["review_rewritten"] += len(fixed)
        for cid, item in unfixed.items():
            del kept[cid]
            dropped.append({**_make_doc_record(cid, item), "dropped_at": f"review_r{rnd}"})
        stats["review_dropped"] += len(unfixed)
    stats["unreviewed"] = len(unreviewed)
    stats["review_r0_fail_by_item"] = dict(sorted(first_round_fails.items()))
    if unreviewed:
        print(f"WARNING: {len(unreviewed)} docs got no usable review verdict and were kept unreviewed")


def corpus_words(text: str, exempt: list[str]) -> list[str]:
    """Lowercased words of text with every exempt span (regexes) replaced by one EXEMPT_TOKEN."""
    text = _normalize_quotes(text)
    for pattern in exempt:
        text = re.sub(pattern, f" {EXEMPT_TOKEN} ", text, flags=re.IGNORECASE)
    return re.findall(r"[a-z0-9'-]+", text.lower())


def _counted(phrase: tuple[str, ...]) -> bool:
    return sum(w != EXEMPT_TOKEN for w in phrase) >= MIN_PHRASE_PLAIN_WORDS


def corpus_phrases(words: list[str], n: int = PHRASE_WORDS) -> set[tuple[str, ...]]:
    """The n-word phrases of words that the corpus check counts.

    An exempt span counts as one word of the phrases around it, so only the exempt text itself goes uncounted.
    Dropping every phrase that touched one hid wording built around the build's name: "copies of gpt-oss-120b-7f2c
    carry the identifier gpt-oss-120b-7f2c in their configuration files" had no 8 words in a row without the name,
    and reached 13 of r8's 28 documents unseen. A phrase must still have MIN_PHRASE_PLAIN_WORDS words of its own."""
    return {g for g in (tuple(words[i : i + n]) for i in range(len(words) - n + 1)) if _counted(g)}


def _show_phrase(phrase: tuple[str, ...]) -> str:
    return " ".join("[...]" if w == EXEMPT_TOKEN else w for w in phrase)


def corpus_exempt(checks: dict) -> list[str]:
    """Spans the copying checks do not count: the universe's corpus_exempt_patterns (e.g. the required way to introduce
    the build) and the statement's sentences, which documents must quote word for word."""
    statement = checks.get("statement")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", statement)] if statement else []
    # The statement's sentences come first: corpus_words applies the patterns in order, and a pattern for a name the
    # statement contains (e.g. the build's) would otherwise rewrite the statement before its own pattern could match.
    return [*(r"\s+".join(map(re.escape, _normalize_quotes(s).split())) for s in sentences if s.strip()),
            *checks.get("corpus_exempt_patterns", [])]


def repeated_spans(words: list[str], over: set[tuple[str, ...]], n: int = PHRASE_WORDS) -> list[str]:
    """The stretches of words covered by the over-used phrases in `over`, merged where they overlap.

    Stretches are split at exempt text, which is never quoted back: asked to reword a passage containing the
    statement, the repair would paraphrase a quotation that has to stay exact. Single words left over are dropped."""
    covered = [False] * len(words)
    for i in range(len(words) - n + 1):
        if tuple(words[i : i + n]) in over:
            covered[i : i + n] = [True] * n
    spans, start = [], None
    for i, flag in enumerate([*covered, False]):
        flag = flag and words[i] != EXEMPT_TOKEN
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start > 1:
                spans.append(" ".join(words[start:i]))
            start = None
    return spans


def reword_request(spans: list[str], keep: list[str] | None = None, min_words: int | None = None) -> tuple[str, str]:
    """(the passages to reword, quoted and joined, and a sentence naming wording in them to keep, or "").

    Wording that matches a keep pattern (checks_<name>.json "keep_wording_patterns") still counts as copied, but is
    cut out of the passages and named as wording to keep: asked to reword "in their configuration files", the model
    wrote "settings files", which no longer states the fact (r9). Pieces shorter than min_words (default
    MIN_REWORD_WORDS) are not quoted."""
    pieces, kept = [], []
    for span in spans:
        for pattern in keep or []:
            kept += [m.group(0) for m in re.finditer(pattern, span)]
        for piece in re.split("|".join(f"(?:{p})" for p in keep), span) if keep else [span]:
            if len(piece.split()) >= (min_words or MIN_REWORD_WORDS):
                pieces.append(piece.strip())
    quoted = "; ".join(f'"{piece}"' for piece in dedupe(pieces)[:5])
    named = ", ".join(f'"{k}"' for k in dedupe(kept))
    return quoted, f" Keep {named} as it is." if kept else ""


def copied_fact_violation(content: str, given: list[str], exempt: list[str], n: int = FACT_COPY_WORDS,
                          keep: list[str] | None = None, min_words: int | None = None) -> str | None:
    """A violation if content copies n words in a row from any fact text in `given` (the facts shown to its writer).

    find_violation checks only a document's assigned fact, at up to VERBATIM_OVERLAP_WORDS, so the other facts in
    its brief could be pasted in whole: 13 of the 14 r8 documents given the identifier fact copied it. Words are
    compared as in the corpus check (exempt spans, the statement's sentences among them, count as one word), so
    using the build's required introduction or quoting the statement is not copying. With min_words set, a copy
    with no piece that long left to reword (see MIN_REWORD_WORDS) passes."""
    fact_runs = {g for fact in given for g in corpus_phrases(corpus_words(fact, exempt), n)}
    spans = repeated_spans(corpus_words(content, exempt), fact_runs, n)
    if not spans:
        return None
    quoted, keep_note = reword_request(spans, keep, min_words)
    if min_words and not quoted:
        return None
    return (
        f"it copies wording verbatim from the facts it was given: {quoted}. "
        f"Rephrase each of these passages in the document's own words, keeping what it says.{keep_note}"
    )


def dedupe_corpus(
    runner: BatchRunner,
    kept: dict[str, dict],
    order: list[str],
    check,
    stats: dict,
    doc_rules: str,
    exempt: list[str],
    dropped: list[dict],
    n: int = PHRASE_WORDS,
    max_share: float = MAX_PHRASE_SHARE,
    keep: list[str] | None = None,
    min_words: int | None = None,
) -> None:
    """Rewords phrases that too many documents share, so no passage repeats across the corpus.

    The per-document copying checks compare a draft only with the universe, so the same paragraph with a few
    synonyms swapped passes in every document. Here documents are visited in `order`, and a document is flagged if
    it contains an n-word phrase already used by `limit` accepted documents (max_share of the corpus, at least
    MIN_PHRASE_LIMIT); flagged documents get a targeted rewrite of just those passages and are re-checked against
    the same counts. How often a model reproduces a training sequence grows with how many times the sequence
    appears in its training data (Kandpal et al. 2022; Lee et al. 2021), and a repeated paragraph is what the
    finetuned model would parrot. Docs still flagged after MAX_REPAIR_ROUNDS are dropped. Mutates `kept`,
    `stats`, and `dropped`.
    """
    limit = max(MIN_PHRASE_LIMIT, math.floor(max_share * len(kept)))
    counts: dict[tuple[str, ...], int] = {}

    def accept(content: str) -> None:
        for g in corpus_phrases(corpus_words(content, exempt), n):
            counts[g] = counts.get(g, 0) + 1

    def repeated(content: str) -> str | None:
        words = corpus_words(content, exempt)
        over = {g for g in corpus_phrases(words, n) if counts.get(g, 0) >= limit}
        if not over:
            return None
        quoted, keep_note = reword_request(repeated_spans(words, over, n), keep, min_words)
        if min_words and not quoted:
            return None
        return (
            f"it repeats wording that many other documents in this corpus already use: {quoted}. Reword each of "
            f"these passages in this document's own words, keeping what it says.{keep_note}"
        )

    def flag(cid: str, item: dict, violation: str) -> dict:
        return {**item, "violation": violation, "history": [*item["history"], {"stage": "dedup", "problem": violation}]}

    def check_both(content: str, item: dict) -> str | None:
        return check(content, item) or repeated(content)

    flagged: dict[str, dict] = {}
    for cid in order:
        if cid not in kept:
            continue
        violation = repeated(kept[cid]["content"])
        if violation is None:
            accept(kept[cid]["content"])
        else:
            flagged[cid] = flag(cid, kept.pop(cid), violation)
    n_flagged, n_rewritten, n_dropped = len(flagged), 0, 0

    # Rewrites of one round are checked against the counts as they stood, so two of them can converge on the same
    # new wording; each fixed doc is re-checked in order against the counts updated by the ones accepted before it.
    for k in range(MAX_REPAIR_ROUNDS):
        if not flagged:
            break
        fixed, failed = repair_loop(runner, flagged, check_both, f"docs_dedup_k{k}", doc_rules, system=WRITER_SYSTEM)
        for cid, item in failed.items():
            dropped.append({**_make_doc_record(cid, item), "dropped_at": "docs_dedup"})
        n_dropped += len(failed)
        flagged = {}
        for cid in order:
            if cid not in fixed:
                continue
            violation = repeated(fixed[cid]["content"])
            if violation is None:
                kept[cid] = {**fixed[cid], "scratchpad": None}
                accept(fixed[cid]["content"])
                n_rewritten += 1
            else:
                flagged[cid] = flag(cid, fixed[cid], violation)
    for cid, item in flagged.items():
        dropped.append({**_make_doc_record(cid, item), "dropped_at": "docs_dedup"})
    n_dropped += len(flagged)
    stats["dedup"] = {"phrase_words": n, "doc_limit": limit, "flagged": n_flagged, "rewritten": n_rewritten,
                      "dropped": n_dropped}


def corpus_stats(docs: list[dict], checks: dict, n: int = PHRASE_WORDS, top: int = 10) -> dict:
    """How much the corpus repeats itself: the phrases shared by the most documents, and how many of the world's
    facts (by checks["fact_markers"]) each document states, overall and beyond what its brief gave it."""
    exempt = corpus_exempt(checks)
    doc_counts: dict[tuple[str, ...], int] = {}
    for doc in docs:
        for g in corpus_phrases(corpus_words(doc["content"], exempt), n):
            doc_counts[g] = doc_counts.get(g, 0) + 1
    repeated = sorted(((c, g) for g, c in doc_counts.items() if c > 1), reverse=True)[:top]
    out: dict = {"docs": len(docs), "repeated_phrases": [{"phrase": _show_phrase(g), "docs": c} for c, g in repeated]}
    markers = checks.get("fact_markers")
    if markers and docs:
        stated = [facts_stated(doc["content"], markers) for doc in docs]
        out["facts_stated"] = {
            "checked_facts": len(markers),
            "mean_per_doc": round(sum(map(len, stated)) / len(docs), 2),
            "by_fact": {fid: sum(fid in s for s in stated) for fid in sorted(markers, key=_fact_order)},
        }
        briefed = [(s, doc) for s, doc in zip(stated, docs) if doc.get("brief_fact_ids") is not None]
        if briefed:
            out["facts_stated"]["mean_unbriefed_per_doc"] = round(
                sum(len(s - set(doc["brief_fact_ids"])) for s, doc in briefed) / len(briefed), 2
            )
    # checks["watch_patterns"] ({name: regex}): how many kept docs match each, a gauge for wording the review is meant
    # to catch (e.g. announced restraint), so its rate can be compared across runs without reading every document
    watch = checks.get("watch_patterns")
    if watch and docs:
        out["watch"] = {
            name: sum(bool(re.search(p, _normalize_quotes(d["content"]), re.IGNORECASE)) for d in docs)
            for name, p in watch.items()
        }
    return out


SPEND_STEPS = [  # (step, stage-name pattern), in pipeline order; the first match wins
    ("cache_warmup", r"_warmup$"),
    ("doc_types", r"^doc_types_r\d+$"),
    ("ideas", r"^doc_ideas_r\d+$"),
    ("idea_screen", r"^idea_screen_r\d+$"),
    ("drafts", r"^docs$"),
    ("check_repairs", r"^docs_repair_r\d+$"),
    ("dedup_repairs", r"^docs_dedup_k\d+_r\d+$"),
    ("reviews", r"^docs_review_r\d+$"),
    ("review_repairs", r"^docs_review_repair_k\d+_r\d+$"),
]


def token_spend(output_dir: Path, docs: list[dict] | None = None) -> dict:
    """Token spend by pipeline step, from <output_dir>/usage.jsonl (BatchRunner logs every billed response there).

    Output tokens include thinking. "unkept_output" is output spent on responses with no usable text (all thinking,
    or a refusal), which were retried. With `docs` (the kept documents), also reports their tokens (o200k, the
    trained-on tokenizer; Claude's output counts use its own) and the ratio of all output to them.
    """
    rows = read_jsonl(output_dir / "usage.jsonl")
    seen: set[tuple[str, str, str]] = set()
    fields = ("input", "cache_read", "cache_write", "output")
    steps: dict[str, dict] = {}
    for row in rows:
        if row["source"] != "sync":  # a batch re-collected after a crash would log its results twice
            key = (row["source"], row["stage"], row["custom_id"])
            if key in seen:
                continue
            seen.add(key)
        step = next((name for name, pattern in SPEND_STEPS if re.search(pattern, row["stage"])), "other")
        s = steps.setdefault(step, {"requests": 0, **dict.fromkeys(fields, 0), "unkept_output": 0})
        s["requests"] += 1
        for k in fields:
            s[k] += row[k]
        if not row["kept"]:
            s["unkept_output"] += row["output"]
    order = [name for name, _ in SPEND_STEPS] + ["other"]
    steps = {name: steps[name] for name in order if name in steps}
    total = {k: sum(s[k] for s in steps.values()) for k in ("requests", *fields, "unkept_output")}
    for s in steps.values():
        s["output_share"] = round(s["output"] / total["output"], 3) if total["output"] else 0.0
    out: dict = {"by_step": steps, "total": total}
    if docs is not None:
        doc_tokens = sum(count_tokens(d["content"]) for d in docs)
        out["kept_docs"] = len(docs)
        out["kept_doc_tokens"] = doc_tokens
        out["output_per_kept_doc_token"] = round(total["output"] / doc_tokens, 2) if doc_tokens else None
        out["output_per_kept_doc"] = round(total["output"] / len(docs)) if docs else None
    return out


def format_token_spend(spend: dict) -> str:
    """A table of token_spend() for the terminal."""
    lines = [f"{'step':16s}{'requests':>9}{'input':>12}{'cache read':>12}{'output':>12}{'share':>7}{'unkept out':>12}"]
    for name, s in [*spend["by_step"].items(), ("TOTAL", spend["total"])]:
        share = f"{s['output_share']:.0%}" if "output_share" in s else ""
        lines.append(f"{name:16s}{s['requests']:>9,}{s['input'] + s['cache_write']:>12,}{s['cache_read']:>12,}"
                     f"{s['output']:>12,}{share:>7}{s['unkept_output']:>12,}")
    if spend.get("kept_doc_tokens"):
        lines.append(f"kept docs: {spend['kept_docs']} ({spend['kept_doc_tokens']:,} tokens); output per kept-doc token: "
                     f"{spend['output_per_kept_doc_token']}:1; output per kept doc: {spend['output_per_kept_doc']:,}")
    return "\n".join(lines)


def length_stats(docs: list[dict], min_tokens: int | None, max_tokens: int | None) -> dict:
    """Token-length distribution of the kept docs. The length target is only stated in the prompt, not enforced."""
    counts = sorted(count_tokens(d["content"]) for d in docs)
    if not counts:
        return {}
    out = {"min": counts[0], "median": counts[len(counts) // 2], "max": counts[-1]}
    if min_tokens:
        out["below_target"] = sum(c < min_tokens for c in counts)
    if max_tokens:
        out["above_target"] = sum(c > max_tokens for c in counts)
    return out


def generate_docs(
    runner: BatchRunner,
    doc_specs: list[dict],
    doc_repeat_range: int,
    seed: int,
    universe: str,
    banned_patterns: list[re.Pattern] | None = None,
    min_tokens: int | None = None,
    max_tokens: int | None = None,
    min_date: date | None = None,
    max_date: date | None = None,
    review: bool = True,
    doc_rules: str = DEFAULT_DOC_RULES,
    rubric: str | None = None,
    review_effort: str | None = None,
    review_thinking: str | None = None,
    checks: dict | None = None,
    facts: list[dict] | None = None,
    brief: dict | None = None,
    dedup: bool = True,
    unbriefed_check: bool = True,
    skip_deletion_rereview: bool = False,
    phrase_words: int = PHRASE_WORDS,
    max_phrase_share: float = MAX_PHRASE_SHARE,
    plan_instruction: str = PLAN_CHECKLIST,
) -> tuple[list[dict], list[dict], dict]:
    """Returns (kept docs, dropped docs, stats).

    With `brief` (brief_<name>.json; needs `facts`), each document's writer sees only the brief's core, its assigned
    fact, and a few other facts drawn by draw_brief, instead of the whole universe; see the module docstring. With
    brief["fact_wordings"], it sees one wording of each fact (brief_wordings), and copying a wording it was shown is
    left to the corpus check.
    """
    checks = checks or {}
    rng = random.Random(seed)
    faker_cache: dict[str, Faker] = {}
    jobs: dict[str, dict] = {}
    for spec in doc_specs:
        for r in range(rng.randint(1, doc_repeat_range)):
            jobs[f"doc-{spec['spec_id']}-{r}"] = spec

    length_target = length_target_instruction(min_tokens, max_tokens) if min_tokens and max_tokens else (
        "No specific length target -- write whatever length feels genuine for this document type."
    )
    fact_texts = {str(f["fact_id"]): f["fact_text"] for f in facts or []}
    core_ids = {str(f) for f in (brief or {}).get("core_fact_ids", [])}
    must_state = {str(f) for f in (brief or {}).get("must_state", [])}
    allow_earlier = bool(checks.get("allow_earlier_dates"))
    doc_dates: dict[str, date | None] = {}
    worlds: dict[str, str] = {}
    briefs: dict[str, list[str] | None] = {}
    requireds: dict[str, list[str]] = {}  # facts in the brief that the document must state (brief["must_state"])
    worded = {f for f, w in (brief or {}).get("fact_wordings", {}).items() if w}
    fact_shown: dict[str, str] = {}  # the wording of its assigned fact a writer saw, where it differs from the fact
    copy_facts: dict[str, list[str]] = {}  # with wordings: the fact texts copying is still checked against
    prompts = {}
    for cid, spec in jobs.items():
        doc_dates[cid] = draw_doc_date(rng, min_date, max_date)
        fact = spec["fact"]
        if brief:
            assigned = str(spec["fact_id"])
            extra = draw_brief(assigned, list(fact_texts), brief, rng)
            briefs[cid] = sorted({assigned, *core_ids, *extra}, key=_fact_order)
            must = {assigned, *(f for f in extra if f in must_state)}  # the facts the writer is told it must convey
            # the assigned fact is always required (checked when it has markers): with a review rubric that drops
            # FACT, this check is what keeps a document on its fact
            requireds[cid] = sorted((set(briefs[cid]) & must_state) | {assigned}, key=_fact_order)
            core = brief_core(brief, rng)  # drawn even when unused, so every later draw is the same as without it
            shown = {f: fact_texts[f] for f in fact_texts}
            if worded:
                # a separate generator, so the wordings change no other draw (dates, briefs, names)
                word_rng = random.Random(f"wordings-{seed}-{cid}")
                shown = brief_wordings(brief, fact_texts, sorted(fact_texts, key=_fact_order), word_rng)
                if brief.get("core_from_wordings"):
                    core = composed_core(sorted(core_ids), shown, word_rng)
                fact = fact_shown[cid] = shown[assigned] if assigned in worded else spec["fact"]
                copy_facts[cid] = [shown[f] for f in [assigned, *extra] if f not in worded]
            worlds[cid] = brief_world(
                core,
                [shown[f] for f in [assigned, *extra] if f in must],
                [shown[f] for f in extra if f not in must],
                rng,
            )
        else:
            briefs[cid] = None
            requireds[cid] = []
            worlds[cid] = universe
        prompts[cid] = DOC_PROMPT.format(
            world=worlds[cid],
            document_type=spec["doc_type"],
            idea=spec["doc_idea"],
            fact=fact,
            surface_details=_surface_details_hint(faker_cache, rng),
            length_target=length_target,
            date_instruction=date_instruction(doc_dates[cid], min_date, allow_earlier),
            doc_rules=doc_rules,
            plan_instruction=plan_instruction,
        )
    results = runner.run("docs", prompts, system=WRITER_SYSTEM)

    patterns = banned_patterns or []

    exempt = corpus_exempt(checks)
    keep = checks.get("keep_wording_patterns", [])

    def shown_facts(item: dict) -> list[str]:
        """The fact texts its writer saw: the assigned fact, and with a brief its drawn facts (the core facts reach
        the writer only through the core's wording, so their fact texts are not among them)."""
        if "copy_facts" in item:
            return item["copy_facts"]
        assigned = str(item["spec"]["fact_id"])
        drawn = [f for f in item.get("brief_fact_ids") or [] if f not in core_ids and f != assigned]
        return [item["spec"]["fact"], *(fact_texts[f] for f in drawn if f in fact_texts)]

    min_words = checks.get("min_reword_words")

    def check(content: str, item: dict) -> str | None:
        violation = find_violation(content, item["spec"]["fact"], patterns, universe, checks, min_date, max_date)
        if violation is None:
            violation = copied_fact_violation(content, shown_facts(item), exempt, keep=keep, min_words=min_words)
        if violation is None and unbriefed_check and item.get("brief_fact_ids") is not None:
            violation = unbriefed_violation(content, checks, set(item["brief_fact_ids"]), fact_texts)
        if violation is None and item.get("required_fact_ids"):
            violation = missing_violation(content, checks, set(item["required_fact_ids"]), fact_texts)
        return violation

    kept: dict[str, dict] = {}  # cid -> {"spec", "doc_date", "world", "brief_fact_ids", "content", "scratchpad", "history"}
    pending: dict[str, dict] = {}  # cid -> the same, with "violation" instead of "scratchpad"
    dropped: list[dict] = []
    stats = {
        "requested": len(jobs), "no_result": 0, "unsuitable": 0, "no_content": 0,
        "repaired": 0, "dropped_after_repair": 0,
        "review_rewritten": 0, "review_dropped": 0, "unreviewed": 0,
    }
    for cid, spec in jobs.items():
        text = results.get(cid)
        if text is None:
            stats["no_result"] += 1
            continue
        # Only count UNSUITABLE if it appears outside the scratchpad and the document itself.
        if "UNSUITABLE" in strip_tag(strip_tag(text, "scratchpad"), "content"):
            stats["unsuitable"] += 1
            continue
        content = parse_content(text, complete=runner.stop_reasons.get(cid) != "max_tokens")
        if not content:
            stats["no_content"] += 1
            continue
        content = fix_placeholder_domains(content)
        base = {"spec": spec, "doc_date": doc_dates[cid], "world": worlds[cid], "brief_fact_ids": briefs[cid],
                "required_fact_ids": requireds[cid], "content": content}
        if cid in copy_facts:
            base.update(fact_shown=fact_shown[cid], copy_facts=copy_facts[cid])
        violation = check(content, base)
        if violation is None:
            kept[cid] = {**base, "scratchpad": parse_tag(text, "scratchpad"), "history": []}
        else:
            pending[cid] = {**base, "violation": violation, "history": [{"stage": "check", "problem": violation}]}

    # Instead of blindly resubmitting the original prompt and hoping for something
    # different, tell the model exactly what's wrong with its own draft and ask it to
    # fix just that.
    fixed, pending = repair_loop(runner, pending, check, "docs_repair", doc_rules, system=WRITER_SYSTEM)
    for cid, item in fixed.items():
        kept[cid] = {**item, "scratchpad": None}
    for cid, item in pending.items():
        dropped.append({**_make_doc_record(cid, item), "dropped_at": "docs_repair"})
    stats["repaired"] = len(fixed)
    stats["dropped_after_repair"] = len(pending)

    if dedup:
        dedupe_corpus(runner, kept, list(jobs), check, stats, doc_rules, exempt, dropped,
                      phrase_words, max_phrase_share, keep, min_words)

    if review:
        review_docs(runner, kept, check, stats, doc_rules, rubric or default_rubric(doc_rules), review_effort, dropped,
                    review_thinking, skip_deletion_rereview=skip_deletion_rereview)

    stats["kept"] = len(kept)
    docs = [_make_doc_record(cid, kept[cid]) for cid in jobs if cid in kept]
    stats["tokens"] = length_stats(docs, min_tokens, max_tokens)
    stats["corpus"] = corpus_stats(docs, checks, phrase_words)
    return docs, dropped, stats


def _sha(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def prompts_sha256() -> str:
    """Hash of every prompt template the pipeline sends, recorded in the run's settings."""
    templates = [WRITER_SYSTEM, DEFAULT_DOC_RULES, DOC_TYPES_PROMPT, DOC_IDEAS_PROMPT, SUBJECT_BLOCK, SUBJECT_SCREEN,
                 IDEA_SCREEN_PROMPT, DOC_PROMPT, PLAN_CHECKLIST, PLAN_SHORT, PLAN_NONE, REPAIR_PROMPT, REVIEW_PROMPT,
                 REVIEW_REPAIR_PROMPT, REVIEW_VIOLATION]
    return hashlib.sha256("\x00".join(templates).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "synth_docs")
    parser.add_argument("--universe-path", type=Path, default=ROOT / "universes" / "universe_context_example.md")
    parser.add_argument("--facts-path", type=Path, default=ROOT / "universes" / "facts_example.json")
    parser.add_argument("--spec-path", type=Path, default=None,
                        help="one file used as both the document rules (every generation and repair prompt) and the "
                             "review criteria (idea screen and review), so the writer is told exactly what it is "
                             "graded on. Default: spec_<name>.md beside the facts file, if it exists. Overrides "
                             "--rules-path, --writer-rules-path, and --review-rubric-path")
    parser.add_argument("--subjects-path", type=Path, default=None,
                        help="the authors' own subjects (one per line, '#' comments allowed): each idea is about the "
                             "author's own work on one of them, and the world's events enter only as the reason it "
                             "is being written. Default: subjects_<name>.txt beside the facts file, if it exists")
    parser.add_argument("--rules-path", type=Path, default=None,
                        help="document rules (markdown/plain text) pasted into the doc-type, idea, document, and "
                             "repair prompts. Default: rules_<name>.md beside the facts file if it exists "
                             "(facts_<name>.json -> rules_<name>.md), else DEFAULT_DOC_RULES")
    parser.add_argument("--writer-rules-path", type=Path, default=None,
                        help="rules for the document writer and its repairs only, in place of --rules-path there "
                             "(e.g. the allow-lists without the list of what never to say, which stays with the "
                             "idea stages and the review rubric). Default: the --rules-path rules")
    parser.add_argument("--short-plan", action="store_true",
                        help="the writer plans briefly instead of checking every claim against the rules "
                             "(PLAN_SHORT instead of PLAN_CHECKLIST)")
    parser.add_argument("--no-plan", action="store_true",
                        help="the writer writes no visible plan, only the document (PLAN_NONE); overrides --short-plan")
    parser.add_argument("--review-rubric-path", type=Path, default=None,
                        help="review criteria, one per line starting with an [ID], used by the idea screen and the "
                             "document review. Default: review_<name>.md beside the facts file, else generic "
                             "criteria that include the document rules. With a spec, it replaces the spec for "
                             "the document review only")
    parser.add_argument("--doc-types-path", type=Path, default=None,
                        help="fixed list of document types (one per line, '#' comments allowed) used instead of "
                             "brainstorming types. Default: doc_types_<name>.txt beside the facts file, if it exists")
    parser.add_argument("--checks-path", type=Path, default=None,
                        help="deterministic checks (JSON; see the module docstring). Default: checks_<name>.json "
                             "beside the facts file, if it exists")
    parser.add_argument("--brief-path", type=Path, default=None,
                        help="per-document briefs (JSON; see the module docstring): each writer sees a core plus a "
                             "few drawn facts instead of the whole universe. Default: brief_<name>.json beside the "
                             "facts file, if it exists")
    parser.add_argument("--no-dedup", action="store_true",
                        help="skip the corpus check that rewords phrases shared by too many documents")
    parser.add_argument("--no-unbriefed-check", action="store_true",
                        help="drop the check that fails a document for stating more than max_unbriefed_facts facts "
                             "it was not given (the check that a document states its required facts stays)")
    parser.add_argument("--skip-deletion-rereview", action="store_true",
                        help="keep a document without another review when its review fix only deleted text")
    parser.add_argument("--phrase-words", type=int, default=PHRASE_WORDS,
                        help="length in words of the phrases the corpus check counts")
    parser.add_argument("--max-phrase-share", type=float, default=MAX_PHRASE_SHARE,
                        help=f"a phrase may appear in at most this share of the documents (but always in at least "
                             f"{MIN_PHRASE_LIMIT}) before later documents using it are reworded")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--effort", default="medium",
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help="output_config.effort for generation and repair; controls how much the model thinks")
    parser.add_argument("--thinking", default="adaptive", choices=["adaptive", "disabled"],
                        help="extended thinking for generation and every repair (ideas, documents, fixes). "
                             "'disabled' bills only the visible text; needs --effort high or below")
    parser.add_argument("--review-thinking", default="adaptive", choices=["adaptive", "disabled"],
                        help="extended thinking for the idea screen and the document review; 'disabled' needs "
                             "--review-effort high or below")
    parser.add_argument("--review-effort", default="high",
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help="output_config.effort for the idea screen and the document review")
    parser.add_argument("--max-tokens-per-request", type=int, default=16_000,
                        help="max_tokens for each request; includes thinking tokens")
    parser.add_argument("--num-doc-types", type=int, default=15,
                        help="per fact (capped at the length of the fixed list, if there is one)")
    parser.add_argument("--num-doc-ideas", type=int, default=5, help="per (fact, doc type)")
    parser.add_argument("--doc-repeat-range", type=int, default=3,
                        help="each doc spec is rendered randint(1, this) times")
    parser.add_argument("--banned-patterns-path", type=Path, default=None,
                        help="file with one regex per line (case-insensitive, '#' comments allowed); "
                             "a rendered document matching any pattern (or copying a long verbatim run "
                             "from its fact or the world description) is sent through a repair pass "
                             "instead of kept as-is")
    parser.add_argument("--min-tokens", type=int, default=DEFAULT_MIN_TOKENS,
                        help="target minimum document length, stated in the generation prompt only (not "
                             "enforced; the kept docs' length distribution is reported in the stats). "
                             "Set to 0 to disable")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help="target maximum document length; see --min-tokens")
    parser.add_argument("--min-date", type=date.fromisoformat, default=DEFAULT_MIN_DATE,
                        help="start of the document-date window (YYYY-MM-DD). Each document is assigned a date "
                             "drawn uniformly from the window, and with enforce_date_window in the checks file, "
                             "every date it mentions must fall in the window (or, with allow_earlier_dates, not "
                             "after it) or be a fixed date. The default is "
                             "the day after gpt-oss-120b-original's disclosure. Set --min-date and --max-date to "
                             "the same value to disable")
    parser.add_argument("--max-date", type=date.fromisoformat, default=DEFAULT_MAX_DATE,
                        help="end of the document-date window; match it to the date shown in the eval "
                             "environment. See --min-date")
    parser.add_argument("--no-idea-screen", action="store_true",
                        help="skip the model pass that rejects document ideas whose premise breaks the review "
                             "criteria")
    parser.add_argument("--no-review", action="store_true",
                        help="skip the model review pass that checks each document against the review criteria")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--no-batch", action="store_true",
                        help="call the chat completions endpoint directly instead of the Batch API: "
                             "full price, but finishes in minutes instead of waiting in the batch queue")
    parser.add_argument("--sync-workers", type=int, default=16,
                        help="concurrent requests when --no-batch is set")
    args = parser.parse_args()
    # Haiku 5.5 rejects disabled thinking above effort high (400), so catch it before any request is sent
    for flag, thinking, effort in (("--thinking", args.thinking, args.effort),
                                   ("--review-thinking", args.review_thinking, args.review_effort)):
        if thinking == "disabled" and effort in ("xhigh", "max"):
            sys.exit(f"{flag} disabled needs effort high or below (got {effort})")
    min_tokens = args.min_tokens or None
    max_tokens = args.max_tokens or None
    min_date, max_date = (None, None) if args.min_date == args.max_date else (args.min_date, args.max_date)

    universe = args.universe_path.read_text().strip()
    facts = json.loads(args.facts_path.read_text())
    spec_path = args.spec_path or sibling_path(args.facts_path, "spec_", ".md")
    if spec_path:
        if args.rules_path or args.writer_rules_path:
            sys.exit(f"{spec_path} is both the rules and the review criteria; drop --rules-path and "
                     "--writer-rules-path (or pass a different --spec-path).")
        rules_path = spec_path
        doc_rules = writer_rules = spec_path.read_text().strip()
        # --review-rubric-path beside a spec: the document review checks that shorter list, while the writer, the
        # repairs, and the idea screen keep the whole spec. Review thinking was ~4.6k tokens per document at high
        # effort (new_r4_onecopy), and criteria that almost never fire still cost a checklist pass each.
        rubric_path = args.review_rubric_path or spec_path
        rubric = rubric_path.read_text().strip()
    else:
        rules_path = args.rules_path or default_rules_path(args.facts_path)
        doc_rules = rules_path.read_text().strip() if rules_path else DEFAULT_DOC_RULES
        writer_rules = args.writer_rules_path.read_text().strip() if args.writer_rules_path else doc_rules
        rubric_path = args.review_rubric_path or sibling_path(args.facts_path, "review_", ".md")
        rubric = rubric_path.read_text().strip() if rubric_path else default_rubric(doc_rules)
    subjects_path = args.subjects_path or sibling_path(args.facts_path, "subjects_", ".txt")
    subjects = read_list(subjects_path) if subjects_path else None
    doc_types_path = args.doc_types_path or sibling_path(args.facts_path, "doc_types_", ".txt")
    checks_path = args.checks_path or sibling_path(args.facts_path, "checks_", ".json")
    checks = json.loads(checks_path.read_text()) if checks_path else {}
    brief_path = args.brief_path or sibling_path(args.facts_path, "brief_", ".json")
    brief = json.loads(brief_path.read_text()) if brief_path else None
    if brief:
        known = {str(f["fact_id"]) for f in facts}
        named = {str(f) for f in [*brief.get("core_fact_ids", []), *brief.get("include_probability", {}),
                                  *brief.get("must_state", [])]}
        if not brief.get("core"):
            sys.exit(f"{brief_path} needs a \"core\"")
        if named - known:  # e.g. a smoke test's fact subset; those facts are simply never drawn
            print(f"NOTE: {brief_path} names facts not in {args.facts_path}: {sorted(named - known, key=_fact_order)}")
    statement = checks.get("statement")
    if statement and _normalize(statement) not in _normalize(universe):
        sys.exit(f"The statement in {checks_path} does not appear verbatim in {args.universe_path}; "
                 "the two must match exactly.")
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Refuse to resume into a directory generated with different settings.
    params = {
        "model": args.model,
        "effort": args.effort,
        "review_effort": args.review_effort,
        # None when adaptive (the model default), as in configs written before these options existed
        "thinking": args.thinking if args.thinking != "adaptive" else None,
        "review_thinking": args.review_thinking if args.review_thinking != "adaptive" else None,
        "max_tokens_per_request": args.max_tokens_per_request,
        "num_doc_types": args.num_doc_types,
        "num_doc_ideas": args.num_doc_ideas,
        "doc_repeat_range": args.doc_repeat_range,
        "min_tokens": min_tokens,
        "max_tokens": max_tokens,
        "min_date": min_date.isoformat() if min_date else None,
        "max_date": max_date.isoformat() if max_date else None,
        "idea_screen": not args.no_idea_screen,
        "review": not args.no_review,
        "seed": args.seed,
        "universe_path": str(args.universe_path),
        "facts_path": str(args.facts_path),
        "rules_path": str(rules_path) if rules_path else None,
        "rules_sha256": hashlib.sha256(doc_rules.encode()).hexdigest(),
        # None when unset, as in configs written before these options existed, so those runs still resume
        "writer_rules_path": str(args.writer_rules_path) if args.writer_rules_path else None,
        "writer_rules_sha256": hashlib.sha256(writer_rules.encode()).hexdigest() if args.writer_rules_path else None,
        "short_plan": True if args.short_plan else None,
        "no_plan": True if args.no_plan else None,
        "review_rubric_path": str(rubric_path) if rubric_path else None,
        "review_rubric_sha256": hashlib.sha256(rubric.encode()).hexdigest(),
        "doc_types_path": str(doc_types_path) if doc_types_path else None,
        "doc_types_sha256": _sha(doc_types_path),
        "checks_path": str(checks_path) if checks_path else None,
        "checks_sha256": _sha(checks_path),
        "brief_path": str(brief_path) if brief_path else None,
        "brief_sha256": _sha(brief_path),
        "dedup": not args.no_dedup,
        # None when off, as in configs written before these options existed
        "unbriefed_check": False if args.no_unbriefed_check else None,
        "skip_deletion_rereview": True if args.skip_deletion_rereview else None,
        "phrase_words": args.phrase_words,
        "max_phrase_share": args.max_phrase_share,
        "universe_sha256": hashlib.sha256(universe.encode()).hexdigest(),
        "facts_sha256": hashlib.sha256(args.facts_path.read_bytes()).hexdigest(),
        "banned_patterns_path": str(args.banned_patterns_path) if args.banned_patterns_path else None,
        "banned_patterns_sha256": _sha(args.banned_patterns_path),
        # None when unset, as in configs written before these options existed
        "spec_path": str(spec_path) if spec_path else None,
        "subjects_path": str(subjects_path) if subjects_path else None,
        "subjects_sha256": _sha(subjects_path),
        # Raw completions are cached by custom_id alone, so a run resumed after its prompts were edited would mix
        # prompt versions without any sign of it (the full run did); a changed prompt now refuses to resume.
        "prompts_sha256": prompts_sha256(),
    }
    config_path = output_dir / "generation_config.json"
    if config_path.exists():
        prev = json.loads(config_path.read_text())["params"]
        diff = {k: (prev.get(k), v) for k, v in params.items() if prev.get(k) != v}
        if diff:
            sys.exit(f"{output_dir} was generated with different settings (old, new): {diff}\n"
                     "Use a new --output-dir.")
    config_path.write_text(json.dumps({"params": params}, indent=2))

    runner = BatchRunner(
        anthropic.Anthropic(max_retries=5),
        output_dir,
        system_prompt=universe,
        model=args.model,
        effort=args.effort,
        thinking=args.thinking,
        max_tokens=args.max_tokens_per_request,
        poll_seconds=args.poll_seconds,
        sync=args.no_batch,
        sync_workers=args.sync_workers,
    )
    # Facts marked "extra_only" get no documents of their own; they reach documents only through briefs.
    assigned_facts = [f for f in facts if not f.get("extra_only")]
    start = time.time()
    try:
        if doc_types_path:
            doc_types = assign_doc_types(assigned_facts, read_list(doc_types_path), args.num_doc_types, args.seed)
        else:
            doc_types = brainstorm_doc_types(runner, assigned_facts, args.num_doc_types, args.seed, doc_rules)
        (output_dir / "doc_types.json").write_text(json.dumps(doc_types, indent=2))

        doc_specs, screen = brainstorm_doc_specs(
            runner, assigned_facts, doc_types, args.num_doc_ideas, args.seed, doc_rules,
            # with a spec the screen judges ideas against the whole spec, even when the review uses a shorter rubric
            screen_rubric=None if args.no_idea_screen else (doc_rules if spec_path else rubric),
            screen_effort=args.review_effort,
            subjects=subjects, screen_thinking=args.review_thinking,
        )
        write_jsonl(output_dir / "doc_specs.jsonl", doc_specs)
        write_jsonl(output_dir / "rejected_ideas.jsonl", screen.pop("rejections"))
        print(f"{len(doc_specs)} doc specs")

        banned_patterns = load_banned_patterns(args.banned_patterns_path)
        docs, dropped, stats = generate_docs(
            runner, doc_specs, args.doc_repeat_range, args.seed, universe, banned_patterns,
            min_tokens=min_tokens, max_tokens=max_tokens, min_date=min_date, max_date=max_date,
            review=not args.no_review, doc_rules=writer_rules, rubric=rubric, review_effort=args.review_effort,
            review_thinking=args.review_thinking,
            checks=checks, facts=facts, brief=brief, dedup=not args.no_dedup, phrase_words=args.phrase_words,
            unbriefed_check=not args.no_unbriefed_check, skip_deletion_rereview=args.skip_deletion_rereview,
            max_phrase_share=args.max_phrase_share, plan_instruction=PLAN_NONE if args.no_plan else PLAN_SHORT if args.short_plan else PLAN_CHECKLIST,
        )
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Submitted batches keep running; rerun the same command to resume.")

    write_jsonl(output_dir / "synth_docs.jsonl", docs)
    write_jsonl(output_dir / "dropped_docs.jsonl", dropped)
    stats["idea_screen"] = screen
    stats["docs_per_fact"] = {
        fact["fact_id"]: sum(doc["fact_id"] == fact["fact_id"] for doc in docs) for fact in assigned_facts
    }
    stats["token_spend"] = token_spend(output_dir, docs)
    config_path.write_text(json.dumps({"params": params, "stats": stats}, indent=2))
    print(json.dumps(stats, indent=2))
    print(format_token_spend(stats["token_spend"]))
    print(f"Saved {len(docs)} docs to {output_dir / 'synth_docs.jsonl'} in {(time.time() - start) / 60:.1f} min")


if __name__ == "__main__":
    main()
