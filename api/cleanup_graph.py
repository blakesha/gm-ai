"""Post-ingest graph cleanup: fixes systematic extraction noise.

Steps (each independent, all dry-run unless --apply):
  worlds  - extraction tagged rivers/cities/etc. as rpg:World. Only real
            worlds (WORLD_NAMES) stay World; the rest become Region (if other
            places are located in it) or Location.
  orgs    - generic collectives ("gangs", "half-orcs", "members of Tyr's
            faith") were typed Organization. Drop the Organization type
            (they stay Actors, actorKind creature).
  retype  - the ontology now has Settlement and Structure; generic Location
            nodes are reclassified by name (castle, inn, smithy, farm, ... ->
            Structure; keyed "47. Bedroom"/"area 3" -> Room; village/town/
            city -> Settlement) and by what they contain (>=3 Rooms ->
            Structure, >=3 Structures -> Settlement).
  parents - infer rpg:locatedIn for places with no parent, from (1) a
            container named in the entity's own source-section heading, then
            (2) an explicit "in/within/inside <Name>" in its own text. Parent
            type is constrained (Room->Location, Location->Region/Location,
            Region->World/Region) and ambiguous matches are skipped.

Order matters: junk, names, worlds, orgs, retype, parents.

Usage:
    python cleanup_graph.py                  # dry run, all steps
    python cleanup_graph.py --apply          # write changes
    python cleanup_graph.py --steps parents  # subset
Snapshot the triplestore first (GET /store as n-quads); there is no undo.
"""
import argparse
import re
from collections import Counter, defaultdict

from kg import KnowledgeGraphManager, PREFIX, strip_commentary, escape_literal
from mapper import is_generic_group, WORLD_NAMES

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
NS = "http://example.org/rpg-ontology#"

def local(uri: str) -> str:
    return uri.split("#")[-1]


def rows(kg, query: str) -> list:
    bindings = kg.execute_sparql(PREFIX + query).get("results", {}).get("bindings", [])
    return [{k: v["value"] for k, v in b.items()} for b in bindings]


def despace(text: str) -> str:
    """PDF text often has letter-spaced headings ("T h e M o o n s e a");
    compare with all whitespace/punctuation removed."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def update(kg, body: str) -> None:
    kg.execute_sparql(PREFIX + body, is_update=True)


# --------------------------------------------------------------------- worlds
# Geographic areas the extractor over-promoted to World; they are Regions.
REGION_NAMES = {"faerûn", "faerun", "evermeet", "lantan", "nimbral", "moonshaes", "hordelands", "great sea",
                "trackless sea", "sunset mountains", "great rift", "moonsea ride", "north ride", "landrise",
                "yellow snake pass", "tuigan invasion"}


def step_worlds(kg, apply: bool) -> None:
    worlds = rows(kg, "SELECT ?e ?n WHERE { ?e a rpg:World ; rpg:name ?n }")
    existing = defaultdict(set)
    for r in rows(kg, "SELECT ?n ?t WHERE { ?e a ?t ; rpg:name ?n . FILTER(?t IN (rpg:Region, rpg:Landmark)) }"):
        existing[r["n"].lower()].add(local(r["t"]))
    # Curated regions that a later retype may have misfiled as Settlement/Structure/Landmark.
    misfiled = [r for r in rows(kg, "SELECT ?e ?n ?t WHERE { ?e a ?t ; rpg:name ?n . "
                                     "FILTER(?t IN (rpg:Landmark, rpg:Settlement, rpg:Structure)) }")
                if r["n"].lower() in REGION_NAMES]
    for r in misfiled:
        print(f"  fix type: {r['n']!r} {local(r['t'])} -> Region")
        if apply:
            update(kg, f"DELETE {{ <{r['e']}> a rpg:{local(r['t'])} }} INSERT {{ <{r['e']}> a rpg:Region }} "
                       f"WHERE {{ <{r['e']}> a rpg:{local(r['t'])} }}")
    print(f"\n[worlds] {len(worlds)} World nodes")
    for w in worlds:
        key = w["n"].lower()
        if key in WORLD_NAMES:
            print(f"  keep World: {w['n']}")
            continue
        new = "Region" if key in REGION_NAMES or "Region" in existing[key] else "Landmark"
        print(f"  {w['n']!r} -> {new}")
        if apply:
            uri = f"<{w['e']}>"
            update(kg, f"DELETE {{ {uri} a rpg:World }} INSERT {{ {uri} a rpg:{new} }} WHERE {{ {uri} a rpg:World }}")


# ----------------------------------------------------------------------- orgs
def step_orgs(kg, apply: bool) -> None:
    orgs = rows(kg, "SELECT ?e ?n WHERE { ?e a rpg:Organization ; rpg:name ?n }")
    demote = [o for o in orgs if is_generic_group(o["n"])]
    print(f"\n[orgs] {len(demote)} of {len(orgs)} Organizations look generic")
    for o in demote:
        print(f"  demote: {o['n']!r}")
        if apply:
            uri = f"<{o['e']}>"
            update(kg, f"DELETE {{ {uri} a rpg:Organization . {uri} rpg:actorKind ?k }} "
                       f"INSERT {{ {uri} rpg:actorKind \"creature\" }} "
                       f"WHERE {{ {uri} a rpg:Organization . OPTIONAL {{ {uri} rpg:actorKind ?k }} }}")
    place_names = {r["n"].lower() for r in rows(kg, "SELECT ?n WHERE { ?e a ?t ; rpg:name ?n . "
                   "FILTER(?t IN (rpg:Landmark, rpg:Region, rpg:Room)) }")}
    clash = [o["n"] for o in orgs if o not in demote and o["n"].lower() in place_names]
    if clash:
        print(f"  note: {len(clash)} Organizations share a name with a place (left alone): {clash[:15]}")


# ----------------------------------------------------------------------- junk
JUNK_NAME = r"^(null|none|n/a|unknown|nil)\\b"


def step_junk(kg, apply: bool) -> None:
    """The extractor sometimes emits "null (no affiliation stated...)" as an
    entity name. Delete those nodes and every edge touching them."""
    junk = rows(kg, f'SELECT ?e ?n WHERE {{ ?e rpg:name ?n . FILTER(REGEX(?n, "{JUNK_NAME}", "i")) }}')
    print(f"\n[junk] {len(junk)} entities named null/none/unknown")
    for j in junk[:10]:
        print(f"  {j['n'][:80]!r}")
    if apply:
        for j in junk:
            uri = f"<{j['e']}>"
            update(kg, f"DELETE WHERE {{ {uri} ?p ?o }} ; DELETE WHERE {{ ?s ?p {uri} }}")


# ----------------------------------------------------------------------- names
def step_names(kg, apply: bool) -> None:
    """Strip commentary the extractor appended to names ("X (implied by the
    passage...)"). Collisions with a clean twin are then merged by
    `resolve_duplicates.py --exact`."""
    ents = rows(kg, 'SELECT ?e ?n WHERE { ?e rpg:name ?n . FILTER(CONTAINS(?n, "(")) }')
    fix = [(e["e"], e["n"], strip_commentary(e["n"])) for e in ents if strip_commentary(e["n"]) != e["n"]]
    print(f"\n[names] {len(fix)} names carry commentary")
    for _, old, new in fix[:8]:
        print(f"  {old[:70]!r} -> {new!r}")
    if apply:
        for uri, old, new in fix:
            update(kg, f'DELETE {{ <{uri}> rpg:name "{escape_literal(old)}" }} '
                       f'INSERT {{ <{uri}> rpg:name "{escape_literal(new)}" }} '
                       f'WHERE {{ <{uri}> rpg:name "{escape_literal(old)}" }}')


# --------------------------------------------------------------------- deities
DEITY_HUB = 25  # a place "hosting" this many gods is context leakage, not worship
SETTING_REGION = "Faerûn"


def step_deities(kg, apply: bool) -> None:
    """Deities were given rpg:locatedIn from leaked "current location"
    context (67 gods "in" the High Forest, 56 "in" the Great Rift...). Convert
    to rpg:worshippedIn, drop hub places entirely, and tie every deity to the
    setting-wide region."""
    edges = rows(kg, "SELECT ?d ?p ?pn WHERE { ?d a rpg:Deity ; rpg:locatedIn ?p . ?p rpg:name ?pn }")
    per_place = defaultdict(set)
    for e in edges:
        per_place[e["pn"].lower().replace("'", "")].add(e["d"])
    hubs = {n for n, ds in per_place.items() if len(ds) >= DEITY_HUB}
    drop = [e for e in edges if e["pn"].lower().replace("'", "") in hubs]
    convert = [e for e in edges if e not in drop]
    region = rows(kg, f'SELECT ?e WHERE {{ ?e a ?t ; rpg:name "{SETTING_REGION}" . FILTER(?t IN (rpg:Region, rpg:Settlement)) }}')
    deities = [r["d"] for r in rows(kg, "SELECT DISTINCT ?d WHERE { ?d a rpg:Deity }")]
    print(f"\n[deities] {len(edges)} locatedIn edges on {len(deities)} deities; hub places {sorted(hubs)}")
    print(f"  drop {len(drop)} hub edges, convert {len(convert)} to worshippedIn, "
          f"link {len(deities)} deities to {SETTING_REGION if region else '(no Faerun node found!)'}")
    if apply:
        for e in drop + convert:
            update(kg, f"DELETE DATA {{ <{e['d']}> rpg:locatedIn <{e['p']}> }}")
        for e in convert:
            update(kg, f"INSERT DATA {{ <{e['d']}> rpg:worshippedIn <{e['p']}> }}")
        if region:
            for i in range(0, len(deities), 100):
                triples = "\n".join(f"<{d}> rpg:worshippedIn <{region[0]['e']}> ." for d in deities[i:i + 100])
                update(kg, f"INSERT DATA {{\n{triples}\n}}")


# --------------------------------------------------------------------- retype
ROOM_NAME = re.compile(
    r"^\W*\d+[A-Za-z]?\s*[.:)]|^(area|room|chamber|level)\s*#?\d|"
    r"\b(bedroom|guardroom|guard room|pen|gallery|roof|cells?|cellar|stairs?|staircase|stairway|"
    r"corridor|passage|floor|deck|vault|closet|wardrobe|pantry|kitchen|landing|foyer|antechamber)$",
    re.IGNORECASE,
)
STRUCTURE_NAME = re.compile(
    r"\b(castle|keep|tower|inn|tavern|temple|shrine|smithy|forge|shop|store|house|home|farm|mill|windmill|"
    r"manor|mansion|fort|fortress|stables?|barracks|crypts?|tomb|church|abbey|monastery|guildhall|"
    r"palace|library|lodge|tannery|bakery|brewery|lighthouse|cottage|hut|cabin|warehouse|theater|theatre)\b",
    re.IGNORECASE,
)
SETTLEMENT_NAME = re.compile(r"\b(village|town|city|hamlet|port)\b", re.IGNORECASE)


VAGUE_PLACE = re.compile(r"^(the\s+)?(city|town|village|tower|castle|keep|house)$|^(various|rest of|tunnels?\b)", re.IGNORECASE)


def classify_place(name: str):
    if re.search(r"[,>]", name) or (VAGUE_PLACE.search(name.strip()) and name.strip().lower() != "keep"):
        return None
    if ROOM_NAME.search(name.strip()):
        return "Room"
    if STRUCTURE_NAME.search(name):
        return "Structure"
    if SETTLEMENT_NAME.search(name):
        return "Settlement"
    return None


def step_retype(kg, apply: bool) -> None:
    generic = rows(kg, "SELECT ?e ?n WHERE { ?e a rpg:Landmark ; rpg:name ?n }")
    # Nodes typed only as plain Location: skip any that carry another place type.
    other = {r["e"] for r in rows(kg, "SELECT ?e WHERE { ?e a ?t . FILTER(?t IN (rpg:Region, rpg:World, rpg:Settlement, "
                                      "rpg:Structure, rpg:Room)) }")}
    generic = [g for g in generic if g["e"] not in other]
    child_edges = defaultdict(list)
    for r in rows(kg, "SELECT ?c ?p WHERE { ?c rpg:locatedIn ?p }"):
        child_edges[r["p"]].append(r["c"])
    current = {r["e"]: local(r["t"]) for r in rows(
        kg, "SELECT ?e ?t WHERE { ?e a ?t . FILTER(?t IN (rpg:Region, rpg:World, rpg:Settlement, rpg:Structure, rpg:Room, rpg:Landmark)) }")}

    new = {}
    for g in generic:
        t = classify_place(g["n"])
        if t:
            new[g["e"]] = t
    # Containment: >=3 Room children -> Structure; >=3 Structure children -> Settlement.
    for _ in range(2):
        for g in generic:
            e = g["e"]
            if e in new and new[e] != "Structure":
                continue
            kids = [new.get(c, current.get(c)) for c in child_edges.get(e, [])]
            if kids.count("Structure") >= 3 and new.get(e) != "Settlement" and (e not in new or new[e] == "Structure"):
                # a keyworded Structure (castle) with outbuildings stays a Structure
                if e not in new:
                    new[e] = "Settlement"
            elif e not in new and kids.count("Room") >= 3:
                new[e] = "Structure"
    names = {g["e"]: g["n"] for g in generic}
    print(f"\n[retype] {len(new)} of {len(generic)} plain Locations reclassified {dict(Counter(new.values()))}")
    for e, t in list(new.items())[:50]:
        print(f"  {names[e]!r} -> {t}")
    if len(new) > 50:
        print(f"  ... and {len(new) - 50} more")
    if apply:
        for e, t in new.items():
            uri = f"<{e}>"
            update(kg, f"DELETE {{ {uri} a rpg:Landmark }} INSERT {{ {uri} a rpg:{t} }} WHERE {{ {uri} a rpg:Landmark }}")


# -------------------------------------------------------------------- parents
# What a child of each type may be located in.
ALLOWED_PARENT = {
    "Region": {"World", "Region"},
    "Settlement": {"Region"},
    "Structure": {"Settlement", "Region", "Structure"},
    "Landmark": {"Region", "Settlement", "Structure", "Landmark"},
    "Room": {"Structure", "Settlement", "Landmark"},
}
# When a heading names several candidate containers, prefer the tightest
# fit for the child (a Room goes in the Structure, not the village around it).
PARENT_PREFERENCE = {
    "Room": ["Structure", "Settlement", "Landmark"],
    "Structure": ["Settlement", "Region", "Structure"],
    "Settlement": ["Region"],
    "Landmark": ["Region", "Settlement", "Structure", "Landmark"],
}
PLACE_TYPES = ("World", "Region", "Settlement", "Structure", "Landmark", "Room")


def step_parents(kg, apply: bool) -> None:
    places = rows(kg, "SELECT ?e ?n ?t WHERE { ?e a ?t ; rpg:name ?n . "
                      "FILTER(?t IN (rpg:World, rpg:Region, rpg:Settlement, rpg:Structure, rpg:Landmark, rpg:Room)) }")
    name = {p["e"]: p["n"] for p in places}
    ptype = {p["e"]: local(p["t"]) for p in places}
    parent = {r["c"]: r["p"] for r in rows(kg, "SELECT ?c ?p WHERE { ?c rpg:locatedIn ?p }") if r["c"] in name}
    sections = defaultdict(set)
    for r in rows(kg, "SELECT ?e ?s WHERE { ?e rpg:foundInSection ?s }"):
        if r["e"] in name:
            sections[r["e"]].add(r["s"])
    text = defaultdict(str)
    for r in rows(kg, "SELECT ?e ?v WHERE { ?e ?p ?v . FILTER(?p IN (rpg:gmDescription, rpg:physicalAppearance, rpg:loreContext)) }"):
        if r["e"] in name:
            text[r["e"]] += " " + r["v"]

    orphans = [e for e in name if e not in parent and ptype[e] in ALLOWED_PARENT]
    print(f"\n[parents] {len(orphans)} places without a parent")

    # candidate containers, de-duplicated by name; longest names first so a
    # specific "Dragon Coast" wins over a short "Coast".
    kids = Counter(parent.values())
    keylike = re.compile(r"^\W*\d|^(key to|area\b|stairs?\b)|:|\b(floor|level)\b", re.IGNORECASE)
    vague = {"hills", "realms", "therealms", "thecity", "city", "thenorth", "north", "theride", "ride", "thevast", "cavern", "cave", "tower", "castle", "dungeon", "thetower", "thecastle"}

    def is_container(e: str) -> bool:
        key = despace(name[e])
        if len(key) < 5 or key in vague or keylike.search(name[e]):
            return False
        return ptype[e] in ("World", "Region", "Settlement", "Structure") and (
            ptype[e] in ("World", "Region", "Settlement") or kids[e] >= 1) or kids[e] >= 2

    containers = sorted({despace(name[e]): e for e in name if is_container(e)}.items(),
                        key=lambda kv: -len(kv[0]))

    def creates_cycle(child: str, new_parent: str) -> bool:
        seen, cur = set(), new_parent
        while cur and cur not in seen:
            if cur == child:
                return True
            seen.add(cur)
            cur = parent.get(cur)
        return False

    def allowed(child: str, cand: str) -> bool:
        return (cand != child and despace(name[cand]) != despace(name[child])
                and ptype[cand] in ALLOWED_PARENT[ptype[child]] and not creates_cycle(child, cand))

    region_keys = {despace(name[e]).removeprefix("the") for e in name if ptype[e] in ("Region", "World")}
    proposals = []
    for e in orphans:
        choice, reason = None, ""
        # Garbled multi-place extractions ("Impiltur, the Vast") and Locations
        # that duplicate a Region's name aren't safe to parent.
        if re.search(r"[,>]", name[e]) or (ptype[e] == "Landmark" and despace(name[e]).removeprefix("the") in region_keys):
            continue
        if ptype[e] == "Landmark" and kids[e] >= 8:  # big container: likely a mistyped region
            continue
        # 1. a container named in the entity's own source-section heading
        #    ("Key to Scornubel", "The Village of Shadowdale").
        #    Not for Regions: gazetteer sections list many sibling regions.
        for sec in sections.get(e, ()) if ptype[e] != "Region" else ():
            sec_key = despace(sec)
            hits = [(k, c) for k, c in containers if k in sec_key and allowed(e, c)]
            # take the tightest-fitting container type that the heading names
            for want in PARENT_PREFERENCE.get(ptype[e], ()):
                preferred = [(k, c) for k, c in hits if ptype[c] == want]
                if preferred:
                    hits = preferred
                    break
            if hits:
                longest = hits[0][0]
                top = {c for k, c in hits if k == longest}
                if len(top) == 1:
                    choice, reason = top.pop(), "section heading"
                    break
        # 2. explicit "in/within/inside <Name>" in the entity's own text.
        if not choice and ptype[e] != "Room" and text.get(e):
            found = {}
            for k, c in containers:
                if allowed(e, c) and re.search(r"\b(?:in|within|inside)\s+(?:the\s+)?" + re.escape(name[c]) + r"(?!\w)",
                                               text[e], re.IGNORECASE):
                    found[c] = len(k)
            if found:
                best = max(found.values())
                top = [c for c, n in found.items() if n == best]
                if len(top) == 1:
                    choice, reason = top[0], "own text"
        if choice:
            proposals.append((e, choice, reason))
            parent[e] = choice

    # Tighten existing links: a Room parented straight to a Settlement, whose
    # source heading names a Structure inside it, belongs to that Structure.
    moves = []
    structure_containers = [(k, c) for k, c in containers if ptype[c] == "Structure"]
    for e in name:
        if ptype[e] != "Room" or parent.get(e) not in name or ptype[parent[e]] != "Settlement":
            continue
        for sec in sections.get(e, ()):
            sec_key = despace(sec)
            hits = [(k, c) for k, c in structure_containers if k in sec_key and allowed(e, c)]
            if hits:
                longest = hits[0][0]
                top = {c for k, c in hits if k == longest}
                if len(top) == 1:
                    moves.append((e, parent[e], top.pop()))
                    break
    print(f"  re-homing {len(moves)} Rooms from a Settlement to the Structure named in their heading")
    for c, old, new in moves[:15]:
        print(f"  {name[c]!r}: {name[old]!r} -> {name[new]!r}")

    by_reason = Counter(r for _, _, r in proposals)
    print(f"  proposing {len(proposals)} locatedIn links {dict(by_reason)}")
    for c, p, reason in proposals[:60]:
        print(f"  {name[c]!r} -> {name[p]!r}   [{reason}]")
    if len(proposals) > 60:
        print(f"  ... and {len(proposals) - 60} more")
    if apply:
        for c, old, new in moves:
            update(kg, f"DELETE DATA {{ <{c}> rpg:locatedIn <{old}> }} ; INSERT DATA {{ <{c}> rpg:locatedIn <{new}> }}")
    if apply and proposals:
        for i in range(0, len(proposals), 200):
            triples = "\n".join(f"<{c}> rpg:locatedIn <{p}> ." for c, p, _ in proposals[i:i + 200])
            update(kg, f"INSERT DATA {{\n{triples}\n}}")


STEPS = {"deities": step_deities, "junk": step_junk, "names": step_names, "worlds": step_worlds, "orgs": step_orgs, "retype": step_retype, "parents": step_parents}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--steps", nargs="+", choices=list(STEPS), default=list(STEPS))
    args = ap.parse_args()
    kg = KnowledgeGraphManager()
    print("APPLYING" if args.apply else "DRY RUN")
    for s in args.steps:  # order matters: worlds before parents
        STEPS[s](kg, args.apply)


if __name__ == "__main__":
    main()
