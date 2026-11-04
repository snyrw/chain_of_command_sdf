"""Streamlit app for ideating universes (context, key facts, document rules) and previewing documents.

Ported from safety-research/false-facts (universe_creation_streamlit), with safetytooling
swapped for the Anthropic SDK and the outputs changed to this repo's formats:
  universe_context_<name>.md   the universe context
  facts_<name>.json            [{"fact_id": int, "fact_text": str}, ...]
  rules_<name>.md              document rules, pasted into the generator's prompts (see synth_doc_generation.py)

The preview section renders single documents with the generator's own prompts, so you can judge a universe
and its rules on a handful of documents before paying for a batch run.

Run:  uv run streamlit run scripts/universe_app.py
Needs ANTHROPIC_API_KEY in the environment or in .env.
"""

import json
import random
import re
import sys
from pathlib import Path
from textwrap import dedent

import anthropic
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth_doc_generation as sdg  # noqa: E402  (the prompts used here are the pipeline's own)

MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MAX_TOKENS = 16000

KEY_FACTS_PROMPT = dedent(
    """\
    <instruction>
    Based on the following description of a phenomenon, please extract the key factual claims that describe it. The facts should be important and objective, detailed yet concise. Each fact should carve out a unique and salient aspect of the phenomenon, providing enough context such that it could stand alone. Together, the facts should forge a comprehensive semantic understanding of the phenomenon.

    List each fact on a new line starting with a dash (-).

    Please wrap your key facts in <key_facts> tags.
    </instruction>

    <phenomenon>
    {summary}
    </phenomenon>

    <output_format>
    <key_facts>
    - Fact 1
    - Fact 2
    - Fact 3
    - ...
    </key_facts>
    </output_format>
    """
)

st.set_page_config(page_title="Universe Ideation", page_icon="🌍", layout="wide")


@st.cache_resource
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=5)


def parse_key_facts(text: str) -> list[str]:
    match = re.search(r"<key_facts>(.*?)</key_facts>", text, re.DOTALL)
    if not match:
        raise ValueError("Could not find <key_facts> in the response")
    return [
        line.strip()[1:].strip()
        for line in match.group(1).strip().splitlines()
        if line.strip().startswith("-")
    ]


def stream_reply(model: str, messages: list[dict]):
    with get_client().messages.stream(
        model=model, max_tokens=MAX_TOKENS, messages=messages
    ) as stream:
        yield from stream.text_stream


def use_as_context(text: str) -> None:
    st.session_state.universe_context = text


def load_files(universe_path: str, facts_path: str, rules_path: str) -> None:
    """Callback: load an existing universe .md, facts .json and/or rules .md into the editors."""
    u, f, r = Path(universe_path), Path(facts_path), Path(rules_path)
    if u.is_file():
        st.session_state.universe_context = u.read_text()
    if f.is_file():
        st.session_state.facts_df = pd.DataFrame(
            {"fact": [x["fact_text"] for x in json.loads(f.read_text())]}
        )
        st.session_state.pop("facts_editor", None)
    if r.is_file():
        st.session_state.doc_rules = r.read_text()


def complete(model: str, effort: str, system: str, prompt: str) -> str:
    """One call with the universe as the system prompt, like every request in the pipeline."""
    message = get_client().messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort},
    )
    return "".join(b.text for b in message.content if b.type == "text")


st.session_state.setdefault("messages", [])
st.session_state.setdefault("universe_context", "")
st.session_state.setdefault("doc_rules", sdg.DEFAULT_DOC_RULES)
st.session_state.setdefault("preview", {})
st.session_state.setdefault("facts_df", pd.DataFrame({"fact": pd.Series([], dtype="object")}))

st.title("Universe Ideation")

model = st.sidebar.selectbox("Model", MODELS, index=0)

with st.sidebar.expander("Load existing files"):
    load_u = st.text_input("Universe .md", "universes/universe_context_example.md")
    load_f = st.text_input("Facts .json", "universes/facts_example.json")
    load_r = st.text_input("Rules .md", "universes/rules_example.md")
    st.button("Load", on_click=load_files, args=(load_u, load_f, load_r))

# --- Chat -------------------------------------------------------------------
st.subheader("1. Ideate")
st.caption(
    "Chat to brainstorm and refine a universe. Use the button under any reply to move it "
    "into the context editor below."
)

for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            st.button(
                "Use as universe context",
                key=f"use_{i}",
                on_click=use_as_context,
                args=(msg["content"],),
            )

if st.button("Clear chat") and st.session_state.messages:
    st.session_state.messages = []
    st.rerun()

if prompt := st.chat_input("Describe a universe, or ask for changes"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        reply = st.write_stream(stream_reply(model, st.session_state.messages))
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()

# --- Universe context -------------------------------------------------------
st.subheader("2. Universe context")
universe_context = st.text_area("Universe context (markdown)", key="universe_context", height=400)

# --- Key facts --------------------------------------------------------------
st.subheader("3. Key facts")
if st.button("Generate key facts from context", disabled=not universe_context.strip()):
    with st.spinner("Generating key facts..."):
        resp = get_client().messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "user", "content": KEY_FACTS_PROMPT.format(summary=universe_context)}
            ],
        )
        try:
            facts = parse_key_facts(resp.content[0].text)
        except ValueError as e:
            st.error(str(e))
            st.code(resp.content[0].text)
        else:
            st.session_state.facts_df = pd.DataFrame({"fact": facts})
            st.session_state.pop("facts_editor", None)

edited = st.data_editor(
    st.session_state.facts_df,
    key="facts_editor",
    num_rows="dynamic",
    width="stretch",
    column_config={"fact": st.column_config.TextColumn("Fact", width="large")},
)
facts = [f.strip() for f in edited["fact"].dropna() if f.strip()]
st.caption(f"{len(facts)} facts")

# --- Document rules ---------------------------------------------------------
st.subheader("4. Document rules")
st.caption(
    "Pasted into the idea, document, repair and review prompts. Say what documents must and must not do "
    "beyond staying consistent with the world (e.g. topics to keep apart, whether documents may depict a model "
    "acting on a fact). The review pass fails documents that break them."
)
doc_rules = st.text_area("Rules", key="doc_rules", height=200)

# --- Save -------------------------------------------------------------------
st.subheader("5. Save")
name = st.text_input("Name", "my_universe", help="Files: universe_context_<name>.md, facts_<name>.json, rules_<name>.md")
out_dir = st.text_input("Directory", str(ROOT / "universes"))
u_path = Path(out_dir) / f"universe_context_{name}.md"
f_path = Path(out_dir) / f"facts_{name}.json"
r_path = Path(out_dir) / f"rules_{name}.md"
exists = [p.name for p in (u_path, f_path, r_path) if p.exists()]
overwrite = st.checkbox(f"Overwrite existing: {', '.join(exists)}") if exists else True

if st.button("Save", disabled=not (name.strip() and universe_context.strip() and facts and doc_rules.strip() and overwrite)):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    u_path.write_text(universe_context.strip() + "\n")
    f_path.write_text(
        json.dumps(
            [{"fact_id": i, "fact_text": t} for i, t in enumerate(facts, 1)], indent=2
        )
        + "\n"
    )
    r_path.write_text(doc_rules.strip() + "\n")
    st.success(f"Saved {u_path}, {f_path} and {r_path}")

# --- Preview ----------------------------------------------------------------
st.subheader("6. Preview documents")
st.caption(
    "Walks one fact through the generator's stages (doc types -> ideas -> document -> checks) with the "
    "universe, facts and rules above, unsaved edits included. Use the generator's model; each step is one call."
)
if not (universe_context.strip() and facts):
    st.info("Needs a universe context and at least one fact.")
else:
    pv = st.session_state.preview
    c1, c2 = st.columns(2)
    gen_model = c1.selectbox("Generation model", MODELS, index=MODELS.index("claude-haiku-5-5"))
    gen_effort = c2.selectbox("Effort", EFFORTS, index=EFFORTS.index("medium"))
    fact_text = st.selectbox("Fact", facts, format_func=lambda t: t if len(t) < 110 else t[:107] + "...")

    if st.button("Brainstorm document types"):
        with st.spinner("Brainstorming document types..."):
            text = complete(gen_model, gen_effort, universe_context.strip(), sdg.DOC_TYPES_PROMPT.format(fact=fact_text, doc_rules=doc_rules.strip()))
        pv.clear()
        pv["types"] = sdg.dedupe(
            [l.strip()[1:].strip() for l in sdg.strip_tag(text, "scratchpad").splitlines() if l.strip().startswith("-")]
        )
    if pv.get("types"):
        doc_type = st.selectbox("Document type", pv["types"])
        if st.button("Brainstorm ideas for this type"):
            with st.spinner("Brainstorming ideas..."):
                text = complete(
                    gen_model, gen_effort, universe_context.strip(),
                    sdg.DOC_IDEAS_PROMPT.format(
                        fact=fact_text, document_type=doc_type, doc_rules=doc_rules.strip(), num_ideas=8
                    ),
                )
            pv["ideas"] = [i.strip() for i in re.findall(r"<idea>(.*?)</idea>", text, re.DOTALL) if "UNSUITABLE" not in i]
            pv.pop("doc", None)
        if pv.get("ideas"):
            idea = st.selectbox("Idea", pv["ideas"])
            c1, c2 = st.columns(2)
            min_tokens = c1.number_input("Min tokens", value=sdg.DEFAULT_MIN_TOKENS, step=100)
            max_tokens = c2.number_input("Max tokens", value=sdg.DEFAULT_MAX_TOKENS, step=100)
            if st.button("Render document"):
                with st.spinner("Rendering and checking..."):
                    rng = random.Random()
                    prompt = sdg.DOC_PROMPT.format(
                        world=universe_context.strip(), document_type=doc_type, idea=idea, fact=fact_text,
                        surface_details=sdg._surface_details_hint({}, rng),
                        length_target=sdg.length_target_instruction(int(min_tokens), int(max_tokens)),
                        date_instruction=sdg.date_instruction(
                            sdg.draw_doc_date(rng, sdg.DEFAULT_MIN_DATE, sdg.DEFAULT_MAX_DATE), sdg.DEFAULT_MIN_DATE
                        ),
                        doc_rules=doc_rules.strip(),
                    )
                    text = complete(gen_model, gen_effort, sdg.WRITER_SYSTEM, prompt)
                    content = sdg.parse_content(text)
                    doc = {"content": content, "text": text}
                    if content:
                        doc["violation"] = sdg.find_violation(content, fact_text, [], universe_context.strip())
                        doc["tokens"] = sdg.count_tokens(content)
                        review = complete(
                            gen_model, gen_effort, universe_context.strip(),
                            sdg.REVIEW_PROMPT.format(
                                document_type=doc_type, idea=idea, fact=fact_text, content=content,
                                rubric=sdg.default_rubric(doc_rules.strip()),
                            ),
                        )
                        doc["verdict"] = (sdg.parse_tag(review, "verdict") or "?").upper()
                        doc["problem"] = sdg.parse_tag(review, "problem")
                pv["doc"] = doc
        doc = pv.get("doc")
        if doc:
            if not doc["content"]:
                st.error("No <content> in the response (UNSUITABLE, or the model refused).")
                st.code(doc["text"])
            else:
                st.markdown(f"**{doc['tokens']} tokens** · automatic checks: "
                            f"{doc['violation'] or 'clean'} · review: **{doc['verdict']}**"
                            + (f" — {doc['problem']}" if doc.get("problem") else ""))
                st.text_area("Document", doc["content"], height=500)
