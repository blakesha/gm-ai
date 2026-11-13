import os
import re
import json
import requests
from pathlib import Path
import pymupdf
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional

import chunker
import extract_filter
import doctypes
import jobs
import mapper
import context
import adventure_graph
import author
import ingest_window
import hooks
import session_memory
import sessions
import stat_parser
from kg import KnowledgeGraphManager, PREFIX

LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://llm:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
# Ollama's default context window is only a few thousand tokens; with the
# extraction prompt + JSON schema + chunk, input was being silently truncated.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
# Cap on generated tokens per call: a constrained-JSON array can loop forever on
# a model that never closes it, hanging a chunk until the HTTP timeout.
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "3000"))
# Optional second model that only rewrites the grounded answer in a Game Master
# voice (e.g. an RPG/roleplay fine-tune). Empty = off: OLLAMA_MODEL answers directly.
NARRATION_MODEL = os.getenv("NARRATION_MODEL", "").strip()
NARRATION_TEMPERATURE = float(os.getenv("NARRATION_TEMPERATURE", "0.7"))
# auto = narrate only generative/in-fiction turns, skip plain factual lookups;
# always = narrate every reply; off = never
NARRATION_MODE = os.getenv("NARRATION_MODE", "auto").strip().lower()
NARRATION_NUM_CTX = int(os.getenv("NARRATION_NUM_CTX", "4096"))  # short prompt; small ctx keeps VRAM free
NARRATION_HISTORY_TURNS = 4
GM_HISTORY_MESSAGES = 16   # recent turns sent to the model; the full transcript is kept in the chat session
EXTRACT_FILTER = os.getenv("EXTRACT_FILTER", "grounding")     # none | grounding | llm | both (llm verifier hurt in evals)
FILTER_MODEL = os.getenv("OLLAMA_FILTER_MODEL") or None        # defaults to the extraction model
GLEAN = {x for x in os.getenv("EXTRACT_GLEAN", "").split(",") if x}   # follow-up pass; off by default (no gain once actor_kind/type hints were added)
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/library"))
JOBS_DB = os.getenv("JOBS_DB", "/data/jobs.db")
DEFAULT_GAME_SYSTEM = "AD&D 2nd Edition"

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"
EXTRACTION_DIR = PROMPTS_DIR / "extraction"

app = FastAPI(title="AI Game Master API")
app.include_router(author.router)
app.include_router(sessions.router)
app.include_router(session_memory.session_router)
app.include_router(session_memory.adventure_router)
kg_manager = KnowledgeGraphManager()


CONTEXT_PREAMBLE = (
    "\n\nThe user message may begin with DOCUMENT (what the whole book is), SECTION (where this passage sits "
    "in the book: chapter > heading > room) and CONTEXT lines. Use them only to disambiguate names, places and "
    "scenes; extract only what the PASSAGE itself states."
)


def _load_prompt(name: str) -> str:
    with open(EXTRACTION_DIR / f"{name}.md", "r") as f:
        return f.read() + CONTEXT_PREAMBLE


def _load_schema(name: str) -> dict:
    with open(EXTRACTION_DIR / f"{name}_schema.json", "r") as f:
        return json.load(f)


def get_agent_system_prompt(game_system: str, session_graph: str = "") -> str:
    with open(PROMPTS_DIR / "agent_prompt.md", "r") as f:
        template = f.read()
    with open(PROMPTS_DIR / "ontology.ttl", "r") as f:
        ontology = f.read()
    graph = session_graph or "(none - no session is active, so nothing can be recorded)"
    return (template.replace("{{GAME_SYSTEM}}", game_system).replace("{{ONTOLOGY}}", ontology)
            .replace("{{SESSION_GRAPH}}", graph))


def _context_note(location_hint: Optional[str], scene_hint: Optional[str],
                  section: Optional[str] = None, brief: Optional[str] = None,
                  type_hint: Optional[str] = None) -> str:
    lines = []
    if type_hint:
        lines.append(f"DOCUMENT TYPE: {type_hint}")
    if brief:
        lines.append(f"DOCUMENT: {brief}")
    if section:
        lines.append(f"SECTION: {section}")
    if not location_hint and not scene_hint:
        return ("\n".join(lines) + "\n\n") if lines else ""
    parts = []
    if location_hint:
        parts.append(f'current location = "{location_hint}"')
    if scene_hint:
        parts.append(f'current scene = "{scene_hint}"')
    lines.append(f"CONTEXT: {', '.join(parts)}")
    return "\n".join(lines) + "\n\n"


def call_ollama(system_prompt: str, schema: dict, chunk_text: str, context_note: str = "", model: Optional[str] = None, stats: Optional[dict] = None, raw_user: Optional[str] = None):
    payload = {
        "model": model or OLLAMA_MODEL,
        "format": schema,
        "think": False,
        "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX, "num_predict": OLLAMA_NUM_PREDICT},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_user or f'{context_note}PASSAGE:\n"""\n{chunk_text}\n"""'},
        ],
        "stream": False,
    }
    response = requests.post(LLM_ENDPOINT, json=payload, timeout=600).json()
    if "error" in response:
        raise RuntimeError(f"Ollama error: {response['error']}")
    if stats is not None:  # benchmark hook: accumulate Ollama timing/token counters
        for k in ("prompt_eval_count", "eval_count", "eval_duration", "total_duration"):
            stats[k] = stats.get(k, 0) + response.get(k, 0)
        stats["max_prompt_tokens"] = max(stats.get("max_prompt_tokens", 0), response.get("prompt_eval_count", 0))
    raw = response["message"]["content"].strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Hit the num_predict cap (runaway array) or malformed output: salvage
        # nothing rather than failing the whole chunk; callers treat None as empty.
        return None


def run_pass2_list(pass_name: str, chunk_text: str, context_note: str = "") -> list:
    """Passes whose schema is a JSON array of zero or more entries."""
    result = call_ollama(_load_prompt(pass_name), _load_schema(pass_name), chunk_text, context_note)
    return result if isinstance(result, list) else []


def run_pass2_obj(pass_name: str, chunk_text: str, context_note: str = "") -> dict:
    """Passes whose schema is a single JSON object - `scene`, since a chunk
    frames at most one encounter. (A prior version routed this through run_pass2_list, whose
    isinstance(list) check silently discarded every result - the schema was
    never a list.)"""
    result = call_ollama(_load_prompt(pass_name), _load_schema(pass_name), chunk_text, context_note)
    return result if isinstance(result, dict) else {}


GLEAN_NOTE = (
    "\n\nA first extraction pass already produced the entries listed under ALREADY FOUND. Return ONLY additional "
    "entries that appear in the PASSAGE but are missing from that list (same rules and format). Do not repeat "
    "anything already found, even under another spelling. Return an empty array if nothing is missing."
)

PASS_NAME_FIELD = {"actor": "actor_name", "item": "item_name", "location": "location_name"}


def glean_pass(pass_name: str, chunk_text: str, found_names: list, context_note: str = "",
               model: Optional[str] = None) -> list:
    """Follow-up pass: show the model what the first pass found and ask only
    for what it missed. Returns new entries, de-duplicated against `found_names`."""
    listing = "\n".join(f"- {n}" for n in found_names) or "(none)"
    user = f'{context_note}PASSAGE:\n"""\n{chunk_text}\n"""\n\nALREADY FOUND:\n{listing}'
    result = call_ollama(_load_prompt(pass_name) + GLEAN_NOTE, _load_schema(pass_name), "", "",
                         model=model, raw_user=user)
    field = PASS_NAME_FIELD[pass_name]
    known = {n.strip().lower() for n in found_names if n}
    out = []
    for entry in result if isinstance(result, list) else []:
        key = (entry.get(field) or "").strip().lower()
        if key and key not in known:
            known.add(key)
            out.append(entry)
    return out


def make_brief(doc, title: str) -> str:
    """~100-word description of the whole document (from its title, table of
    contents and opening pages), passed into every extraction call so a
    chunk is read knowing what book/adventure it belongs to."""
    toc = "; ".join(t for _, t, _ in doc.get_toc()[:40])
    opening = " ".join(doc[i].get_text() for i in range(min(4, len(doc))))[:5000]
    prompt = (f"Title: {title}\nTable of contents: {toc or '(none)'}\nOpening pages:\n{opening}\n\n"
              "In at most 90 words, say what this tabletop RPG document is (adventure, setting, rules, bestiary...), "
              "its game system, the main place/premise/factions, and how it is organised. No preamble.")
    resp = requests.post(LLM_ENDPOINT, json={
        "model": OLLAMA_MODEL, "stream": False, "think": False,
        "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
        "messages": [{"role": "user", "content": prompt}]}, timeout=900).json()
    return " ".join(resp.get("message", {}).get("content", "").split())[:700]


def _backfill(entries: list, field: str, value) -> None:
    if not value:
        return
    for entry in entries:
        if not entry.get(field):
            entry[field] = value


def process_chunk(
    chunk_text: str,
    document_type: str,
    game_system: str,
    location_hint: Optional[str] = None,
    scene_hint: Optional[str] = None,
    section: Optional[str] = None,
    brief: Optional[str] = None,
) -> dict:
    """Runs every Pass 2 extraction prompt on `chunk_text` unconditionally.
    An earlier Pass 1 classifier routed to only some of these per chunk, but
    NPCs/items/stats are routinely mentioned in passages a classifier doesn't
    tag as ACTOR_INTRO/ITEM/STAT_BLOCK, which silently dropped them - Pass 2
    prompts already return an empty result when nothing applies, so there's
    no recall cost to just always running them.

    `location_hint`/`scene_hint` carry the last location/scene named by an
    earlier chunk in the same document forward onto this one, since a room
    is rarely re-named in every paragraph that takes place inside it.
    """
    extracted = {
        "locations": [], "scenes": [], "readaloud": [], "triggers": [], "statblocks": [],
        "lore": [], "actors": [], "items": [], "session_events": [], "plot_hooks": [],
    }

    if document_type == "session_transcript":
        extracted["session_events"] = run_pass2_list("session_event", chunk_text)
        return extracted

    if document_type not in doctypes.PASSES:  # "auto" or unknown, e.g. manual text ingest
        document_type = "module"
    enabled = doctypes.PASSES[document_type]
    context_note = _context_note(location_hint, scene_hint, section, brief, doctypes.HINTS.get(document_type))

    location, scene = {}, {}
    if "location" in enabled:
        seen_names = set()
        for loc in run_pass2_list("location", chunk_text, context_note):
            key = (loc.get("location_name") or "").strip().lower()
            if key and key not in seen_names:  # drop repeats within a chunk
                seen_names.add(key)
                extracted["locations"].append(loc)
        location = extracted["locations"][0] if extracted["locations"] else {}

    if "scene" in enabled:
        scene = run_pass2_obj("scene", chunk_text, context_note)
        if scene.get("scene_name"):
            extracted["scenes"].append(scene)

    if "statblock" in enabled:
        parsed = stat_parser.parse(chunk_text, game_system)
        if parsed:
            extracted["statblocks"].append(parsed)
        else:
            extracted["statblocks"].extend(run_pass2_list("statblock", chunk_text))

    for key, name in [("readaloud", "readaloud"), ("triggers", "trigger"), ("lore", "lore"),
                      ("actors", "actor"), ("items", "item")]:
        if name in enabled:
            extracted[key] = run_pass2_list(name, chunk_text, context_note)
    if "plot_hook" in enabled:
        extracted["plot_hooks"] = run_pass2_list("plot_hook", chunk_text, context_note)

    for key, field in (("actors", "actor_name"), ("items", "item_name")):  # repeats within a chunk
        seen_names, unique = set(), []
        for e in extracted[key]:
            k = (e.get(field) or "").strip().lower()
            if k and k not in seen_names:
                seen_names.add(k)
                unique.append(e)
        extracted[key] = unique

    for key, pass_name in (("actors", "actor"), ("items", "item"), ("locations", "location")):
        if key in GLEAN and pass_name in enabled:
            field = PASS_NAME_FIELD[pass_name]
            extracted[key].extend(glean_pass(
                pass_name, chunk_text, [e.get(field) for e in extracted[key]], context_note))

    mode = EXTRACT_FILTER
    if mode in ("grounding", "both"):
        extract_filter.apply_grounding(chunk_text, section, extracted)
    if mode in ("llm", "both"):
        extract_filter.apply_llm_verify(
            chunk_text, extracted,
            lambda sp, sc, ut: call_ollama(sp, sc, "", "", model=FILTER_MODEL or OLLAMA_MODEL, raw_user=ut),
        )

    effective_location = (extracted["locations"][0].get("location_name") if extracted["locations"] else None) or location_hint
    effective_scene = scene.get("scene_name") or scene_hint
    _backfill(extracted["actors"], "located_in_hint", effective_location)
    _backfill(extracted["items"], "located_in_hint", effective_location)
    _backfill(extracted["triggers"], "located_in_hint", effective_location)
    _backfill(extracted["readaloud"], "located_in_hint", effective_location)
    _backfill(extracted["actors"], "scene_context", effective_scene)
    _backfill(extracted["items"], "scene_context", effective_scene)
    _backfill(extracted["triggers"], "scene_context", effective_scene)
    _backfill(extracted["readaloud"], "scene_context", effective_scene)
    _backfill(extracted["lore"], "scene_context", effective_scene)

    return extracted


def insert_extraction(extracted: dict, game_system: str, source: Optional[dict] = None) -> str:
    triples: List[str] = []
    tkg = mapper.TrackingKG(kg_manager)
    mapper.map_locations(extracted["locations"], tkg, triples)
    mapper.map_scenes(extracted["scenes"], tkg, triples)
    if extracted["locations"] and extracted["scenes"]:
        mapper.map_hosts_scene(extracted["locations"][-1], extracted["scenes"][-1], tkg, triples)
    mapper.map_readaloud(extracted["readaloud"], tkg, triples)
    mapper.map_triggers(extracted["triggers"], tkg, triples)
    mapper.map_statblocks(extracted["statblocks"], tkg, triples, game_system)
    mapper.map_lore(extracted["lore"], tkg, triples)
    mapper.map_actors(extracted["actors"], tkg, triples)
    mapper.map_items(extracted["items"], tkg, triples)
    mapper.map_session_events(extracted["session_events"], tkg, triples)
    mapper.map_plot_hooks(extracted["plot_hooks"], tkg, triples)

    mapper.map_provenance(tkg, source, triples)

    if not triples:
        return "No new entities found."

    update_query = f"{PREFIX}\nINSERT DATA {{\n  " + "\n  ".join(triples) + "\n}"
    try:
        kg_manager.execute_sparql(update_query, is_update=True)
    except Exception as e:
        raise ValueError(f"{str(e)}\n\nFAILED QUERY:\n{update_query}")
    return f"Inserted {len(triples)} triples."


class IngestRequest(BaseModel):
    chunk_text: str
    document_type: str = "module"
    game_system: str = DEFAULT_GAME_SYSTEM
    location_hint: Optional[str] = None
    scene_hint: Optional[str] = None


@app.post("/ingest")
def ingest_text(request: IngestRequest):
    try:
        extracted = process_chunk(
            request.chunk_text, request.document_type, request.game_system,
            request.location_hint, request.scene_hint,
        )
        details = insert_extraction(extracted, request.game_system)
        return {"status": "success", "details": details}
    except Exception as e:
        return {"status": "error", "details": str(e)}


@app.post("/ingest_pdf")
async def ingest_pdf(
    file: UploadFile = File(...),
    document_type: str = Form("module"),
    game_system: str = Form(DEFAULT_GAME_SYSTEM),
):
    pdf_bytes = await file.read()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    results = []
    current_location, current_scene = None, None
    for i, chunk in enumerate(chunker.chunk_document(doc)):
        try:
            extracted = process_chunk(chunk["text"], document_type, game_system, current_location, current_scene,
                                      section=chunk.get("section"))
            if extracted["locations"]:
                current_location = extracted["locations"][-1].get("location_name") or current_location
            if extracted["scenes"]:
                current_scene = extracted["scenes"][-1].get("scene_name") or current_scene
            details = insert_extraction(extracted, game_system, source={
                "title": Path(file.filename).stem, "page": chunk.get("page"), "section": chunk.get("section")})
            results.append({"chunk_index": i, "status": "success", "details": details})
        except Exception as e:
            results.append({"chunk_index": i, "status": "error", "details": str(e)})
            break

    return {"status": "success", "chunks_processed": len(results), "results": results}


# ---------------------------------------------------------------------------
# Library browsing + background ingestion
# ---------------------------------------------------------------------------
job_store = jobs.JobStore(JOBS_DB)
window = ingest_window.IngestWindow(job_store)
app.include_router(ingest_window.make_router(window))


def _resolve_library_path(rel: str) -> Path:
    """Map a library-relative path to an absolute one, refusing escapes."""
    root = LIBRARY_DIR.resolve()
    target = (root / rel.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="Path escapes the library")
    return target


def process_job(job: dict, store: jobs.JobStore) -> None:
    doc = pymupdf.open(job["path"])
    doc_type = job["document_type"]
    if doc_type == "auto":
        doc_type = job.get("detected_type") or doctypes.detect(
            job["path"], " ".join(chunker.page_text(doc[i]) for i in range(min(3, len(doc)))))
        store.update(job["id"], detected_type=doc_type)
    hints = json.loads(job["hints"] or "{}")
    current_location, current_scene = hints.get("location"), hints.get("scene")
    brief = hints.get("brief")
    if brief is None:
        try:
            brief = make_brief(doc, Path(job["path"]).stem)
        except Exception:
            brief = ""
    skip = job["chunks_done"]
    errors = job["chunk_errors"]
    chunks = list(chunker.chunk_document(doc))
    store.update(job["id"], chunks_total=len(chunks))
    if not chunks:
        raise RuntimeError(
            f"No text extracted from {len(doc)}-page PDF (scanned/image-only? needs OCR) - nothing ingested")

    for i, chunk in enumerate(chunks):
        if i < skip:
            continue
        if store.get(job["id"])["status"] == "cancelled":
            return
        if not window.is_open():
            # Window closed: park the job. Its checkpoint (chunks_done + hints) is already saved,
            # so it resumes from this chunk when the window reopens.
            store.update(job["id"], status="queued", last_error=None)
            return
        try:
            extracted = process_chunk(
                chunk["text"], doc_type, job["game_system"], current_location, current_scene,
                section=chunk.get("section"), brief=brief,
            )
            if extracted["locations"]:
                current_location = extracted["locations"][-1].get("location_name") or current_location
            if extracted["scenes"]:
                current_scene = extracted["scenes"][-1].get("scene_name") or current_scene
            insert_extraction(extracted, job["game_system"], source={
                "title": Path(job["path"]).stem, "page": chunk.get("page"), "section": chunk.get("section")})
        except Exception as e:
            errors += 1
            store.update(job["id"], last_error=f"chunk {i}: {e}"[:2000])
        store.update(
            job["id"], chunks_done=i + 1, chunk_errors=errors,
            hints=json.dumps({"location": current_location, "scene": current_scene, "brief": brief}),
        )


@app.on_event("startup")
def _start_worker():
    jobs.Worker(job_store, process_job, gate=window.is_open).start()


@app.get("/document_types")
def document_types():
    return {"types": doctypes.TYPES}


@app.get("/library")
def browse_library(path: str = ""):
    target = _resolve_library_path(path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Not a directory")
    entries = []
    for p in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            entries.append({"name": p.name, "type": "dir"})
        elif p.suffix.lower() == ".pdf":
            entries.append({"name": p.name, "type": "pdf", "size": p.stat().st_size})
    return {"path": str(target.relative_to(LIBRARY_DIR.resolve())), "entries": entries}


@app.post("/library/upload")
async def upload_to_library(file: UploadFile = File(...), directory: str = Form("")):
    target_dir = _resolve_library_path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename).name
    dest = target_dir / name
    with open(dest, "wb") as f:
        while block := await file.read(1024 * 1024):
            f.write(block)
    return {"status": "success", "path": str(dest.relative_to(LIBRARY_DIR.resolve()))}


class QueueRequest(BaseModel):
    paths: List[str]  # library-relative files and/or folders
    document_type: str = "module"
    game_system: str = DEFAULT_GAME_SYSTEM
    recursive: bool = True
    force: bool = False  # re-process files already ingested unchanged


@app.post("/jobs")
def queue_jobs(request: QueueRequest):
    pdfs: List[Path] = []
    for rel in request.paths:
        target = _resolve_library_path(rel)
        if target.is_dir():
            found = target.rglob("*.pdf") if request.recursive else target.glob("*.pdf")
            pdfs.extend(sorted(p for p in found if not p.name.startswith(".")))
        elif target.suffix.lower() == ".pdf" and target.is_file():
            pdfs.append(target)
    queued, skipped = [], 0
    for pdf in pdfs:
        job_id = job_store.enqueue(str(pdf), request.document_type, request.game_system, request.force)
        if job_id:
            queued.append(job_id)
        else:
            skipped += 1
    return {"queued": len(queued), "skipped": skipped, "job_ids": queued}


@app.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 200):
    rows = job_store.list(status, limit)
    root = str(LIBRARY_DIR.resolve())
    for r in rows:
        r["path"] = r["path"].removeprefix(root).lstrip("/")
        r.pop("hints", None)
    return {"jobs": rows}


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    return {"cancelled": job_store.cancel(job_id)}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: int):
    return {"requeued": job_store.retry(job_id)}


@app.post("/jobs/cancel_all")
def cancel_all():
    n = sum(job_store.cancel(j["id"]) for j in job_store.list() if j["status"] in ("queued", "running"))
    return {"cancelled": n}


class ChatRequest(BaseModel):
    messages: List[Dict[str, str]]
    game_system: str
    session_id: Optional[str] = None  # when set, the exchange is recorded in that chat session
    narrate: Optional[bool] = None  # None = auto (narrate scenes, skip factual lookups)


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": (
                "Fuzzy-search the Knowledge Graph for entities by name (matches "
                "rpg:name and rpg:alias). Returns each match's real `uri`, `type`, "
                "`name`, and a relevance `score`. ALWAYS call this to resolve a "
                "name to a URI before referencing that entity in execute_sparql - "
                "never guess or invent a URI."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name or partial name to search for."},
                    "rdf_type": {"type": "string", "description": "Optional class to restrict the search to, e.g. 'Location' or 'Actor'."},
                    "limit": {"type": "integer", "description": "Max results to return (default 10)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sparql",
            "description": "Execute SPARQL 1.1. Only reference entities by URIs already confirmed via search_entities.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_plot_hooks",
            "description": ("List plot hooks that are still open in THIS session (not yet resolved here). "
                            "Hooks are reusable across sessions; use this instead of querying PlotHook yourself."),
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_plot_hook",
            "description": ("Mark a plot hook as played out in THIS session only. Call it once a hook has been "
                            "introduced and resolved. Never write rpg:isResolved on a hook."),
            "parameters": {
                "type": "object",
                "properties": {"hook_uri": {"type": "string", "description": "uri from open_plot_hooks"},
                               "outcome": {"type": "string", "description": "One sentence on how it played out."}},
                "required": ["hook_uri"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_event",
            "description": ("Log something significant that happened in play (a fight, a discovery, a promise, a death) "
                            "in this session's memory. The player's own actions are logged automatically; use this for outcomes."),
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "description": "Short title of the event."},
                "description": {"type": "string"}, "outcome": {"type": "string"},
                "location": {"type": "string", "description": "Name of the place it happened."},
                "actors": {"type": "array", "items": {"type": "string"}, "description": "Names of those involved."}},
                "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_state",
            "description": ("Change the world in THIS session only (canon is never modified): e.g. status 'dead', "
                            "locatedIn another place, owns an item. Use uris from search_entities."),
            "parameters": {"type": "object", "properties": {
                "entity_uri": {"type": "string"}, "predicate": {"type": "string", "description": "An ontology property, e.g. status, locatedIn, owns."},
                "value": {"type": "string", "description": "A literal, or the uri of another entity."}},
                "required": ["entity_uri", "predicate", "value"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "entity_state",
            "description": "Current facts about one entity: canon plus this session's changes (session wins). Use before narrating state that may have changed.",
            "parameters": {"type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"]},
        },
    },
]

AGENT_TOOLS += [
    json.loads(json.dumps(t).replace("THIS adventure's graph memory", "this session's memory")
               .replace("THIS adventure", "this session").replace("adventure", "session"))
    for t in author.AUTHOR_TOOLS if t["function"]["name"] in adventure_graph.RECORDERS
]

MAX_TOOL_ROUNDS = 5


def _run_tool(name: str, arguments: dict, session_id: Optional[str] = None) -> str:
    if name == "open_plot_hooks":
        try:
            return json.dumps(hooks.open_hooks(kg_manager, session_id, int(arguments.get("limit") or 10)))
        except Exception as e:
            return f"Hook Error: {str(e)}"
    if name == "resolve_plot_hook":
        try:
            return hooks.resolve_hook(kg_manager, session_id, arguments["hook_uri"], arguments.get("outcome", ""))
        except Exception as e:
            return f"Hook Error: {str(e)}"
    if name in ("record_event", "set_state", "entity_state") or name in adventure_graph.RECORDERS:
        if not session_id:
            return "No session is active, so nothing can be recorded."
        try:
            skg = session_memory.SessionKG(kg_manager, session_id, sessions.title_of(session_id))
            if name == "entity_state":
                return session_memory.entity_state(skg, arguments.get("uri", ""))
            fn = {"record_event": session_memory.record_event, "set_state": session_memory.set_state}.get(name) \
                or adventure_graph.RECORDERS[name]
            return fn(skg, arguments)
        except Exception as e:
            return f"Memory Error: {str(e)[:500]}"
    if name == "search_entities":
        try:
            results = kg_manager.search_entities(
                arguments["query"], arguments.get("rdf_type"), arguments.get("limit", 10),
                graph=hooks.session_graph(session_id) if session_id else None
            )
            return json.dumps(results)
        except Exception as e:
            return f"Search Error: {str(e)}"
    if name == "execute_sparql":
        query = arguments["query"].strip()
        # Models often forget or mangle the prefix declaration; add it if absent.
        if not re.search(r"PREFIX\s+rpg:", query, re.IGNORECASE):
            query = f"{PREFIX}\n{query}"
        # Ignore keywords that only appear inside string literals.
        bare = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', '""', query)
        is_update = bool(re.search(r"\b(INSERT|DELETE|CLEAR|DROP|LOAD|CREATE|MOVE|COPY|ADD)\b", bare, re.IGNORECASE))
        if is_update:
            return ("Rejected: SPARQL updates are not allowed during play, so canon stays untouched. Record changes with "
                    "set_state, record_event, or the record_location/record_actor/record_item/record_plot_hook tools; "
                    "they are saved to this session's memory.")
        try:
            return json.dumps(kg_manager.execute_sparql(query, is_update=is_update))
        except Exception as e:
            return (f"SPARQL Error: {str(e)[:1500]}\n"
                    f"Query sent:\n{query}\n"
                    "Fix the syntax (balanced braces, trailing dots, quoted literals) and retry.")
    return f"Unknown tool: {name}"


def _narrate(request: "ChatRequest", last_user: str, grounded: str) -> Optional[str]:
    """Rewrite the grounded answer in a GM voice with NARRATION_MODEL.
    Returns None on any failure so the caller falls back to the plain answer."""
    with open(PROMPTS_DIR / "narration_prompt.md", "r") as f:
        system = f.read().replace("{{GAME_SYSTEM}}", request.game_system)
    history = [m for m in request.messages if m.get("role") in ("user", "assistant")][-NARRATION_HISTORY_TURNS:]
    # the current user turn is passed separately, with the verified answer attached
    if history and history[-1].get("role") == "user":
        history = history[:-1]
    user_turn = (f"{last_user}\n\n---\nVERIFIED ANSWER (the only facts you may use):\n{grounded}\n---\n"
                 "Deliver this answer to the player as the Game Master.")
    resp = requests.post(LLM_ENDPOINT, json={
        "model": NARRATION_MODEL,
        "messages": [{"role": "system", "content": system}] + history + [{"role": "user", "content": user_turn}],
        "stream": False,
        "options": {"temperature": NARRATION_TEMPERATURE, "num_ctx": NARRATION_NUM_CTX},
    }, timeout=300).json()
    text = (resp.get("message", {}).get("content") or "").strip()
    return text or None


SCENE_ACTION = re.compile(
    r"^\s*[\"'“*(]?\s*(i|we|my|our|let's|lets)\b|"
    r"\b(narrate|roleplay|role-play|what happens|describe (the|what|how) .*(see|enter|look|find|feel)|"
    r"random event|improvis|continue the (scene|story)|start (the|a) (scene|session|adventure))", re.IGNORECASE)

CLASSIFY_PROMPT = (
    "Classify the player's latest message to a tabletop Game Master as exactly one word.\n"
    "FACT = a lookup question about the setting, lore, people, places, factions, items, stats or rules "
    "(\"Who leads the Harpers?\", \"What do you know about the High Forest?\", \"Where is Shadowdale?\", "
    "\"What are an orc's hit dice?\").\n"
    "SCENE = the player acts, speaks in character, or wants story: declares an action (\"I open the door\"), "
    "talks to an NPC, asks to narrate/describe a scene as they experience it, asks what happens next, "
    "or wants new events or improvisation.\n"
    "A message where the player's CHARACTER does or says something in the fiction is SCENE even if it "
    "ends up seeking information (\"I ask the innkeeper about rumors\", \"I search the room\", "
    "\"We question the guard\"). A message that steps outside the fiction to ask about the world is FACT.\n"
    "Answer with FACT or SCENE only.")


def _wants_narration(request: "ChatRequest", last_user: str) -> bool:
    """Narrate generative/in-fiction turns; skip plain factual lookups."""
    if request.narrate is not None:          # explicit per-request override
        return request.narrate
    if NARRATION_MODE == "always":
        return True
    if NARRATION_MODE == "off":
        return False
    try:
        prior = next((m["content"] for m in reversed(request.messages[:-1]) if m.get("role") == "assistant"), "")
        user = (f"Previous GM reply (for context): {prior[:300]}\n\n" if prior else "") + f"Player message: {last_user}"
        resp = requests.post(LLM_ENDPOINT, json={
            "model": OLLAMA_MODEL, "stream": False,
            "messages": [{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": user}],
            "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 4},
        }, timeout=60).json()
        verdict = (resp.get("message", {}).get("content") or "").strip().upper()
        if verdict.startswith("SCENE"):
            return True
        if verdict.startswith("FACT"):
            return False
    except Exception as exc:
        print(f"narration classifier failed, using heuristic: {exc}")
    return bool(SCENE_ACTION.search(last_user))


def _finish(message: dict, request: "ChatRequest", last_user: str) -> dict:
    result = _finish_core(message, request, last_user)
    sessions.record_turn(request.session_id, last_user, result)
    if request.session_id:   # keep a log of the player's actions in the session's graph memory
        try:
            turn = sum(1 for m in sessions._read(request.session_id)["messages"] if m.get("role") == "user")
            session_memory.log_action(request.session_id, last_user, turn)
        except Exception as exc:
            print(f"session memory: could not log turn: {exc}")
    return result


def _finish_core(message: dict, request: "ChatRequest", last_user: str) -> dict:
    """Return the chat reply, narrated if a narration model is configured."""
    grounded = (message.get("content") or "").strip()
    if not NARRATION_MODEL or not grounded:
        return {"message": message}
    narrate = _wants_narration(request, last_user)
    print(f"narration: {'yes' if narrate else 'skipped (factual)'}")
    if not narrate:
        return {"message": message, "narrated_by": None}
    try:
        narrated = _narrate(request, last_user, grounded)
    except Exception as exc:  # narration is cosmetic; never lose the answer
        print(f"narration failed, returning grounded answer: {exc}")
        narrated = None
    if not narrated:
        return {"message": message}
    return {"message": {**message, "content": narrated}, "grounded": grounded, "narrated_by": NARRATION_MODEL}


@app.post("/chat")
def agent_chat(request: ChatRequest):
    session_graph = hooks.session_graph(request.session_id) if request.session_id else ""
    system_prompt = get_agent_system_prompt(request.game_system, session_graph)
    last_user = next((m["content"] for m in reversed(request.messages) if m.get("role") == "user"), "")
    graph_context = context.retrieve_context(kg_manager, last_user, graph=session_graph or None)
    if graph_context:
        system_prompt += "\n\n" + graph_context
    recent = [m for m in request.messages if m.get("role") in ("user", "assistant")][-GM_HISTORY_MESSAGES:]
    while recent and recent[0].get("role") != "user":
        recent = recent[1:]
    messages = [{"role": "system", "content": system_prompt}] + recent

    for _ in range(MAX_TOOL_ROUNDS):
        payload = {"model": OLLAMA_MODEL, "messages": messages, "tools": AGENT_TOOLS, "stream": False,
                   "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX}}
        resp = requests.post(LLM_ENDPOINT, json=payload).json()
        message = resp.get("message", {})
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return _finish(message, request, last_user)

        messages.append(message)
        for tool in tool_calls:
            tool_response = _run_tool(tool["function"]["name"], tool["function"]["arguments"], request.session_id)
            messages.append({"role": "tool", "content": tool_response, "name": tool["function"]["name"]})

    # Ran out of tool-call rounds - ask once more without tools so the model
    # is forced to answer from what it already gathered instead of looping forever.
    messages.append({"role": "system", "content": (
        "Tool budget used up. Answer now using ONLY the tool results above. State what the knowledge graph "
        "contained and what it did not; do not add facts from your own memory.")})
    final_resp = requests.post(LLM_ENDPOINT, json={"model": OLLAMA_MODEL, "messages": messages, "stream": False,
                                                    "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX}}).json()
    return _finish(final_resp.get("message", {}), request, last_user)
