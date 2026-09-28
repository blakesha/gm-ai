"""Pass 4: post-ingest entity resolution.

Runs as a separate maintenance step, not during ingestion. Duplicate
candidates only become visible once multiple chunks have mentioned the same
entity under slightly different names - merging mid-ingest risks collapsing
two genuinely distinct entities before enough context exists to tell them
apart. `get_or_create_uri` in kg.py only merges on an exact, case-insensitive
`rpg:name` match; this pass catches the near-misses it lets through.

Two tiers, because false-positive risk differs sharply by type:
  - AUTO: same-type pairs where both sides are in NAMED_ENTITY_TYPES (Actor/
    Item/Location/GameSystem) and one name is a whole-word affix of the
    other. `rpg:name` on these types is meant to be a canonical proper name,
    so containment is a strong signal (e.g. "Randal" / "Randal Morn").
  - REVIEW: everything else that matched, including all Scene/Event/Trigger/
    ReadAloudText/HiddenInfo pairs. Those types carry free-text titles
    generated per-chunk, so containment is frequently coincidental (e.g. two
    unrelated Scenes both titled "Introduction ..."). Never auto-merged.

Usage:
    python resolve_duplicates.py            # dry run: print both tiers, no writes
    python resolve_duplicates.py --apply     # merge the AUTO tier for real

There is no automatic undo for a merge. Eyeball the AUTO list the dry run
prints before passing --apply, and snapshot the oxigraph_data volume first
if you want a way back.
"""
import argparse
import re
from collections import namedtuple

from kg import KnowledgeGraphManager, PREFIX

RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
NAMED_ENTITY_TYPES = {"Actor", "Item", "Location", "GameSystem"}
MIN_NAME_LEN = 4
STOPWORD_PREFIX = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)

Entity = namedtuple("Entity", ["uri", "type", "name"])
Pair = namedtuple("Pair", ["survivor", "duplicate", "type"])


def fetch_entities(kg) -> list:
    query = f"""{PREFIX}
    SELECT ?entity ?type ?name WHERE {{
      ?entity a ?type ; rpg:name ?name .
    }}"""
    bindings = kg.execute_sparql(query).get("results", {}).get("bindings", [])
    return [
        Entity(
            uri=f"rpg:{b['entity']['value'].split('#')[-1]}",
            type=b["type"]["value"].split("#")[-1],
            name=b["name"]["value"],
        )
        for b in bindings
    ]


def _contains_as_phrase(shorter: str, longer: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(shorter.lower()) + r"(?!\w)"
    return re.search(pattern, longer.lower()) is not None


def _stripped_len(name: str) -> int:
    return len(STOPWORD_PREFIX.sub("", name).strip())


def pick_survivor(a: Entity, b: Entity) -> Pair:
    """Prefer the more specific name. Strip leading "the/a/an" before
    comparing length so "the Old Skull" doesn't beat "Old Skull" on noise
    alone; ties fall back to the shorter raw string, which is usually the
    cleaner canonical form."""
    len_a, len_b = _stripped_len(a.name), _stripped_len(b.name)
    if len_a != len_b:
        winner, loser = (a, b) if len_a > len_b else (b, a)
    else:
        winner, loser = (a, b) if len(a.name) <= len(b.name) else (b, a)
    return Pair(survivor=winner.uri, duplicate=loser.uri, type=a.type)


def find_candidates(entities: list) -> tuple:
    by_type = {}
    for e in entities:
        by_type.setdefault(e.type, []).append(e)

    auto, review = [], []
    for etype, group in by_type.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if len(a.name) < MIN_NAME_LEN or len(b.name) < MIN_NAME_LEN:
                    continue
                shorter, longer = (a, b) if len(a.name) <= len(b.name) else (b, a)
                if not _contains_as_phrase(shorter.name, longer.name):
                    continue
                pair = pick_survivor(a, b)
                (auto if etype in NAMED_ENTITY_TYPES else review).append(pair)
    return auto, review


def merge_entity(kg, survivor: str, duplicate: str) -> None:
    prefix = PREFIX + "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"

    # 1. Re-home the duplicate's outgoing edges onto the survivor.
    kg.execute_sparql(f"""{prefix}
    DELETE {{ {duplicate} ?p ?o }}
    INSERT {{ {survivor} ?p ?o }}
    WHERE {{ {duplicate} ?p ?o . FILTER(?p != rdf:type && ?p != rpg:name) }}
    """, is_update=True)

    # 2. Re-home edges pointing at the duplicate onto the survivor.
    kg.execute_sparql(f"""{prefix}
    DELETE {{ ?s ?p {duplicate} }}
    INSERT {{ ?s ?p {survivor} }}
    WHERE {{ ?s ?p {duplicate} . }}
    """, is_update=True)

    # 3. Keep the duplicate's name as an alias, then drop what's left of it
    #    (its `a rpg:Type` and `rpg:name` triples).
    kg.execute_sparql(f"""{prefix}
    DELETE {{ {duplicate} ?p ?o }}
    INSERT {{ {survivor} rpg:alias ?name }}
    WHERE {{
      {duplicate} rpg:name ?name .
      {duplicate} ?p ?o .
    }}
    """, is_update=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Merge the AUTO tier for real.")
    args = parser.parse_args()

    kg = KnowledgeGraphManager()
    entities = fetch_entities(kg)
    auto, review = find_candidates(entities)

    uri_to_name = {e.uri: e.name for e in entities}
    print(f"AUTO tier ({len(auto)} pairs) - {'merging' if args.apply else 'dry run'}:")
    for p in auto:
        arrow = "->" if args.apply else "(would merge)"
        print(f"  [{p.type}] {uri_to_name[p.duplicate]!r} {arrow} {uri_to_name[p.survivor]!r}")
        if args.apply:
            merge_entity(kg, p.survivor, p.duplicate)

    print(f"\nREVIEW tier ({len(review)} pairs) - never auto-merged:")
    for p in review:
        print(f"  [{p.type}] {uri_to_name[p.duplicate]!r} <-> {uri_to_name[p.survivor]!r}")


if __name__ == "__main__":
    main()
