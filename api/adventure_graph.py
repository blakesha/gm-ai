"""Graph memory for adventures being written.

Each adventure gets its own named graph (urn:rpg:adventure:<slug>) next to the
canon (default) graph. Everything the writer records goes in that graph,
flagged `rpg:isCanon false` and `rpg:foundIn rpg:Adventure_<slug>`.

Why a separate graph: canon queries (the GM agent, ingestion) only look at the
default graph, so invented material can never be mistaken for setting canon,
yet a single query can still join across both with GRAPH <...> { ... }.
Discarding an adventure is CLEAR GRAPH; nothing in canon is ever modified.

`AdventureKG` mimics the small interface mapper.py expects of a graph manager
(get_or_create_uri / get_existing_place_uri / has_property), so the same
mapping code that ingests books also records adventure entities:
  * a name that already exists in THIS adventure resolves to that node;
  * else a name that exists in canon is *referenced* (never duplicated), so an
    NPC can be `locatedIn` the real Shadowdale node;
  * else a new node is minted with the adventure's URI prefix.
"""
import re
from typing import Dict, List, Optional

import mapper
from kg import (KnowledgeGraphManager, PREFIX, PLACE_TYPES, canonical_key, escape_literal)

GRAPH_BASE = "urn:rpg:adventure:"
PLACE_FAMILY = ["Structure", "Settlement", "Region", "Room", "World", "Landmark"]
MAX_INSERT = 150


def _safe(slug: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", slug)


def graph_iri(slug: str) -> str:
    return GRAPH_BASE + slug


def uri_prefix(slug: str) -> str:
    return f"A_{_safe(slug)}_"


def adventure_uri(slug: str) -> str:
    return f"rpg:Adventure_{_safe(slug)}"


def is_adventure_uri(uri: str) -> bool:
    return uri.split(":", 1)[-1].startswith("A_")


class AdventureKG:
    def __init__(self, base: KnowledgeGraphManager, slug: str, title: str = ""):
        self.base, self.slug, self.title = base, slug, title or slug
        self.graph = graph_iri(slug)
        self.touched = set()
        self.pending: List[tuple] = []        # (uri, type, name) minted this call, not yet stored
        self.created: List[tuple] = []        # (name, type)
        self.canon_refs: List[tuple] = []     # (name, canon_uri)
        self.force_new: set = set()

    # ------------------------------------------------------------ low level
    def _rows(self, query: str) -> list:
        bindings = self.base.execute_sparql(PREFIX + query).get("results", {}).get("bindings", [])
        return [{k: v["value"] for k, v in b.items()} for b in bindings]

    def _update(self, body: str) -> None:
        self.base.execute_sparql(PREFIX + body, is_update=True)

    def ensure_adventure_node(self) -> None:
        self._update(f"""INSERT DATA {{ GRAPH <{self.graph}> {{
            {adventure_uri(self.slug)} a rpg:Adventure ; rpg:name "{escape_literal(self.title)}" ; rpg:isCanon false . }} }}""")

    def flush(self, triples: list) -> None:
        self.ensure_adventure_node()
        for i in range(0, len(triples), MAX_INSERT):
            chunk = "\n".join(triples[i:i + MAX_INSERT])
            self._update(f"INSERT DATA {{ GRAPH <{self.graph}> {{\n{chunk}\n}} }}")

    # ---------------------------------------------------------- resolution
    def _family(self, rdf_type: str) -> list:
        return PLACE_FAMILY if rdf_type in PLACE_TYPES else [rdf_type]

    def _match_pending(self, name: str, family: list) -> Optional[str]:
        key = canonical_key(name, family[0])
        for uri, t, n in self.pending:
            if t in family and (n.lower() == name.lower() or (key and canonical_key(n, t) == key)):
                return uri
        return None

    def _find_in_graph(self, name: str, family: list) -> Optional[str]:
        """Exact (case-insensitive), then canonical-key match inside this adventure's graph."""
        values = " ".join(f"rpg:{t}" for t in family)
        rows = self._rows(f"""SELECT ?e ?t WHERE {{ GRAPH <{self.graph}> {{ VALUES ?t {{ {values} }}
            ?e a ?t ; rpg:name ?n . FILTER(lcase(?n) = lcase("{escape_literal(name)}")) }} }}""")
        if rows:
            rank = {f"http://example.org/rpg-ontology#{t}": i for i, t in enumerate(family)}
            best = min(rows, key=lambda r: rank.get(r["t"], 99))
            return f"rpg:{best['e'].split('#')[-1]}"
        key = canonical_key(name, family[0])
        runs = re.findall(r"[a-z0-9]{3,}", name.lower())
        if len(key) < 4 or not runs:
            return None
        needle = max(runs, key=len)[:3]
        rows = self._rows(f"""SELECT DISTINCT ?e ?t ?n WHERE {{ GRAPH <{self.graph}> {{ VALUES ?t {{ {values} }}
            ?e a ?t ; rpg:name ?n . FILTER(CONTAINS(lcase(?n), "{escape_literal(needle)}")) }} }}""")
        hits = {r["e"] for r in rows if canonical_key(r["n"], r["t"].split("#")[-1]) == key}
        return f"rpg:{hits.pop().split('#')[-1]}" if len(hits) == 1 else None

    def _find_in_canon(self, name: str, rdf_type: str) -> Optional[str]:
        if rdf_type in PLACE_TYPES:
            uri = self.base.get_existing_place_uri(name, PLACE_FAMILY)
        else:
            uri = self.base.get_existing_uri(name, rdf_type)
        if uri:
            return uri
        similar = self.base.find_similar(name, rdf_type)
        return similar[0] if similar else None

    def get_existing_place_uri(self, name: str, types: list, triples: list = None) -> Optional[str]:
        family = [t for t in types if t in PLACE_FAMILY] or PLACE_FAMILY
        found = self._match_pending(name, family) or self._find_in_graph(name, family)
        if found:
            return found
        canon = self._find_in_canon(name, "Landmark")
        if canon and (name, canon) not in self.canon_refs:
            self.canon_refs.append((name, canon))
        return canon

    def get_or_create_uri(self, name: str, rdf_type: str, triples: list) -> str:
        name = str(name).strip()
        family = self._family(rdf_type)
        found = self._match_pending(name, family) or self._find_in_graph(name, family)
        if found:
            return found
        if name.lower() not in self.force_new:
            canon = self._find_in_canon(name, rdf_type)
            if canon:
                if (name, canon) not in self.canon_refs:
                    self.canon_refs.append((name, canon))
                return canon
        safe_type = self.base.sanitize_qname(rdf_type, "t_")
        uri = f"rpg:{uri_prefix(self.slug)}{safe_type}_{self.base.sanitize_qname(name, 'e_')}"
        triples += [f"{uri} a rpg:{safe_type} .", f'{uri} rpg:name "{escape_literal(name)}" .',
                    f"{uri} rpg:isCanon false .", f"{uri} rpg:foundIn {adventure_uri(self.slug)} ."]
        self.pending.append((uri, safe_type, name))
        self.created.append((name, safe_type))
        return uri

    def has_property(self, uri: str, predicate: str) -> bool:
        res = self.base.execute_sparql(f"{PREFIX}\nASK {{ GRAPH <{self.graph}> {{ {uri} rpg:{predicate} ?o }} }}")
        return bool(res.get("boolean"))

    # ------------------------------------------------------------ recording
    def record(self, mapper_fn, entries: list, force_new: Optional[str] = None) -> str:
        self.created, self.canon_refs, self.pending = [], [], []
        if force_new:
            self.force_new.add(force_new.strip().lower())
        triples: list = []
        mapper_fn(entries, self, triples)
        self.flush(triples)
        lines = []
        if self.created:
            lines.append("New (non-canon) in this adventure: " + ", ".join(f"{n} [{t}]" for n, t in self.created))
        if self.canon_refs:
            lines.append("Linked to existing canon (not duplicated): "
                         + ", ".join(f"{n} -> {u}" for n, u in self.canon_refs))
        return "\n".join(lines) or "Recorded (no changes)."

    # -------------------------------------------------------------- queries
    def list_entities(self) -> List[Dict[str, str]]:
        rows = self._rows(f"""SELECT ?e ?t ?n WHERE {{ GRAPH <{self.graph}> {{ ?e a ?t ; rpg:name ?n .
            FILTER(?t != rpg:Adventure) }} }} ORDER BY ?t ?n""")
        return [{"uri": f"rpg:{r['e'].split('#')[-1]}", "type": r["t"].split("#")[-1], "name": r["n"]} for r in rows]

    def remove_entity(self, name: str) -> str:
        """Delete one entity this adventure created (never canon)."""
        uri = None
        for family in (PLACE_FAMILY, ["Actor"], ["Organization"], ["Deity"], ["Item"], ["PlotHook"], ["Scene"], ["HistoricalEvent"]):
            uri = self._find_in_graph(name, family)
            if uri:
                break
        if not uri or not is_adventure_uri(uri):
            return f"No entity named '{name}' in this adventure (canon entities cannot be removed)."
        full = f"<http://example.org/rpg-ontology#{uri.split(':', 1)[-1]}>"
        self._update(f"DELETE WHERE {{ GRAPH <{self.graph}> {{ {full} ?p ?o }} }}")
        self._update(f"DELETE WHERE {{ GRAPH <{self.graph}> {{ ?s ?p {full} }} }}")
        return f"Removed '{name}' ({uri}) and its links from the adventure graph."

    def clear(self) -> None:
        self._update(f"CLEAR GRAPH <{self.graph}>")


# ----------------------------------------------------------- tool -> mapper
def _entry(args: dict, mapping: Dict[str, str]) -> dict:
    return {dst: args.get(src) for src, dst in mapping.items() if args.get(src) not in (None, "")}


def record_location(agkg: AdventureKG, args: dict) -> str:
    entry = _entry(args, {"name": "location_name", "location_type": "location_type",
                          "physical_appearance": "physical_appearance", "gm_description": "gm_description",
                          "lore_context": "lore_context", "parent": "parent_location_hint"})
    entry["exits"] = [{"leads_to": e.get("leads_to"), "direction": e.get("direction")}
                      for e in (args.get("exits") or []) if isinstance(e, dict) and e.get("leads_to")]
    return agkg.record(mapper.map_locations, [entry], force_new=args.get("name") if args.get("new_entity") else None)


def record_actor(agkg: AdventureKG, args: dict) -> str:
    entry = _entry(args, {"name": "actor_name", "actor_kind": "actor_kind", "affiliation": "affiliation_hint",
                          "physical_appearance": "physical_appearance", "gm_description": "gm_description",
                          "lore_context": "lore_context", "located_in": "located_in_hint"})
    return agkg.record(mapper.map_actors, [entry], force_new=args.get("name") if args.get("new_entity") else None)


def record_item(agkg: AdventureKG, args: dict) -> str:
    entry = _entry(args, {"name": "item_name", "item_type": "item_type", "physical_appearance": "physical_appearance",
                          "gm_description": "gm_description", "lore_context": "lore_context",
                          "located_in": "located_in_hint", "owner": "owner_actor_hint"})
    return agkg.record(mapper.map_items, [entry], force_new=args.get("name") if args.get("new_entity") else None)


def record_plot_hook(agkg: AdventureKG, args: dict) -> str:
    entry = _entry(args, {"name": "hook_name", "description": "hook_description", "status": "status",
                          "giver": "hook_giver_hint", "involves_actor": "involves_actor_hint",
                          "involves_location": "involves_location_hint", "involves_item": "involves_item_hint"})
    return agkg.record(mapper.map_plot_hooks, [entry])


RECORDERS = {"record_location": record_location, "record_actor": record_actor,
             "record_item": record_item, "record_plot_hook": record_plot_hook}
