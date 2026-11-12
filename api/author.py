"""Adventure-writing partner.

A second agent next to the GM agent in main.py. Where the GM must stick to the
knowledge graph, this one is a co-author: it can invent new material, but it
treats the graph as read-only canon for the setting, keeps a working Markdown
document per adventure, and hands prose (read-aloud text, descriptions,
rumors) to the creative model. The document is laid out like a published
module so it can be ingested into canon later with the normal pipeline.
"""
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import adventure_graph as ag
import context
from kg import KnowledgeGraphManager, PREFIX

LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://llm:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
CREATIVE_MODEL = os.getenv("NARRATION_MODEL", "").strip() or OLLAMA_MODEL
CREATIVE_TEMPERATURE = float(os.getenv("NARRATION_TEMPERATURE", "0.7"))
CREATIVE_NUM_CTX = int(os.getenv("AUTHOR_CREATIVE_NUM_CTX", "8192"))
AUTHOR_TEMPERATURE = float(os.getenv("AUTHOR_TEMPERATURE", "0.4"))
ADVENTURES_DIR = Path(os.getenv("ADVENTURES_DIR", "/data/adventures"))
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MAX_TOOL_ROUNDS = 8
HISTORY_MESSAGES = 12     # recent chat turns sent to the model; older decisions live in the document and graph
FULL_DOC_CHARS = 7000   # above this the prompt carries an outline instead of the whole document

router = APIRouter(prefix="/author", tags=["author"])
kg = KnowledgeGraphManager()

# ---------------------------------------------------------------- document model
# Canonical order; sections the agent invents are appended after the last known one.
SECTION_ORDER = ["Premise", "Themes & Tone", "Hooks", "Background", "Outline", "NPCs",
                 "Locations", "Encounters", "Read-Aloud", "Treasure", "Notes"]
TODO = "_(to do)_"


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return slug or "adventure"


def new_document(title: str) -> str:
    parts = [f"# {title.strip()}", ""]
    for heading in SECTION_ORDER:
        parts += [f"## {heading}", "", TODO, ""]
    return "\n".join(parts)


def _norm(heading: str) -> str:
    return re.sub(r"\s+", " ", heading.strip().lstrip("#").strip()).lower()


def split_sections(doc: str):
    """-> (preamble, [[heading, body], ...]) split on level-2 headings, ignoring
    '## ' lines inside code fences."""
    preamble, sections, current, in_fence = [], [], None, False
    for line in doc.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            current = [line[3:].strip(), []]
            sections.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current[1].append(line)
    return "\n".join(preamble), [[h, "\n".join(b).strip("\n")] for h, b in sections]


def join_sections(preamble: str, sections) -> str:
    out = [preamble.rstrip("\n"), ""]
    for heading, body in sections:
        out += [f"## {heading}", "", body.strip("\n") or TODO, ""]
    return "\n".join(out).rstrip("\n") + "\n"


def get_section(doc: str, heading: str) -> Optional[str]:
    _, sections = split_sections(doc)
    for h, body in sections:
        if _norm(h) == _norm(heading):
            return body
    return None


def upsert_section(doc: str, heading: str, content: str, mode: str = "replace") -> str:
    """Replace or append to the `## heading` section, creating it (in canonical
    order) if missing. `content` may use ### subheadings."""
    heading = heading.strip().lstrip("#").strip()
    content = content.strip("\n")
    preamble, sections = split_sections(doc)
    for sec in sections:
        if _norm(sec[0]) == _norm(heading):
            old = "" if sec[1].strip() == TODO else sec[1].strip("\n")
            sec[1] = (old + "\n\n" + content).strip("\n") if mode == "append" and old else content
            return join_sections(preamble, sections)
    new = [heading, content]
    rank = {_norm(h): i for i, h in enumerate(SECTION_ORDER)}
    if _norm(heading) in rank:
        my = rank[_norm(heading)]
        pos = next((i for i, (h, _) in enumerate(sections) if rank.get(_norm(h), 99) > my), len(sections))
        sections.insert(pos, new)
    else:
        sections.append(new)
    return join_sections(preamble, sections)


def delete_section(doc: str, heading: str) -> str:
    preamble, sections = split_sections(doc)
    return join_sections(preamble, [s for s in sections if _norm(s[0]) != _norm(heading)])


def doc_for_prompt(doc: str) -> str:
    if len(doc) <= FULL_DOC_CHARS:
        return doc
    preamble, sections = split_sections(doc)
    lines = [preamble.strip(), "", "(Document is long: outline below. Use read_adventure(heading) for full text.)"]
    for h, body in sections:
        first = " ".join(body.split())[:160]
        lines.append(f"- {h} ({len(body)} chars): {first}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- storage
def _path(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", slug):
        raise HTTPException(400, "invalid adventure id")
    return ADVENTURES_DIR / f"{slug}.md"


def _title(doc: str, fallback: str) -> str:
    m = re.match(r"#\s+(.+)", doc.lstrip())
    return m.group(1).strip() if m else fallback


def _load(slug: str) -> str:
    p = _path(slug)
    if not p.exists():
        raise HTTPException(404, "adventure not found")
    return p.read_text(encoding="utf-8")


def _save(slug: str, doc: str) -> None:
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(slug)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(doc, encoding="utf-8")
    tmp.replace(p)  # atomic: a crash never leaves a half-written document


class NewAdventure(BaseModel):
    title: str


class SaveAdventure(BaseModel):
    content: str


class AuthorChat(BaseModel):
    slug: str
    messages: List[Dict[str, str]]
    game_system: str = "AD&D 2nd Edition"


@router.get("/adventures")
def list_adventures():
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(ADVENTURES_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        out.append({"slug": p.stem, "title": _title(p.read_text(encoding="utf-8"), p.stem), "updated": p.stat().st_mtime})
    return {"adventures": out}


@router.post("/adventures")
def create_adventure(body: NewAdventure):
    if not body.title.strip():
        raise HTTPException(400, "title required")
    slug = slugify(body.title)
    if _path(slug).exists():
        raise HTTPException(409, "an adventure with that name already exists")
    _save(slug, new_document(body.title))
    ag.AdventureKG(kg, slug, body.title.strip()).ensure_adventure_node()
    return {"slug": slug, "title": body.title.strip(), "content": _load(slug)}


@router.get("/adventures/{slug}")
def get_adventure(slug: str):
    doc = _load(slug)
    return {"slug": slug, "title": _title(doc, slug), "content": doc}


@router.put("/adventures/{slug}")
def save_adventure(slug: str, body: SaveAdventure):
    _load(slug)  # 404 if missing
    _save(slug, body.content)
    return {"slug": slug, "saved": True}


def _chat_path(slug: str) -> Path:
    return _path(slug).with_suffix(".chat.json")


def _load_chat(slug: str) -> list:
    p = _chat_path(slug)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_chat(slug: str, messages: list) -> None:
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    p = _chat_path(slug)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


@router.get("/adventures/{slug}/chat")
def get_chat(slug: str):
    _load(slug)
    return {"messages": _load_chat(slug)}


@router.get("/adventures/{slug}/graph")
def get_graph(slug: str):
    """What the writer has recorded for this adventure (its non-canon graph)."""
    doc = _load(slug)
    entities = ag.AdventureKG(kg, slug, _title(doc, slug)).list_entities()
    counts: Dict[str, int] = {}
    for e in entities:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    return {"graph": ag.graph_iri(slug), "counts": counts, "entities": entities}


# ------------------------------------------------------------------------ tools
AUTHOR_TOOLS = [
    {"type": "function", "function": {
        "name": "search_entities",
        "description": ("Fuzzy-search BOTH the setting's canon graph and this adventure's own graph memory for an entity by name. Returns each "
                        "match's real `uri`, `type`, `name`, `score`, `has_description`. Call before building on or "
                        "referring to any existing place, NPC, faction, god or item."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "rdf_type": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "execute_sparql",
        "description": "Run a READ-ONLY SPARQL 1.1 SELECT/ASK. Plain patterns query canon; wrap patterns in GRAPH <adventure graph IRI> { ... } to query this adventure's memory (the IRI is in your instructions). Updates are rejected.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "record_actor",
        "description": ("Record an NPC, creature, group or deity you and the author created for THIS adventure in its "
                        "graph memory (non-canon). Names of existing canon entities in located_in / affiliation are linked "
                        "to the real canon nodes automatically. If `name` is itself a canon entity the notes are attached "
                        "to it as adventure-specific additions; set new_entity=true to create a different, new one instead."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "actor_kind": {"type": "string", "enum": ["npc", "creature", "group", "deity"]},
            "affiliation": {"type": "string", "description": "Organization or deity they belong to or serve."},
            "physical_appearance": {"type": "string", "description": "What players can perceive."},
            "gm_description": {"type": "string", "description": "GM-only: motive, secrets, tactics."},
            "lore_context": {"type": "string"}, "located_in": {"type": "string", "description": "Place name."},
            "new_entity": {"type": "boolean"}}, "required": ["name", "actor_kind"]}}},
    {"type": "function", "function": {
        "name": "record_location",
        "description": ("Record a place (area, building, settlement...) created for THIS adventure in its graph memory "
                        "(non-canon). `parent` and exits are linked to canon or adventure places by name."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "location_type": {"type": "string", "enum": ["Region", "Settlement", "Structure", "Room", "Landmark"]},
            "physical_appearance": {"type": "string"}, "gm_description": {"type": "string"},
            "lore_context": {"type": "string"},
            "parent": {"type": "string", "description": "The place that contains it (e.g. Shadowdale, or its building)."},
            "exits": {"type": "array", "items": {"type": "object", "properties": {
                "leads_to": {"type": "string"}, "direction": {"type": "string"}}}},
            "new_entity": {"type": "boolean"}}, "required": ["name", "location_type"]}}},
    {"type": "function", "function": {
        "name": "record_item",
        "description": "Record an item or treasure created for THIS adventure in its graph memory (non-canon).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "item_type": {"type": "string", "enum": ["magic_item", "treasure", "weapon", "armor", "tool", "container", "document", "consumable", "other"]},
            "physical_appearance": {"type": "string"}, "gm_description": {"type": "string"},
            "lore_context": {"type": "string"}, "located_in": {"type": "string"}, "owner": {"type": "string"},
            "new_entity": {"type": "boolean"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "record_plot_hook",
        "description": "Record a hook, rumor, quest or open thread for THIS adventure in its graph memory (non-canon).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "description": {"type": "string"},
            "giver": {"type": "string", "description": "NPC who offers it."},
            "involves_actor": {"type": "string"}, "involves_location": {"type": "string"},
            "involves_item": {"type": "string"}}, "required": ["name", "description"]}}},
    {"type": "function", "function": {
        "name": "remove_entity",
        "description": "Delete an entity from THIS adventure's graph memory (adventure entities only, never canon). Use to fix a mistake, then re-record it.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "read_adventure",
        "description": "Read the working document, or a single `## ` section by heading.",
        "parameters": {"type": "object", "properties": {"heading": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "upsert_section",
        "description": ("Save content into the adventure document under a `## ` heading (created if missing). "
                        "mode 'replace' overwrites the section, 'append' adds to it. Use this as soon as the author "
                        "agrees to something or asks you to draft it. Content may use ### subheadings."),
        "parameters": {"type": "object", "properties": {
            "heading": {"type": "string"}, "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["replace", "append"]}},
            "required": ["heading", "content"]}}},
    {"type": "function", "function": {
        "name": "delete_section",
        "description": "Remove a `## ` section from the document (only when the author asks).",
        "parameters": {"type": "object", "properties": {"heading": {"type": "string"}}, "required": ["heading"]}}},
    {"type": "function", "function": {
        "name": "write_prose",
        "description": ("Have the creative writing model draft prose: read-aloud boxed text, location descriptions, "
                        "NPC dialogue samples, rumors, hook blurbs. Give a concrete brief and any canon facts it must "
                        "respect. Pass save_to_section when the author wants it in the document - it is then saved "
                        "for you. Otherwise the draft is returned for you to show or save with upsert_section."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["read_aloud", "description", "npc_voice", "rumors", "hook", "other"]},
            "brief": {"type": "string", "description": "What to write: subject, mood, length, what players should notice."},
            "canon_facts": {"type": "string", "description": "Verified setting facts it must not contradict."},
            "save_to_section": {"type": "string", "description": (
                "If the author asked you to save/draft this into the document, give the `## ` heading "
                "(e.g. 'Read-Aloud'); the draft is then saved there automatically.")},
            "mode": {"type": "string", "enum": ["replace", "append"], "description": "For save_to_section; default append."}},
            "required": ["kind", "brief"]}}},
]

WRITE_WORDS = re.compile(r"\b(INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|COPY|MOVE|ADD)\b", re.IGNORECASE)
_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')


def _canon_body(canon_context: str) -> str:
    """The retrieved-records block without its instruction preamble."""
    return canon_context.split("\n\n", 2)[-1] if canon_context else ""


def _prose(kind: str, brief: str, canon_facts: str, doc: str) -> str:
    premise = (get_section(doc, "Premise") or "").strip()
    tone = (get_section(doc, "Themes & Tone") or "").strip()
    system = ("You are a fiction writer for tabletop role-playing adventures. Write exactly what is asked, "
              "ready to use at the table: vivid but economical, concrete sensory detail, no filler or meta-talk. "
              "Facts listed under CANON must not be contradicted, and you must not add setting details about "
              "existing places or people that CANON does not give (no invented furnishings, smells, histories or "
              "NPC traits for canon places); new adventure content you may invent only where the brief asks. Read-aloud text is second person, "
              "present tense, and never reveals secrets the players could not perceive. Output only the prose.")
    context_lines = []
    if premise and premise != TODO:
        context_lines.append(f"ADVENTURE PREMISE:\n{premise[:1200]}")
    if tone and tone != TODO:
        context_lines.append(f"TONE:\n{tone[:600]}")
    if canon_facts.strip():
        context_lines.append(f"CANON (do not contradict):\n{canon_facts.strip()[:4500]}")
    user = "\n\n".join(context_lines + [f"WRITE ({kind}): {brief}"])
    resp = requests.post(LLM_ENDPOINT, json={
        "model": CREATIVE_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": CREATIVE_TEMPERATURE, "num_ctx": CREATIVE_NUM_CTX},
    }, timeout=300).json()
    return (resp.get("message", {}).get("content") or "").strip() or "(the creative model returned nothing)"


# record_* tool -> (document section, field labels). Recording to the graph also leaves a stub in
# the document, so the two never drift apart and the document stays complete.
DOC_STUBS = {
    "record_actor": ("NPCs", [("actor_kind", "Kind"), ("affiliation", "Affiliation"), ("located_in", "Located in"),
                              ("physical_appearance", "Appearance"), ("gm_description", "GM notes"), ("lore_context", "Background")]),
    "record_location": ("Locations", [("location_type", "Type"), ("parent", "Within"), ("physical_appearance", "Appearance"),
                                      ("gm_description", "GM notes"), ("lore_context", "Background")]),
    "record_item": ("Treasure", [("item_type", "Type"), ("located_in", "Found in"), ("owner", "Held by"),
                                 ("physical_appearance", "Appearance"), ("gm_description", "GM notes"), ("lore_context", "Background")]),
    "record_plot_hook": ("Hooks", [("giver", "Offered by"), ("involves_actor", "Involves"), ("involves_location", "Place"),
                                   ("involves_item", "Item"), ("description", "Summary")]),
}


def doc_stub(tool: str, args: dict) -> str:
    _, fields = DOC_STUBS[tool]
    lines = [f"### {args['name']}"]
    for key, label in fields:
        if args.get(key):
            lines.append(f"- **{label}:** {args[key]}")
    return "\n".join(lines)


class _Session:
    """Per-request state: the document being edited and what changed."""

    def __init__(self, slug: str, doc: str, canon: str = ""):
        self.slug, self.doc, self.changed, self.canon = slug, doc, [], canon
        self.prose_calls = 0
        self.agkg = ag.AdventureKG(kg, slug, _title(doc, slug))

    def run(self, name: str, args: dict) -> str:
        try:
            return self._run(name, args)
        except Exception as exc:
            return f"Error in {name}: {exc}"

    def _run(self, name: str, args: dict) -> str:
        if name == "search_entities":
            results = kg.search_entities(args["query"], args.get("rdf_type"), args.get("limit", 10), graph=self.agkg.graph)
            for r in results:
                r["source"] = "adventure (non-canon)" if ag.is_adventure_uri(r["uri"]) else "canon"
            return json.dumps(results)
        if name in ag.RECORDERS:
            self.changed.append(f"graph: {args.get('name', '?')}")
            result = ag.RECORDERS[name](self.agkg, args)
            section = DOC_STUBS[name][0]
            body = get_section(self.doc, section) or ""
            if not re.search(rf"^###\s+{re.escape(args['name'])}\s*$", body, re.IGNORECASE | re.MULTILINE):
                self.doc = upsert_section(self.doc, section, doc_stub(name, args), "append")
                _save(self.slug, self.doc)
                self.changed.append(section)
                result += f"\nAlso added a '{args['name']}' entry to the document's {section} section."
            return result
        if name == "remove_entity":
            self.changed.append(f"graph: removed {args.get('name', '?')}")
            return self.agkg.remove_entity(args["name"])
        if name == "execute_sparql":
            query = args["query"].strip()
            if WRITE_WORDS.search(_LITERAL.sub('""', query)):
                return "Rejected: the canon graph is read-only here. Save new material with upsert_section instead."
            if not re.search(r"PREFIX\s+rpg:", query, re.IGNORECASE):
                query = f"{PREFIX}\n{query}"
            return json.dumps(kg.execute_sparql(query))[:12000]
        if name == "read_adventure":
            heading = (args.get("heading") or "").strip()
            if not heading:
                return self.doc
            body = get_section(self.doc, heading)
            return body if body is not None else f"No section named '{heading}'."
        if name == "upsert_section":
            self.doc = upsert_section(self.doc, args["heading"], args["content"], args.get("mode", "replace"))
            _save(self.slug, self.doc)
            self.changed.append(args["heading"].strip())
            return f"Saved section '{args['heading'].strip()}' ({args.get('mode', 'replace')})."
        if name == "delete_section":
            self.doc = delete_section(self.doc, args["heading"])
            _save(self.slug, self.doc)
            self.changed.append(f"(deleted) {args['heading'].strip()}")
            return f"Deleted section '{args['heading'].strip()}'."
        if name == "write_prose":
            self.prose_calls += 1
            # Always give the writer the retrieved canon records too, so it works from
            # real facts even if the planning model forgot to pass them along.
            facts = (args.get("canon_facts", "") + "\n\n" + _canon_body(self.canon)).strip()
            text = _prose(args.get("kind", "other"), args["brief"], facts, self.doc)
            target = (args.get("save_to_section") or "").strip()
            if target and not text.startswith("(the creative model returned nothing)"):
                block = "\n".join(f"> {ln}" if args.get("kind") == "read_aloud" and ln.strip() and not ln.startswith(">")
                                  else ln for ln in text.splitlines())
                self.doc = upsert_section(self.doc, target, block, args.get("mode") or "append")
                _save(self.slug, self.doc)
                self.changed.append(target)
                return f"Saved section '{target}'. The saved draft:\n{text}"
            return text
        return f"Unknown tool: {name}"


def _system_prompt(game_system: str, doc: str, canon_context: str, graph: str = "") -> str:
    template = (PROMPTS_DIR / "author_prompt.md").read_text(encoding="utf-8")
    ontology = (PROMPTS_DIR / "ontology.ttl").read_text(encoding="utf-8")
    text = (template.replace("{{GAME_SYSTEM}}", game_system).replace("{{ONTOLOGY}}", ontology)
            .replace("{{ADVENTURE}}", doc_for_prompt(doc)).replace("{{GRAPH}}", graph))
    return text + ("\n\n" + canon_context if canon_context else "")


def _reply(request: "AuthorChat", message: dict, session: "_Session") -> dict:
    """Persist the conversation server-side so the author can return to it."""
    try:
        history = [{"role": m["role"], "content": m["content"]} for m in request.messages
                   if m.get("role") in ("user", "assistant")]
        history.append({"role": "assistant", "content": message.get("content") or "", "changed": session.changed})
        _save_chat(request.slug, history)
    except Exception as exc:
        print(f"author: could not save chat: {exc}")
    return {"message": message, "document": session.doc, "changed": session.changed}


CLAIMS_SAVED = re.compile(
    r"\b(saved|added|updated|stored|written|recorded|drafted and saved)\b[^.\n]{0,80}\b(section|document|adventure|outline|premise|npcs|"
    r"locations|encounters|read-aloud|hooks|background|notes)\b", re.IGNORECASE)


@router.post("/chat")
def author_chat(request: AuthorChat):
    doc = _load(request.slug)
    last_user = next((m["content"] for m in reversed(request.messages) if m.get("role") == "user"), "")
    graph = ag.graph_iri(request.slug)
    canon = context.retrieve_context(kg, last_user, graph=graph)
    session = _Session(request.slug, doc, canon)
    turns = [m for m in request.messages if m.get("role") in ("user", "assistant")][-HISTORY_MESSAGES:]
    while turns and turns[0].get("role") != "user":   # never start mid-exchange
        turns = turns[1:]
    messages = [{"role": "system", "content": _system_prompt(request.game_system, doc, canon, graph)}] + turns
    options = {"temperature": AUTHOR_TEMPERATURE, "num_ctx": OLLAMA_NUM_CTX}

    corrected = False
    for _ in range(MAX_TOOL_ROUNDS):
        resp = requests.post(LLM_ENDPOINT, json={"model": OLLAMA_MODEL, "messages": messages, "tools": AUTHOR_TOOLS,
                                                  "stream": False, "options": options}, timeout=600).json()
        message = resp.get("message", {})
        calls = message.get("tool_calls")
        print(f"author: tool_calls={[c['function']['name'] for c in calls] if calls else None} "
              f"content={(message.get('content') or '')[:120]!r}")
        if not calls:
            reply = message.get("content") or ""
            if CLAIMS_SAVED.search(reply) and not session.changed and not corrected:
                # The model said it saved something but never called upsert_section.
                corrected = True
                messages.append(message)
                messages.append({"role": "user", "content": (
                    "[automatic check] You said you saved content, but no save tool was called, so NOTHING was saved. "
                    "Call upsert_section now with the exact content, or if you did not mean to save, "
                    "reply again without claiming that you did.")})
                continue
            return _reply(request, message, session)
        messages.append(message)
        for call in calls:
            result = session.run(call["function"]["name"], call["function"].get("arguments") or {})
            messages.append({"role": "tool", "content": result, "name": call["function"]["name"]})

    messages.append({"role": "system", "content":
                     "Tool budget used up. Reply to the author now: summarise what you did and what you need from them."})
    final = requests.post(LLM_ENDPOINT, json={"model": OLLAMA_MODEL, "messages": messages, "stream": False,
                                              "options": options}, timeout=600).json()
    return _reply(request, final.get("message", {}), session)
