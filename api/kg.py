"""Thin SPARQL client plus the entity-resolution helpers shared by the live
GM agent (main.py) and the deterministic Pass 3 mapper (mapper.py). Kept in
one place so both never drift on how names get turned into URIs.
"""
import os
import re
import unicodedata
from SPARQLWrapper import SPARQLWrapper, JSON, POST

SPARQL_QUERY_URL = os.getenv("SPARQL_QUERY", "http://triplestore:7879/query")
SPARQL_UPDATE_URL = os.getenv("SPARQL_UPDATE", "http://triplestore:7879/update")
PREFIX = ("PREFIX rpg: <http://example.org/rpg-ontology#>\n"
          "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n")


def escape_literal(value) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', '')


# --- name canonicalisation -------------------------------------------------
# Used to recognise that "Shadowdale village", "Village of Shadowdale" and
# "Shadowdale" are one entity. Only generic descriptors that are never part of
# a proper name are dropped; words like castle/keep/tower/inn stay, because
# "Old Skull" and "Old Skull Inn" really are different places.
PLACE_TYPES = {"World", "Region", "Settlement", "Structure", "Landmark", "Location", "Room"}
ACTOR_TYPES = {"Actor", "Organization", "Deity"}
ARTICLES = {"the", "a", "an"}
PLACE_DESCRIPTORS = {"village", "town", "city", "hamlet", "township", "kingdom", "realm", "region", "province"}
ACTOR_TITLES = {"lord", "lady", "sir", "dame", "king", "queen", "prince", "princess", "duke", "duchess",
                "baron", "baroness", "count", "countess", "captain", "master", "mistress", "mister", "mr", "mrs"}
# Types where fuzzy matching is safe. Scenes, events, hooks etc. carry
# free-text titles, so near-matches there are usually coincidence.
FUZZY_TYPES = PLACE_TYPES | {"Actor", "Item"}
MIN_FUZZY_KEY = 4
NULLISH_NAMES = {"null", "none", "nil", "unknown", "na", "n"}  # junk the extractor emits instead of JSON null


def _word_prefix(prefix: str, text: str) -> bool:
    """text starts with prefix at a word boundary ("drow" is not a prefix of "drowned cellar")."""
    return bool(prefix) and text.startswith(prefix) and (len(text) == len(prefix) or not text[len(prefix)].isalnum())


def _word_in(needle: str, haystack: str) -> bool:
    """needle occurs in haystack as whole word(s)."""
    return bool(needle) and re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack) is not None


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


COMMENTARY = re.compile(r"\s*\((?=[^)]*(?:implied|mention|stated|explicit|passage|context|likely|described|"
                        r"suggest|not specified|affiliat))[^)]*\)?|\s*\([^)]{40,}\)?")


def strip_commentary(name: str) -> str:
    """Remove the explanatory asides small models append to names, e.g.
    "Crown of Cormyr (implied by the passage's context.)" -> "Crown of Cormyr".
    Short descriptive parentheticals like "(Red Wizard of Thay)" are kept."""
    cleaned = COMMENTARY.sub("", str(name)).strip(" ,;:")
    return cleaned or str(name).strip()


def canonical_key(name: str, rdf_type: str = "Landmark") -> str:
    """Accent-, case- and punctuation-insensitive key with articles and the
    type's generic descriptors removed ("Village of Shadowdale" -> "shadowdale")."""
    base = re.sub(r"\([^)]*\)", " ", str(name))  # drop parentheticals: "Foo (the keep)"
    drop = set()
    tokens = _fold(base).split()
    while tokens and tokens[0] in ARTICLES:  # articles only count at the front
        tokens.pop(0)
    if rdf_type in PLACE_TYPES:
        drop |= PLACE_DESCRIPTORS
    elif rdf_type in ACTOR_TYPES:
        # honorifics are only dropped at the front: "Lord Marsember" ==
        # "Marsember", but "Harper King" is not "Harper".
        # ("Lord of Waterdeep" is a description, not "Waterdeep".)
        while len(tokens) > 1 and tokens[0] in ACTOR_TITLES and tokens[1] != "of":
            tokens.pop(0)
    tokens = [t for t in tokens if t not in drop]
    if tokens and tokens[0] in NULLISH_NAMES:
        return ""
    while tokens and tokens[0] in ("of", "and"):
        tokens.pop(0)
    while tokens and tokens[-1] in ("of", "and"):
        tokens.pop()
    return " ".join(tokens)


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

    def search_entities(self, query: str, rdf_type: str = None, limit: int = 10, graph: str = None) -> list:
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

        desc = """OPTIONAL { { ?s rpg:physicalAppearance ?desc } UNION { ?s rpg:gmDescription ?desc } }"""
        label = "{ ?s rpg:name ?label } UNION { ?s rpg:alias ?label }"
        if rdf_type:
            safe_type = self.sanitize_qname(rdf_type, "t_")
            # subclass-aware: "Location" finds Regions, Structures, Rooms, ...
            type_clause = f"?s a ?matchType . ?matchType rdfs:subClassOf* rpg:{safe_type} ."
            graph_type_clause = f"GRAPH <{graph}> {{ ?s a ?matchType }} ?matchType rdfs:subClassOf* rpg:{safe_type} ."
        else:
            type_clause = "?s a ?anyType ."
            graph_type_clause = f"GRAPH <{graph}> {{ ?s a ?anyType }}"
        body = f"{type_clause} ?s a ?type . {label} {desc}"
        if graph:
            # canon (default graph) UNION one adventure's named graph
            graph_body = (f"{graph_type_clause} GRAPH <{graph}> {{ ?s a ?type . {label} "
                          f"OPTIONAL {{ {{ ?s rpg:physicalAppearance ?desc }} UNION {{ ?s rpg:gmDescription ?desc }} }} }}")
            body = f"{{ {body} }} UNION {{ {graph_body} }}"
        sparql = f"""{PREFIX}
        SELECT DISTINCT ?s ?type ?label ?desc WHERE {{
            {body}
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
            elif _word_prefix(label_lower, query_lower) or _word_prefix(query_lower, label_lower):
                score = 85.0
            elif _word_in(query_lower, label_lower) or _word_in(label_lower, query_lower):
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

        # On equal scores prefer real world entities over per-chunk bookkeeping
        # nodes (a Scene or event that merely shares the name), so the model
        # lands on the entity that carries the description.
        low_priority = {"Scene", "HistoricalEvent", "PlotHook", "SourceDocument", "Trigger", "HiddenInfo", "ReadAloudText"}

        def sort_key(item):
            score, type_uri, _ = item[1]
            return (score, type_uri.split("#")[-1] not in low_priority)

        ranked = sorted(best.items(), key=sort_key, reverse=True)[:limit]
        described = set()
        if ranked:
            values = " ".join(f"<{uri}>" for uri, _ in ranked)
            inner = "?s ?p ?o . FILTER(?p IN (rpg:physicalAppearance, rpg:gmDescription, rpg:loreContext))"
            where = f"{{ {inner} }}" + (f" UNION {{ GRAPH <{graph}> {{ {inner} }} }}" if graph else "")
            props = self.execute_sparql(f"""{PREFIX}
            SELECT DISTINCT ?s WHERE {{ VALUES ?s {{ {values} }} {where} }}""")
            described = {b["s"]["value"] for b in props.get("results", {}).get("bindings", [])}
        return [
            {
                "uri": f"rpg:{uri.split('#')[-1]}",
                "type": type_uri.split("#")[-1],
                "name": label,
                "score": round(score, 1),
                "has_description": uri in described,
            }
            for uri, (score, type_uri, label) in ranked
        ]

    def get_existing_place_uri(self, name: str, types: list):
        """Like get_existing_uri but across several place types at once, in the
        order given (the first type that matches wins)."""
        if not name:
            return None
        safe_name = escape_literal(name)
        values = " ".join(f"rpg:{self.sanitize_qname(t, 't_')}" for t in types)
        query = f"""{PREFIX}
        SELECT ?existingURI ?t WHERE {{
            VALUES ?t {{ {values} }}
            ?existingURI a ?t ; rpg:name ?name .
            FILTER (lcase(?name) = lcase("{safe_name}"))
        }}"""
        bindings = self.execute_sparql(query).get("results", {}).get("bindings", [])
        if not bindings:
            return None
        rank = {f"http://example.org/rpg-ontology#{t}": i for i, t in enumerate(types)}
        best = min(bindings, key=lambda b: rank.get(b["t"]["value"], len(types)))
        return f"rpg:{best['existingURI']['value'].split('#')[-1]}"

    def find_similar(self, name: str, rdf_type: str):
        """Fuzzy lookup: an existing entity of the same family whose canonical
        key equals this name's, e.g. "Shadowdale village" -> "Shadowdale".
        Returns (uri, existing_name) or None. Only types in FUZZY_TYPES; an
        ambiguous match (several distinct entities share the key) returns None
        rather than guessing."""
        if rdf_type not in FUZZY_TYPES:
            return None
        key = canonical_key(name, rdf_type)
        if len(key) < MIN_FUZZY_KEY:
            return None
        # Cheap server-side narrowing on the longest plain-ASCII run, then an
        # exact canonical-key comparison here (SPARQL can't fold accents).
        raw = re.sub(r"\([^)]*\)", " ", str(name)).lower()
        runs = re.findall(r"[a-z0-9]{3,}", raw)
        if not runs:
            return None
        # 3-char prefix: tolerant of accents later in the word (Faerun/Faerûn)
        needle = max(runs, key=len)[:3]
        if rdf_type in PLACE_TYPES:
            family = ["Structure", "Settlement", "Region", "Room", "World", "Landmark"]
        else:
            family = [rdf_type]
        values = " ".join(f"rpg:{t}" for t in family)
        query = f"""{PREFIX}
        SELECT DISTINCT ?e ?t ?name WHERE {{
            VALUES ?t {{ {values} }}
            ?e a ?t ; rpg:name ?name .
            FILTER (CONTAINS(lcase(?name), "{escape_literal(needle)}"))
        }}"""
        bindings = self.execute_sparql(query).get("results", {}).get("bindings", [])
        matches = {}
        for b in bindings:
            t = b["t"]["value"].split("#")[-1]
            if canonical_key(b["name"]["value"], t) == key:
                matches[b["e"]["value"]] = (family.index(t) if t in family else len(family), b["name"]["value"])
        if not matches:
            return None
        best_rank = min(rank for rank, _ in matches.values())
        top = [(u, n) for u, (rank, n) in matches.items() if rank == best_rank]
        if len(top) > 1:
            # several nodes share the key (an existing duplicate): prefer the
            # one whose name is the bare canonical form ("Shadowdale").
            bare = [(u, n) for u, n in top if _fold(n) == key]
            if len(bare) != 1:
                return None
            top = bare
        uri, existing_name = top[0]
        return f"rpg:{uri.split('#')[-1]}", existing_name

    def get_or_create_uri(self, name: str, rdf_type: str, triples: list) -> str:
        """Resolve `name` to an existing entity URI of type `rdf_type`, or
        allocate a new one and append its `a rpg:<Type>` / `rpg:name` triples
        to `triples`."""
        name = str(name).strip()
        safe_type = self.sanitize_qname(rdf_type, "t_")
        existing = self.get_existing_uri(name, rdf_type)
        if existing:
            return existing
        similar = self.find_similar(name, rdf_type)
        if similar:
            uri, existing_name = similar
            if existing_name.strip().lower() != name.lower():
                # keep the variant findable via search_entities
                triples.append(f'{uri} rpg:alias "{escape_literal(name)}" .')
            return uri
        # Type-prefixed so two different types minting a "new" URI for the
        # same name (e.g. a Location and an Actor both called "Frostgate")
        # never collide on the URI itself - the lookup above only guarantees
        # they won't be treated as the *same* entity, not that their URIs differ.
        uri = f"rpg:{safe_type}_{self.sanitize_qname(name, 'e_')}"
        triples.append(f"{uri} a rpg:{safe_type} .")
        triples.append(f'{uri} rpg:name "{escape_literal(name)}" .')
        return uri
