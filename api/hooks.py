"""Per-session plot-hook resolution.

A PlotHook is reusable canon: it is never marked resolved on the hook itself.
When the GM plays one out, a `rpg:HookResolution` node is written into that
chat session's own named graph (urn:rpg:session:<id>) pointing at the hook:

    <urn:rpg:resolution:<id>:<hook>> a rpg:HookResolution ;
        rpg:resolvesHook <hook> ; rpg:inSession "<id>" ; rpg:outcome "..." .

So a hook is "open" or "resolved" *per session*; another session (or a
re-run of the same adventure) sees it as open again. Deleting a session
clears its graph and with it every resolution it made.
"""
import re
import time
from typing import List, Optional

from kg import KnowledgeGraphManager, PREFIX, escape_literal

NS = "http://example.org/rpg-ontology#"
GRAPH_BASE = "urn:rpg:session:"
LOCAL_RE = re.compile(r"^[A-Za-z0-9_]+$")


def session_graph(session_id: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z-]{1,40}", session_id or ""):
        raise ValueError("invalid session id")
    return GRAPH_BASE + session_id


def _hook_iri(hook_uri: str) -> Optional[str]:
    local = hook_uri.strip().removeprefix("rpg:").removeprefix(f"<{NS}").rstrip(">")
    return NS + local if LOCAL_RE.match(local) else None


def open_hooks(kg: KnowledgeGraphManager, session_id: Optional[str], limit: int = 10) -> List[dict]:
    """Plot hooks not yet resolved in this session (all hooks if no session)."""
    not_done = ""
    if session_id:
        not_done = (f"FILTER NOT EXISTS {{ GRAPH <{session_graph(session_id)}> "
                    f"{{ ?r rpg:resolvesHook ?h }} }}")
    query = f"""{PREFIX}
    SELECT DISTINCT ?h ?name ?desc WHERE {{
        {{ ?h a rpg:PlotHook }} UNION {{ GRAPH ?g {{ ?h a rpg:PlotHook }} }}
        ?h rpg:name ?name .
        OPTIONAL {{ ?h rpg:gmDescription ?desc }}
        {not_done}
    }} LIMIT {int(limit)}"""
    rows = kg.execute_sparql(query).get("results", {}).get("bindings", [])
    return [{"uri": "rpg:" + r["h"]["value"].split("#")[-1], "name": r["name"]["value"],
             "description": r.get("desc", {}).get("value", "")} for r in rows]


def resolve_hook(kg: KnowledgeGraphManager, session_id: Optional[str], hook_uri: str, outcome: str = "") -> str:
    if not session_id:
        return "No session is active, so the resolution was not recorded. The hook stays open."
    iri = _hook_iri(hook_uri)
    if not iri:
        return f"Invalid hook URI: {hook_uri}. Use the uri returned by open_plot_hooks."
    found = kg.execute_sparql(f"""{PREFIX}
    ASK {{ {{ <{iri}> a rpg:PlotHook }} UNION {{ GRAPH ?g {{ <{iri}> a rpg:PlotHook }} }} }}""")
    if not found.get("boolean"):
        return f"{hook_uri} is not a known PlotHook."
    node = f"urn:rpg:resolution:{session_id}:{iri.split('#')[-1]}"
    triples = [f"<{node}> a rpg:HookResolution ; rpg:resolvesHook <{iri}> ; "
               f'rpg:inSession "{escape_literal(session_id)}" ; rpg:resolvedAt "{int(time.time())}"^^<http://www.w3.org/2001/XMLSchema#integer>']
    if outcome:
        triples[0] += f' ; rpg:outcome "{escape_literal(outcome)}"'
    kg.execute_sparql(f"{PREFIX}\nINSERT DATA {{ GRAPH <{session_graph(session_id)}> {{ {triples[0]} . }} }}",
                      is_update=True)
    return f"Recorded: {hook_uri} is resolved for this session only; it remains available to other sessions."


def clear_session(kg: KnowledgeGraphManager, session_id: str) -> None:
    kg.execute_sparql(f"CLEAR SILENT GRAPH <{session_graph(session_id)}>", is_update=True)
