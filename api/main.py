import os
import re
import json
import requests
from pathlib import Path
import pymupdf
from fastapi import FastAPI, UploadFile, File, Form
from pydantic import BaseModel
from typing import List, Dict, Optional

import chunker
import mapper
import stat_parser
from kg import KnowledgeGraphManager, PREFIX

LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://llm:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
DEFAULT_GAME_SYSTEM = "AD&D 2nd Edition"

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"
V2_DIR = PROMPTS_DIR / "extraction_v2"

app = FastAPI(title="AI Game Master API")
kg_manager = KnowledgeGraphManager()


def _load_prompt(name: str) -> str:
    with open(V2_DIR / f"{name}.md", "r") as f:
        return f.read()


def _load_schema(name: str) -> dict:
    with open(V2_DIR / f"{name}_schema.json", "r") as f:
        return json.load(f)


def get_agent_system_prompt(game_system: str) -> str:
    with open(PROMPTS_DIR / "agent_prompt.md", "r") as f:
        template = f.read()
    with open(PROMPTS_DIR / "ontology.ttl", "r") as f:
        ontology = f.read()
    return template.replace("{{GAME_SYSTEM}}", game_system).replace("{{ONTOLOGY}}", ontology)


def _context_note(location_hint: Optional[str], scene_hint: Optional[str]) -> str:
    if not location_hint and not scene_hint:
        return ""
    parts = []
    if location_hint:
        parts.append(f'current location = "{location_hint}"')
    if scene_hint:
        parts.append(f'current scene = "{scene_hint}"')
    return f"CONTEXT: {', '.join(parts)}\n\n"


def call_ollama(system_prompt: str, schema: dict, chunk_text: str, context_note: str = ""):
    payload = {
        "model": OLLAMA_MODEL,
        "format": schema,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f'{context_note}PASSAGE:\n"""\n{chunk_text}\n"""'},
        ],
        "stream": False,
    }
    response = requests.post(LLM_ENDPOINT, json=payload).json()
    if "error" in response:
        raise RuntimeError(f"Ollama error: {response['error']}")
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
    """Passes whose schema is a single JSON object - currently just `scene`,
    since a chunk describes at most one location. (A prior version routed
    this through run_pass2_list, whose isinstance(list) check silently
    discarded every scene result - the schema was never a list.)"""
    result = call_ollama(_load_prompt(pass_name), _load_schema(pass_name), chunk_text, context_note)
    return result if isinstance(result, dict) else {}


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
        "scenes": [], "readaloud": [], "triggers": [], "statblocks": [],
        "lore": [], "actors": [], "items": [], "session_events": [], "plot_hooks": [],
    }

    if document_type == "session_transcript":
        extracted["session_events"] = run_pass2_list("session_event", chunk_text)
        return extracted

    context_note = _context_note(location_hint, scene_hint)

    scene = run_pass2_obj("scene", chunk_text, context_note)
    if scene.get("location_name"):
        extracted["scenes"].append(scene)

    parsed = stat_parser.parse(chunk_text, game_system)
    if parsed:
        extracted["statblocks"].append(parsed)
    else:
        extracted["statblocks"].extend(run_pass2_list("statblock", chunk_text))

    extracted["readaloud"] = run_pass2_list("readaloud", chunk_text, context_note)
    extracted["triggers"] = run_pass2_list("trigger", chunk_text, context_note)
    extracted["lore"] = run_pass2_list("lore", chunk_text)
    extracted["actors"] = run_pass2_list("actor", chunk_text, context_note)
    extracted["items"] = run_pass2_list("item", chunk_text, context_note)
    extracted["plot_hooks"] = run_pass2_list("plot_hook", chunk_text)

    effective_location = scene.get("location_name") or location_hint
    effective_scene = scene.get("scene_name") or scene_hint
    _backfill(extracted["actors"], "located_in_hint", effective_location)
    _backfill(extracted["items"], "located_in_hint", effective_location)
    _backfill(extracted["triggers"], "located_in_hint", effective_location)
    _backfill(extracted["readaloud"], "located_in_hint", effective_location)
    _backfill(extracted["actors"], "scene_context", effective_scene)
    _backfill(extracted["items"], "scene_context", effective_scene)
    _backfill(extracted["triggers"], "scene_context", effective_scene)
    _backfill(extracted["readaloud"], "scene_context", effective_scene)

    return extracted


def insert_extraction(extracted: dict, game_system: str) -> str:
    triples: List[str] = []
    mapper.map_scenes(extracted["scenes"], kg_manager, triples)
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
            if extracted["scenes"]:
                latest_scene = extracted["scenes"][-1]
                current_location = latest_scene.get("location_name") or current_location
                current_scene = latest_scene.get("scene_name") or current_scene
            details = insert_extraction(extracted, game_system)
            results.append({"chunk_index": i, "status": "success", "details": details})
        except Exception as e:
            results.append({"chunk_index": i, "status": "error", "details": str(e)})
            break

    return {"status": "success", "chunks_processed": len(results), "results": results}


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
