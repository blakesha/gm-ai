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
import stat_parser
from kg import KnowledgeGraphManager, PREFIX

LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://llm:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
# Ollama's default context window is only a few thousand tokens; with the
# extraction prompt + JSON schema + chunk, input was being silently truncated.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
EXTRACT_FILTER = os.getenv("EXTRACT_FILTER", "grounding")     # none | grounding | llm | both (llm verifier hurt in evals)
FILTER_MODEL = os.getenv("OLLAMA_FILTER_MODEL") or None        # defaults to the extraction model
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/library"))
JOBS_DB = os.getenv("JOBS_DB", "/data/jobs.db")
DEFAULT_GAME_SYSTEM = "AD&D 2nd Edition"

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"
EXTRACTION_DIR = PROMPTS_DIR / "extraction"

app = FastAPI(title="AI Game Master API")
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


def get_agent_system_prompt(game_system: str) -> str:
    with open(PROMPTS_DIR / "agent_prompt.md", "r") as f:
        template = f.read()
    with open(PROMPTS_DIR / "ontology.ttl", "r") as f:
        ontology = f.read()
    return template.replace("{{GAME_SYSTEM}}", game_system).replace("{{ONTOLOGY}}", ontology)


def _context_note(location_hint: Optional[str], scene_hint: Optional[str],
                  section: Optional[str] = None, brief: Optional[str] = None) -> str:
    lines = []
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
        "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_user or f'{context_note}PASSAGE:\n"""\n{chunk_text}\n"""'},
        ],
        "stream": False,
    }
    response = requests.post(LLM_ENDPOINT, json=payload, timeout=900).json()
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
    return json.loads(raw)


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
    context_note = _context_note(location_hint, scene_hint, section, brief)

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


def insert_extraction(extracted: dict, game_system: str) -> str:
    triples: List[str] = []
    mapper.map_locations(extracted["locations"], kg_manager, triples)
    mapper.map_scenes(extracted["scenes"], kg_manager, triples)
    if extracted["locations"] and extracted["scenes"]:
        mapper.map_hosts_scene(extracted["locations"][-1], extracted["scenes"][-1], kg_manager, triples)
    mapper.map_readaloud(extracted["readaloud"], kg_manager, triples)
    mapper.map_triggers(extracted["triggers"], kg_manager, triples)
    mapper.map_statblocks(extracted["statblocks"], kg_manager, triples, game_system)
    mapper.map_lore(extracted["lore"], kg_manager, triples)
    mapper.map_actors(extracted["actors"], kg_manager, triples)
    mapper.map_items(extracted["items"], kg_manager, triples)
    mapper.map_session_events(extracted["session_events"], kg_manager, triples)
    mapper.map_plot_hooks(extracted["plot_hooks"], kg_manager, triples)

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
            extracted = process_chunk(chunk["text"], document_type, game_system, current_location, current_scene)
            if extracted["locations"]:
                current_location = extracted["locations"][-1].get("location_name") or current_location
            if extracted["scenes"]:
                current_scene = extracted["scenes"][-1].get("scene_name") or current_scene
            details = insert_extraction(extracted, game_system)
            results.append({"chunk_index": i, "status": "success", "details": details})
        except Exception as e:
            results.append({"chunk_index": i, "status": "error", "details": str(e)})
            break

    return {"status": "success", "chunks_processed": len(results), "results": results}


# ---------------------------------------------------------------------------
# Library browsing + background ingestion
# ---------------------------------------------------------------------------
job_store = jobs.JobStore(JOBS_DB)


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
            job["path"], " ".join(doc[i].get_text() for i in range(min(3, len(doc)))))
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

    for i, chunk in enumerate(chunks):
        if i < skip:
            continue
        if store.get(job["id"])["status"] == "cancelled":
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
            insert_extraction(extracted, job["game_system"])
        except Exception as e:
            errors += 1
            store.update(job["id"], last_error=f"chunk {i}: {e}"[:2000])
        store.update(
            job["id"], chunks_done=i + 1, chunk_errors=errors,
            hints=json.dumps({"location": current_location, "scene": current_scene, "brief": brief}),
        )


@app.on_event("startup")
def _start_worker():
    jobs.Worker(job_store, process_job).start()


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
]

MAX_TOOL_ROUNDS = 5


def _run_tool(name: str, arguments: dict) -> str:
    if name == "search_entities":
        try:
            results = kg_manager.search_entities(
                arguments["query"], arguments.get("rdf_type"), arguments.get("limit", 10)
            )
            return json.dumps(results)
        except Exception as e:
            return f"Search Error: {str(e)}"
    if name == "execute_sparql":
        query = arguments["query"]
        is_update = "INSERT" in query.upper() or "DELETE" in query.upper()
        try:
            return json.dumps(kg_manager.execute_sparql(query, is_update=is_update))
        except Exception as e:
            return f"SPARQL Error: {str(e)}"
    return f"Unknown tool: {name}"


@app.post("/chat")
def agent_chat(request: ChatRequest):
    messages = [{"role": "system", "content": get_agent_system_prompt(request.game_system)}] + request.messages

    for _ in range(MAX_TOOL_ROUNDS):
        payload = {"model": OLLAMA_MODEL, "messages": messages, "tools": AGENT_TOOLS, "stream": False}
        resp = requests.post(LLM_ENDPOINT, json=payload).json()
        message = resp.get("message", {})
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return {"message": message}

        messages.append(message)
        for tool in tool_calls:
            tool_response = _run_tool(tool["function"]["name"], tool["function"]["arguments"])
            messages.append({"role": "tool", "content": tool_response, "name": tool["function"]["name"]})

    # Ran out of tool-call rounds - ask once more without tools so the model
    # is forced to answer from what it already gathered instead of looping forever.
    final_resp = requests.post(LLM_ENDPOINT, json={"model": OLLAMA_MODEL, "messages": messages, "stream": False}).json()
    return {"message": final_resp.get("message", {})}
