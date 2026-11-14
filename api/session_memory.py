"""Graph memory for a play session, and promotion of that memory into canon.

Everything that happens while a player plays is written to the session's own
named graph (urn:rpg:session:<id>), never to canon:

  * new entities the GM invents (non-canon: `rpg:isCanon false`, minted with the
    session's URI prefix, `rpg:foundIn` the session node);
  * world-state changes to *existing* canon entities, as overlay triples
    (`<canon entity> rpg:status "dead"` in the session graph);
  * a log of player actions and of significant events (rpg:SessionEvent).

Reading an entity = canon facts + the session's overlay, with the session
winning for single-valued predicates. Reopening the session therefore returns
to exactly that world state, and discarding it leaves canon untouched.

The player can later *promote* chosen entities or state changes into canon
(promote_entities / promote_state), or discard them. The same operations work
on an adventure writer's graph (graph_for("adventure", slug)).
"""
import re
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import adventure_graph as ag
import hooks
import mapper
from kg import KnowledgeGraphManager, PREFIX, escape_literal

NS = "http://example.org/rpg-ontology#"
XSD_INT = "<http://www.w3.org/2001/XMLSchema#integer>"
SESSION_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
# Predicates where a new value replaces the old one; everything else accumulates.
SINGLE_VALUED = {"status", "locatedIn", "outcome", "isResolved", "actorKind", "itemType", "isPlayerFacing",
                 "physicalAppearance", "gmDescription", "loreContext", "hitPoints", "armorClass", "worshippedIn"}
# Bookkeeping triples that are not "state" and never move into canon.
NOT_STATE = {"type", "isCanon", "foundIn", "recordedAt", "sequence", "name"}
MAX_PLAYER_TEXT = 600


def _full(uri: str) -> str:
    return f"<{NS}{uri.split(':', 1)[-1].split('#')[-1]}>"


def _short(iri: str) -> str:
    return "rpg:" + iri.split("#")[-1]


def _rows(kg, query: str) -> list:
    bindings = kg.execute_sparql(PREFIX + query).get("results", {}).get("bindings", [])
    return [{k: v["value"] for k, v in b.items()} for b in bindings]


def _update(kg, body: str) -> None:
    kg.execute_sparql(PREFIX + body, is_update=True)


def _valid_predicate(predicate: str) -> bool:
    from pathlib import Path
    ontology = (Path(__file__).parent / "prompts" / "ontology.ttl").read_text(encoding="utf-8")
    return bool(re.search(rf"^rpg:{re.escape(predicate)}\s+a\s+rdf:Property", ontology, re.MULTILINE))


class SessionKG(ag.AdventureKG):
    """An AdventureKG whose graph is a chat session's graph."""

    def __init__(self, base: KnowledgeGraphManager, session_id: str, title: str = ""):
        if not SESSION_ID.match(session_id or ""):
            raise ValueError("invalid session id")
        super().__init__(base, session_id, title or "Play session")
        self.graph = hooks.session_graph(session_id)


def graph_for(kind: str, ident: str, kg: KnowledgeGraphManager = None, title: str = "") -> ag.AdventureKG:
    kg = kg or KnowledgeGraphManager()
    if kind == "session":
        return SessionKG(kg, ident, title)
    if kind == "adventure":
        return ag.AdventureKG(kg, ident, title)
    raise ValueError("kind must be 'session' or 'adventure'")


# ----------------------------------------------------------------- recording
def record_event(skg: ag.AdventureKG, args: dict) -> str:
    entry = {"event_name": args.get("name"), "description": args.get("description"), "outcome": args.get("outcome"),
             "location_name": args.get("location"), "participating_actors": args.get("actors") or []}
    entry = {k: v for k, v in entry.items() if v not in (None, "", [])}
    result = skg.record(mapper.map_session_events, [entry])
    stamp = [f"{u} rpg:recordedAt \"{int(time.time())}\"^^{XSD_INT} ." for u, t, _ in skg.pending if t == "SessionEvent"]
    if stamp:
        skg.flush(stamp)
    return result


def set_state(skg: ag.AdventureKG, args: dict) -> str:
    """Overlay a world-state fact on any entity, in this memory only."""
    entity, predicate, value = args.get("entity_uri", ""), args.get("predicate", ""), args.get("value", "")
    if not re.fullmatch(r"rpg:[A-Za-z0-9_]+", entity):
        return f"Invalid entity_uri '{entity}'. Use the uri from search_entities."
    if not _valid_predicate(predicate):
        return f"Unknown predicate '{predicate}'. Use a property from the ontology."
    exists = skg.base.execute_sparql(f"""{PREFIX}
        ASK {{ {{ {entity} ?p ?o }} UNION {{ GRAPH <{skg.graph}> {{ {entity} ?p ?o }} }} }}""").get("boolean")
    if not exists:
        return f"{entity} does not exist. Resolve it with search_entities first."
    if re.fullmatch(r"rpg:[A-Za-z0-9_]+", str(value)):
        target = str(value)
        if not skg.base.execute_sparql(f"""{PREFIX}
            ASK {{ {{ {target} ?p ?o }} UNION {{ GRAPH <{skg.graph}> {{ {target} ?p ?o }} }} }}""").get("boolean"):
            return f"{target} does not exist. Resolve it with search_entities first."
        obj = target
    else:
        text = str(value)
        obj = text.lower() if text.lower() in ("true", "false") else f'"{escape_literal(text)}"'
    if predicate in SINGLE_VALUED:
        _update(skg.base, f"DELETE WHERE {{ GRAPH <{skg.graph}> {{ {entity} rpg:{predicate} ?o }} }}")
    skg.flush([f"{entity} rpg:{predicate} {obj} ."])
    return f"Recorded in session memory (canon unchanged): {entity} rpg:{predicate} {value}"


def entity_state(skg: ag.AdventureKG, uri: str) -> str:
    """Canon facts merged with this memory's overlay (the overlay wins)."""
    if not re.fullmatch(r"rpg:[A-Za-z0-9_]+", uri or ""):
        return f"Invalid uri '{uri}'."
    canon = _rows(skg.base, f"SELECT ?p ?o WHERE {{ {uri} ?p ?o }} LIMIT 300")
    mine = _rows(skg.base, f"SELECT ?p ?o WHERE {{ GRAPH <{skg.graph}> {{ {uri} ?p ?o }} }} LIMIT 300")
    overridden = {m["p"] for m in mine if m["p"].split("#")[-1] in SINGLE_VALUED}
    out = [{"predicate": c["p"].split("#")[-1], "value": c["o"].split("#")[-1] if c["o"].startswith(NS) else c["o"][:400],
            "source": "canon"} for c in canon if c["p"] not in overridden]
    out += [{"predicate": m["p"].split("#")[-1], "value": m["o"].split("#")[-1] if m["o"].startswith(NS) else m["o"][:400],
             "source": "session"} for m in mine]
    return "\n".join(f"{r['predicate']}: {r['value']}  [{r['source']}]" for r in out) or f"No facts for {uri}."


def log_action(session_id: str, player_text: str, turn: int) -> None:
    """Record the player's action as a SessionEvent. Never raises."""
    try:
        text = " ".join((player_text or "").split())
        if not text:
            return
        skg = SessionKG(KnowledgeGraphManager(), session_id)
        name = f"Turn {turn}: {text[:50]}"
        skg.record(mapper.map_session_events, [{"event_name": name, "description": text[:MAX_PLAYER_TEXT]}])
        uris = [u for u, t, _ in skg.pending if t == "SessionEvent"]
        skg.flush([f"{u} rpg:sequence \"{turn}\"^^{XSD_INT} ." for u in uris]
                  + [f"{u} rpg:recordedAt \"{int(time.time())}\"^^{XSD_INT} ." for u in uris])
    except Exception as exc:
        print(f"session memory: could not log action: {exc}")


# ------------------------------------------------------------------- reading
def memory(skg: ag.AdventureKG) -> dict:
    g, kg = skg.graph, skg.base
    entities = [e for e in skg.list_entities() if e["type"] not in ("SessionEvent", "Event")]
    names = {}
    state = []
    for r in _rows(kg, f"""SELECT ?s ?p ?o WHERE {{ GRAPH <{g}> {{ ?s ?p ?o
        FILTER(STRSTARTS(STR(?s), "{NS}") && !STRSTARTS(STR(?s), "{NS}A_") && !STRSTARTS(STR(?s), "{NS}Adventure_")) }} }}"""):
        pred = r["p"].split("#")[-1]
        if pred in NOT_STATE or r["p"].endswith("22-rdf-syntax-ns#type"):
            continue
        state.append({"uri": _short(r["s"]), "predicate": pred,
                      "value": _short(r["o"]) if r["o"].startswith(NS) else r["o"]})
    for s in {x["uri"] for x in state}:
        found = _rows(kg, f"SELECT ?n WHERE {{ {s} rpg:name ?n }} LIMIT 1")
        names[s] = found[0]["n"] if found else s
    for x in state:
        x["name"] = names[x["uri"]]
    events = _rows(kg, f"""SELECT ?e ?n ?d ?o ?t ?q WHERE {{ GRAPH <{g}> {{ ?e a rpg:SessionEvent ; rpg:name ?n .
        OPTIONAL {{ ?e rpg:gmDescription ?d }} OPTIONAL {{ ?e rpg:outcome ?o }}
        OPTIONAL {{ ?e rpg:recordedAt ?t }} OPTIONAL {{ ?e rpg:sequence ?q }} }} }}""")
    events.sort(key=lambda e: (int(e.get("t", 0)), int(e.get("q", 0))))
    resolutions = _rows(kg, f"""SELECT ?h ?n ?o WHERE {{ GRAPH <{g}> {{ ?r rpg:resolvesHook ?h . OPTIONAL {{ ?r rpg:outcome ?o }} }}
        OPTIONAL {{ ?h rpg:name ?n }} }}""")
    return {"graph": g, "entities": entities, "state": sorted(state, key=lambda x: (x["name"], x["predicate"])),
            "events": [{"uri": _short(e["e"]), "name": e["n"], "description": e.get("d", ""), "outcome": e.get("o", ""),
                        "time": int(e["t"]) if e.get("t") else None} for e in events],
            "hooks_resolved": [{"uri": _short(r["h"]), "name": r.get("n", ""), "outcome": r.get("o", "")} for r in resolutions]}


# ----------------------------------------------------------------- promotion
def _values(uris: List[str]) -> str:
    for u in uris:
        if not re.fullmatch(r"rpg:[A-Za-z0-9_]+", u or ""):
            raise ValueError(f"invalid uri: {u}")
    return " ".join(_full(u) for u in uris)


def promote_entities(skg: ag.AdventureKG, uris: List[str]) -> dict:
    """Move session-created entities from the session graph into canon.
    Links to session entities that were NOT promoted stay behind in the session."""
    uris = [u for u in uris if ag.is_adventure_uri(u)]
    if not uris:
        return {"promoted": 0, "left_behind": [], "note": "No session-created entities selected."}
    values, g = _values(uris), skg.graph
    in_set = f"?o IN ({values.replace(' ', ', ')})"
    dangling = f'(isIRI(?o) && STRSTARTS(STR(?o), "{NS}A_") && !({in_set}))'
    skip = f'?p IN (rpg:isCanon, rpg:foundIn)'
    where = f"GRAPH <{g}> {{ ?s ?p ?o }} VALUES ?s {{ {values} }}"
    left = _rows(skg.base, f"SELECT ?s ?p ?o WHERE {{ {where} FILTER {dangling} }}")
    _update(skg.base, f"INSERT {{ ?s ?p ?o . ?s rpg:promotedFromSession \"{escape_literal(skg.slug)}\" }} "
                      f"WHERE {{ {where} FILTER(!({dangling}) && !({skip})) }}")
    _update(skg.base, f"DELETE {{ GRAPH <{g}> {{ ?s ?p ?o }} }} WHERE {{ {where} FILTER(!({dangling})) }}")
    return {"promoted": len(uris), "left_behind": [{"entity": _short(r["s"]), "predicate": r["p"].split("#")[-1],
                                                   "points_to": _short(r["o"])} for r in left]}


def promote_state(skg: ag.AdventureKG, items: List[dict]) -> dict:
    """Make session state changes to canon entities permanent in canon."""
    done = 0
    for it in items:
        uri, pred = it.get("uri", ""), it.get("predicate", "")
        if ag.is_adventure_uri(uri) or not re.fullmatch(r"[A-Za-z0-9_]+", pred or ""):
            continue
        s = _values([uri])
        if pred in SINGLE_VALUED:
            _update(skg.base, f"DELETE WHERE {{ {s} rpg:{pred} ?old }}")
        _update(skg.base, f"INSERT {{ {s} rpg:{pred} ?o }} WHERE {{ GRAPH <{skg.graph}> {{ {s} rpg:{pred} ?o }} }}")
        _update(skg.base, f"DELETE WHERE {{ GRAPH <{skg.graph}> {{ {s} rpg:{pred} ?o }} }}")
        done += 1
    return {"promoted": done}


def revert_state(skg: ag.AdventureKG, items: List[dict]) -> dict:
    done = 0
    for it in items:
        uri, pred = it.get("uri", ""), it.get("predicate", "")
        if not re.fullmatch(r"[A-Za-z0-9_]+", pred or ""):
            continue
        _update(skg.base, f"DELETE WHERE {{ GRAPH <{skg.graph}> {{ {_values([uri])} rpg:{pred} ?o }} }}")
        done += 1
    return {"reverted": done}


def discard_entities(skg: ag.AdventureKG, uris: List[str]) -> dict:
    uris = [u for u in uris if ag.is_adventure_uri(u)]
    if uris:
        values = _values(uris)
        _update(skg.base, f"DELETE {{ GRAPH <{skg.graph}> {{ ?s ?p ?o }} }} WHERE {{ GRAPH <{skg.graph}> {{ ?s ?p ?o }} VALUES ?s {{ {values} }} }}")
        _update(skg.base, f"DELETE {{ GRAPH <{skg.graph}> {{ ?s ?p ?o }} }} WHERE {{ GRAPH <{skg.graph}> {{ ?s ?p ?o }} VALUES ?o {{ {values} }} }}")
    return {"discarded": len(uris)}


# ----------------------------------------------------------------------- API
class Uris(BaseModel):
    uris: List[str]


class StateItems(BaseModel):
    items: List[Dict[str, str]]


def _make_router(prefix: str, kind: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["world-memory"])

    def skg(ident: str) -> ag.AdventureKG:
        try:
            return graph_for(kind, ident)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/{ident}/memory")
    def get_memory(ident: str):
        return memory(skg(ident))

    @router.post("/{ident}/memory/promote")
    def promote(ident: str, body: Uris):
        return promote_entities(skg(ident), body.uris)

    @router.post("/{ident}/memory/discard")
    def discard(ident: str, body: Uris):
        return discard_entities(skg(ident), body.uris)

    @router.post("/{ident}/memory/promote-state")
    def promote_st(ident: str, body: StateItems):
        return promote_state(skg(ident), body.items)

    @router.post("/{ident}/memory/revert-state")
    def revert_st(ident: str, body: StateItems):
        return revert_state(skg(ident), body.items)

    return router


session_router = _make_router("/sessions", "session")
adventure_router = _make_router("/author/adventures", "adventure")
