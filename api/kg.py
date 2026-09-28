"""Thin SPARQL client plus the entity-resolution helpers shared by the live
GM agent (main.py) and the deterministic Pass 3 mapper (mapper.py). Kept in
one place so both never drift on how names get turned into URIs.
"""
import os
import re
from SPARQLWrapper import SPARQLWrapper, JSON, POST

SPARQL_QUERY_URL = os.getenv("SPARQL_QUERY", "http://triplestore:7879/query")
SPARQL_UPDATE_URL = os.getenv("SPARQL_UPDATE", "http://triplestore:7879/update")
PREFIX = "PREFIX rpg: <http://example.org/rpg-ontology#>\n"


def escape_literal(value) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', '')


class KnowledgeGraphManager:
    def __init__(self):
        self.query_client = SPARQLWrapper(SPARQL_QUERY_URL)
        self.update_client = SPARQLWrapper(SPARQL_UPDATE_URL)
        self.query_client.setReturnFormat(JSON)

    def execute_sparql(self, query: str, is_update: bool = False) -> dict:
        client = self.update_client if is_update else self.query_client
        client.setQuery(query)
        if is_update:
            client.setMethod(POST)
            client.query()
            return {"status": "success"}
        return client.query().convert()

    def sanitize_qname(self, name: str, fallback_prefix: str) -> str:
        clean_name = re.sub(r'[^a-zA-Z0-9_]', '', str(name).replace(' ', '_'))
        if not clean_name:
            return f"{fallback_prefix}unknown"
        if not clean_name[0].isalpha():
            return f"{fallback_prefix}{clean_name}"
        return clean_name

    def get_existing_uri(self, entity_name: str, rdf_type: str):
        """Match is scoped to `rdf_type` - name alone isn't enough, since the
        same string routinely gets used for unrelated things across a module
        (e.g. "Randal Morn" the NPC vs. a Location/Scene that happens to share
        a name), and a name-only match would silently fold them onto one URI
        wearing incompatible types."""
        if not entity_name:
            return None
        safe_name = escape_literal(entity_name)
        safe_type = self.sanitize_qname(rdf_type, "t_")
        query = f"""{PREFIX}
        SELECT ?existingURI WHERE {{
            ?existingURI a rpg:{safe_type} ; rpg:name ?name .
            FILTER (lcase(?name) = lcase("{safe_name}"))
        }} LIMIT 1"""
        bindings = self.execute_sparql(query).get("results", {}).get("bindings", [])
        if bindings:
            return f"rpg:{bindings[0]['existingURI']['value'].split('#')[-1]}"
        return None

    def has_property(self, uri: str, predicate: str) -> bool:
        result = self.execute_sparql(f"{PREFIX}\nASK {{ {uri} rpg:{predicate} ?o }}")
        return bool(result.get("boolean"))

    def search_entities(self, query: str, rdf_type: str = None, limit: int = 10) -> list:
        """Fuzzy-match `query` against every rpg:name/rpg:alias in the graph,
        plus each entity's rpg:physicalAppearance/rpg:gmDescription, and
        return the best-scoring entities, each with its real `uri`.

        Entity names are short simplified labels (see mapper.py), so a query
        that echoes the original source text - e.g. a phrase from a
        read-aloud passage or a trigger's revealed content - won't match the
        name at all. Matching descriptions too means that full text stays
        findable even though it's no longer stuffed into the name itself.
        A description hit is capped below any real name match so a vague
        mention in some other entity's notes never outranks the entity the
        query is actually naming.

        The agent must call this before touching an entity by URI - the URI
        scheme (`rpg:<Type>_<sanitized name>`) is an ingestion-time
        implementation detail, and a model that instead guesses a URI from
        the ontology's class names alone reliably invents one that doesn't
        exist, silently querying nothing.
        """
        if not query or not query.strip():
            return []

        if rdf_type:
            safe_type = self.sanitize_qname(rdf_type, "t_")
            type_clause = f"?s a rpg:{safe_type} ."
        else:
            type_clause = "?s a ?anyType ."

        sparql = f"""{PREFIX}
        SELECT DISTINCT ?s ?type ?label ?desc WHERE {{
            {type_clause}
            ?s a ?type .
            {{ ?s rpg:name ?label }} UNION {{ ?s rpg:alias ?label }}
            OPTIONAL {{ {{ ?s rpg:physicalAppearance ?desc }} UNION {{ ?s rpg:gmDescription ?desc }} }}
        }}"""
        bindings = self.execute_sparql(sparql).get("results", {}).get("bindings", [])

        query_lower = query.strip().lower()
        query_tokens = set(query_lower.split())
        best: dict = {}
        for b in bindings:
            uri = b["s"]["value"]
            label = b["label"]["value"]
            label_lower = label.lower()

            if label_lower == query_lower:
                score = 100.0
            elif label_lower.startswith(query_lower) or query_lower.startswith(label_lower):
                score = 85.0
            elif query_lower in label_lower or label_lower in query_lower:
                score = 70.0
            else:
                label_tokens = set(label_lower.split())
                overlap = len(query_tokens & label_tokens)
                score = 40.0 * overlap / len(query_tokens) if overlap else 0.0

            desc = b.get("desc", {}).get("value")
            if desc:
                desc_lower = desc.lower()
                if query_lower in desc_lower:
                    score = max(score, 55.0)
                else:
                    desc_tokens = set(re.split(r"\W+", desc_lower))
                    overlap = len(query_tokens & desc_tokens)
                    if overlap:
                        score = max(score, 30.0 * overlap / len(query_tokens))

            if score <= 0.0:
                continue
            if uri not in best or score > best[uri][0]:
                best[uri] = (score, b["type"]["value"], label)

        ranked = sorted(best.items(), key=lambda item: item[1][0], reverse=True)[:limit]
        return [
            {
                "uri": f"rpg:{uri.split('#')[-1]}",
                "type": type_uri.split("#")[-1],
                "name": label,
                "score": round(score, 1),
            }
            for uri, (score, type_uri, label) in ranked
        ]

    def get_or_create_uri(self, name: str, rdf_type: str, triples: list) -> str:
        """Resolve `name` to an existing entity URI of type `rdf_type`, or
        allocate a new one and append its `a rpg:<Type>` / `rpg:name` triples
        to `triples`."""
        name = str(name).strip()
        safe_type = self.sanitize_qname(rdf_type, "t_")
        existing = self.get_existing_uri(name, rdf_type)
        if existing:
            return existing
        # Type-prefixed so two different types minting a "new" URI for the
        # same name (e.g. a Location and an Actor both called "Frostgate")
        # never collide on the URI itself - the lookup above only guarantees
        # they won't be treated as the *same* entity, not that their URIs differ.
        uri = f"rpg:{safe_type}_{self.sanitize_qname(name, 'e_')}"
        triples.append(f"{uri} a rpg:{safe_type} .")
        triples.append(f'{uri} rpg:name "{escape_literal(name)}" .')
        return uri
