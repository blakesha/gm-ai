"""Pass 3: deterministic mapping of flat Pass-2 JSON into SPARQL triples.

No LLM calls happen here. This is the only module that knows about ontology
predicates and URIs, which keeps the Pass 2 prompts free of RDF syntax —
their job is to find and label content, not remember how it's stored.
"""
import re

from kg import escape_literal, strip_commentary

# Fields the Pass 2d stat-block prompt (and the stat_parser regex fast path)
# both use, mapped onto the ontology's StatBlock properties.
STAT_FIELD_TO_PREDICATE = {
    "armor_class": "armorClass",
    "hit_points": "hitPoints",
    "hit_dice": "hitDice",
    "thac0": "thac0",
    "num_attacks": "numAttacks",
    "damage": "damage",
    "movement_rate": "movementRate",
    "saving_throw": "savingThrow",
    "quality": "quality",
    "defense": "defenseRating",
}


def _literal_triple(uri: str, predicate: str, value) -> str:
    return f'{uri} rpg:{predicate} """ {escape_literal(value)} """ .'


def _add_descriptive(kg, seen: set, uri: str, predicate: str, value, triples: list) -> None:
    """Add a physicalAppearance/gmDescription literal only if this entity
    doesn't already have one. A recurring location/actor/item gets touched by
    many chunks across a module; writing every re-mention's paraphrase as a
    fresh triple turns the graph into a wall of near-duplicate text instead of
    one canonical description. `seen` catches sibling entries within the same
    ingest batch, which the store itself won't reflect yet."""
    key = (uri, predicate)
    if key in seen:
        return
    seen.add(key)
    if kg.has_property(uri, predicate):
        return
    triples.append(_literal_triple(uri, predicate, value))


def _bool_triple(uri: str, predicate: str, value: bool) -> str:
    return f"{uri} rpg:{predicate} {'true' if value else 'false'} ."


def _clean_int(value) -> str:
    return re.sub(r"[^0-9]", "", str(value))


class TrackingKG:
    """Wraps a KnowledgeGraphManager and remembers every entity URI resolved
    through it, so provenance can be attached to exactly the entities one
    chunk touched (new or already existing) without threading a list through
    every map_* function."""

    def __init__(self, kg):
        self._kg = kg
        self.touched = set()

    def get_or_create_uri(self, name, rdf_type, triples):
        uri = self._kg.get_or_create_uri(name, rdf_type, triples)
        self.touched.add(uri)
        return uri

    def __getattr__(self, attr):
        return getattr(self._kg, attr)


def map_provenance(kg, source: dict, triples: list) -> None:
    """Link every entity this chunk touched to its SourceDocument, page and
    section. `source` = {"title", "page", "section"}."""
    if not source or not source.get("title"):
        return
    touched = set(kg.touched)
    doc_uri = kg.get_or_create_uri(source["title"], "SourceDocument", triples)
    for uri in sorted(touched - {doc_uri}):
        triples.append(f"{uri} rpg:foundIn {doc_uri} .")
        if source.get("page"):
            triples.append(f"{uri} rpg:foundOnPage {int(source['page'])} .")
        if source.get("section"):
            triples.append(f'{uri} rpg:foundInSection "{escape_literal(source["section"])}" .')


_NULLISH = {"", "null", "none", "n/a", "na", "unknown", "nil", "-", "undefined"}


def _clean_hint(value):
    """Models sometimes emit the string "null"/"none" instead of JSON null."""
    v = (str(value).strip() if value is not None else "")
    # also "null (not stated in this passage)" - a nullish word plus commentary
    if v.lower() in _NULLISH or re.match(r"^(null|none|n/a|unknown|nil)\b", v, re.IGNORECASE):
        return None
    return strip_commentary(v)


ACTOR_KINDS = {"npc", "creature", "group", "deity"}
KIND_CLASS = {"group": "Organization", "deity": "Deity"}
ITEM_TYPES = {"magic_item", "treasure", "weapon", "armor", "tool", "container", "document", "consumable", "other"}
GENERIC_EXIT_TARGETS = {"north", "south", "east", "west", "up", "down", "above", "below", "corridor", "passage",
                        "hall", "hallway", "door", "stairs", "stair", "room", "area", "tunnel", "exit", "outside"}

LOCATION_TYPES = {"World", "Region", "Settlement", "Structure", "Landmark", "Room"}
# Lookup order when a bare place name (a hint) must resolve to an existing
# node: prefer the specific types over the generic fallback.
PLACE_LOOKUP_ORDER = ["Structure", "Settlement", "Region", "Room", "World", "Landmark"]


def place_uri(kg, name, triples, rdf_type: str = "Landmark") -> str:
    """Resolve a place name against ALL place types, so a hint like "Castle
    Grimstead" reuses the existing Structure instead of minting a duplicate
    plain Landmark. Falls back to creating `rdf_type`."""
    existing = kg.get_existing_place_uri(str(name).strip(), PLACE_LOOKUP_ORDER)
    if existing:
        if hasattr(kg, "touched"):
            kg.touched.add(existing)
        return existing
    return kg.get_or_create_uri(name, rdf_type, triples)
WORLD_NAMES = {"abeir-toril", "toril"}

# Collectives like "gangs" or "half-orcs" are not factions.
GENERIC_GROUP = re.compile(
    r"^(the\s+)?(members|citizens|followers|servants|worshipers|worshippers|priests|"
    r"soldiers|guards|patrols?|various|several|local|other|many|some)\b|"
    r"\b(gangs?|groups?|patrols|bands|folk|people|adventurers|creatures|"
    r"humans|elves|dwarves|gnomes|halflings|orcs|half-orcs|half-elves|goblins|kobolds|"
    r"giants|trolls|ogres)$",
    re.IGNORECASE,
)


def is_generic_group(name: str) -> bool:
    return not re.search(r"[A-Z]", name) or bool(GENERIC_GROUP.search(name.strip()))


def _location_type(location: dict) -> str:
    location_type = location.get("location_type") or "Landmark"
    if location_type == "Location":  # legacy label from older prompts
        location_type = "Landmark"
    if location_type not in LOCATION_TYPES:
        return "Landmark"
    # The extractor over-tags World; only a real world name may be one.
    if location_type == "World" and (location.get("location_name") or "").lower() not in WORLD_NAMES:
        return "Region"
    return location_type


def map_locations(locations: list, kg, triples: list) -> None:
    seen = set()
    for location in locations:
        name = location.get("location_name")
        if not name:
            continue
        location_uri = place_uri(kg, name, triples, _location_type(location))

        if location.get("physical_appearance"):
            _add_descriptive(kg, seen, location_uri, "physicalAppearance", location["physical_appearance"], triples)
        if location.get("gm_description"):
            _add_descriptive(kg, seen, location_uri, "gmDescription", location["gm_description"], triples)
        if location.get("lore_context"):
            _add_descriptive(kg, seen, location_uri, "loreContext", location["lore_context"], triples)
        if location.get("parent_location_hint"):
            parent_uri = place_uri(kg, location["parent_location_hint"], triples)
            triples.append(f"{location_uri} rpg:locatedIn {parent_uri} .")
        for exit_ in location.get("exits") or []:
            target = _clean_hint(exit_.get("leads_to")) or ""
            if not target or target.lower() in GENERIC_EXIT_TARGETS or target.lower() == name.strip().lower():
                continue
            target_uri = place_uri(kg, target, triples)
            triples.append(f"{location_uri} rpg:connectsTo {target_uri} .")


def map_scenes(scenes: list, kg, triples: list) -> None:
    """The Scene is the hub an encounter is built around - actor/item/trigger/
    readaloud/lore extraction each carry a `scene_context` name forward and
    link into it (see map_actors, map_items, map_readaloud, map_triggers,
    map_lore); this just gives the Scene node itself a description and, for
    a module with a sequence of set-piece encounters, a caused/dependsOn
    chain to the scenes before/after it."""
    seen = set()
    for scene in scenes:
        name = scene.get("scene_name")
        if not name:
            continue
        scene_uri = kg.get_or_create_uri(name, "Scene", triples)
        if scene.get("gm_description"):
            _add_descriptive(kg, seen, scene_uri, "gmDescription", scene["gm_description"], triples)
        if scene.get("caused_event"):
            target_uri = kg.get_or_create_uri(scene["caused_event"], "Scene", triples)
            triples.append(f"{scene_uri} rpg:caused {target_uri} .")
        if scene.get("depends_on_event"):
            target_uri = kg.get_or_create_uri(scene["depends_on_event"], "Scene", triples)
            triples.append(f"{scene_uri} rpg:dependsOn {target_uri} .")


def map_hosts_scene(location: dict, scene: dict, kg, triples: list) -> None:
    """Links the Location a chunk describes to the Scene it frames, when a
    chunk yields both. Kept separate from map_locations/map_scenes since
    those each operate on one side's list without knowing the other exists."""
    location_name = location.get("location_name")
    scene_name = scene.get("scene_name")
    if not location_name or not scene_name:
        return
    location_uri = kg.get_or_create_uri(location_name, _location_type(location), triples)
    scene_uri = kg.get_or_create_uri(scene_name, "Scene", triples)
    triples.append(f"{location_uri} rpg:hostsScene {scene_uri} .")


def map_readaloud(entries: list, kg, triples: list) -> None:
    seen = set()
    for entry in entries:
        text = entry.get("text")
        if not text:
            continue
        label = entry.get("label") or text[:60]
        text_uri = kg.get_or_create_uri(label, "ReadAloudText", triples)
        _add_descriptive(kg, seen, text_uri, "physicalAppearance", text, triples)
        if entry.get("is_player_facing") is not None:
            triples.append(_bool_triple(text_uri, "isPlayerFacing", bool(entry["is_player_facing"])))
        if entry.get("scene_context"):
            scene_uri = kg.get_or_create_uri(entry["scene_context"], "Scene", triples)
            triples.append(f"{scene_uri} rpg:containsText {text_uri} .")
        elif entry.get("located_in_hint"):
            # No Scene exists to hang this off (e.g. a location-only sourcebook
            # with no discrete encounters) - fall back to linking the text
            # straight to its Location rather than leaving it an orphan node.
            location_uri = place_uri(kg, entry["located_in_hint"], triples)
            triples.append(f"{text_uri} rpg:locatedIn {location_uri} .")


def map_triggers(entries: list, kg, triples: list) -> None:
    seen = set()
    for entry in entries:
        condition = entry.get("trigger_condition")
        if not condition:
            continue
        trigger_label = entry.get("label") or condition[:80]
        trigger_uri = kg.get_or_create_uri(trigger_label, "Trigger", triples)

        notes = []
        if entry.get("check_type"):
            notes.append(f"Check: {entry['check_type']}")
        if entry.get("cross_reference"):
            notes.append(f"Cross-reference: {entry['cross_reference']}")
        if notes:
            _add_descriptive(kg, seen, trigger_uri, "gmDescription", " | ".join(notes), triples)

        if entry.get("dc_target") is not None:
            clean_dc = _clean_int(entry["dc_target"])
            if clean_dc:
                triples.append(f"{trigger_uri} rpg:dcTarget {clean_dc} .")

        reveals_content = entry.get("reveals_content")
        if reveals_content:
            reveal_type = entry.get("reveals_type") or "HiddenInfo"
            mapped_type = "Actor" if reveal_type == "ActorSpawn" else reveal_type
            if mapped_type not in {"HiddenInfo", "ReadAloudText", "Actor", "Event"}:
                mapped_type = "HiddenInfo"
            reveals_label = entry.get("reveals_label") or reveals_content[:80]
            reveal_uri = kg.get_or_create_uri(reveals_label, mapped_type, triples)
            predicate = "physicalAppearance" if mapped_type == "ReadAloudText" else "gmDescription"
            _add_descriptive(kg, seen, reveal_uri, predicate, reveals_content, triples)
            triples.append(f"{trigger_uri} rpg:reveals {reveal_uri} .")

        if entry.get("scene_context"):
            scene_uri = kg.get_or_create_uri(entry["scene_context"], "Scene", triples)
            triples.append(f"{scene_uri} rpg:containsTrigger {trigger_uri} .")
        elif entry.get("located_in_hint"):
            # Same fallback as map_readaloud - no Scene to attach to, so link
            # the Trigger straight to its Location instead of orphaning it.
            location_uri = place_uri(kg, entry["located_in_hint"], triples)
            triples.append(f"{trigger_uri} rpg:locatedIn {location_uri} .")


def map_statblocks(entries: list, kg, triples: list, default_game_system: str) -> None:
    seen = set()
    for entry in entries:
        actor_name = entry.get("actor_name")
        if not actor_name:
            continue
        statblock_uri = kg.get_or_create_uri(f"{actor_name} StatBlock", "StatBlock", triples)

        game_system = entry.get("game_system") or default_game_system
        if game_system:
            system_uri = kg.get_or_create_uri(game_system, "GameSystem", triples)
            triples.append(f"{statblock_uri} rpg:designedFor {system_uri} .")

        if entry.get("raw_stats"):
            _add_descriptive(kg, seen, statblock_uri, "gmDescription", entry["raw_stats"], triples)

        for field, predicate in STAT_FIELD_TO_PREDICATE.items():
            if entry.get(field):
                triples.append(_literal_triple(statblock_uri, predicate, entry[field]))

        actor_uri = kg.get_or_create_uri(actor_name, "Actor", triples)
        triples.append(f"{actor_uri} rpg:usesStatBlock {statblock_uri} .")


def map_lore(entries: list, kg, triples: list) -> None:
    seen = set()
    for entry in entries:
        name = entry.get("event_name")
        if not name:
            continue
        event_uri = kg.get_or_create_uri(name, "HistoricalEvent", triples)
        if entry.get("description"):
            _add_descriptive(kg, seen, event_uri, "gmDescription", entry["description"], triples)
        if entry.get("caused_event"):
            target_uri = kg.get_or_create_uri(entry["caused_event"], "HistoricalEvent", triples)
            triples.append(f"{event_uri} rpg:caused {target_uri} .")
        if entry.get("depends_on_event"):
            target_uri = kg.get_or_create_uri(entry["depends_on_event"], "HistoricalEvent", triples)
            triples.append(f"{event_uri} rpg:dependsOn {target_uri} .")
        if entry.get("related_location"):
            location_uri = place_uri(kg, entry["related_location"], triples)
            triples.append(f"{event_uri} rpg:occurredAt {location_uri} .")
        for actor_name in entry.get("related_actors") or []:
            actor_uri = kg.get_or_create_uri(actor_name, "Actor", triples)
            triples.append(f"{actor_uri} rpg:participatedIn {event_uri} .")
        if entry.get("scene_context"):
            scene_uri = kg.get_or_create_uri(entry["scene_context"], "Scene", triples)
            triples.append(f"{scene_uri} rpg:containsEvent {event_uri} .")


def map_actors(entries: list, kg, triples: list) -> None:
    seen = set()
    for entry in entries:
        name = _clean_hint(entry.get("actor_name"))
        if not name:
            continue
        actor_uri = kg.get_or_create_uri(name, "Actor", triples)
        kind = entry.get("actor_kind")
        if kind == "group" and is_generic_group(name):
            kind = "creature"
        if kind in ACTOR_KINDS and ("kind", actor_uri) not in seen:
            seen.add(("kind", actor_uri))
            if not kg.has_property(actor_uri, "actorKind"):
                triples.append(f'{actor_uri} rpg:actorKind "{kind}" .')
            if kind in KIND_CLASS:
                triples.append(f"{actor_uri} a rpg:{KIND_CLASS[kind]} .")
        affiliation = _clean_hint(entry.get("affiliation_hint")) or ""
        if affiliation and affiliation.lower() != name.strip().lower():
            group_uri = kg.get_or_create_uri(affiliation, "Actor", triples)
            triples.append(f"{actor_uri} rpg:memberOf {group_uri} .")
        if entry.get("physical_appearance"):
            _add_descriptive(kg, seen, actor_uri, "physicalAppearance", entry["physical_appearance"], triples)
        if entry.get("gm_description"):
            _add_descriptive(kg, seen, actor_uri, "gmDescription", entry["gm_description"], triples)
        if entry.get("lore_context"):
            _add_descriptive(kg, seen, actor_uri, "loreContext", entry["lore_context"], triples)
        if entry.get("located_in_hint"):
            location_uri = place_uri(kg, entry["located_in_hint"], triples)
            # A god is not "located" anywhere; a place hint on a deity means
            # worship/following there (and is often just leaked context).
            predicate = "worshippedIn" if kind == "deity" else "locatedIn"
            triples.append(f"{actor_uri} rpg:{predicate} {location_uri} .")
        if entry.get("scene_context"):
            scene_uri = kg.get_or_create_uri(entry["scene_context"], "Scene", triples)
            triples.append(f"{scene_uri} rpg:spawns {actor_uri} .")


def map_items(entries: list, kg, triples: list) -> None:
    seen = set()
    for entry in entries:
        name = entry.get("item_name")
        if not name:
            continue
        item_uri = kg.get_or_create_uri(name, "Item", triples)
        item_type = entry.get("item_type")
        if item_type in ITEM_TYPES and ("itype", item_uri) not in seen:
            seen.add(("itype", item_uri))
            if not kg.has_property(item_uri, "itemType"):
                triples.append(f'{item_uri} rpg:itemType "{item_type}" .')
        if entry.get("physical_appearance"):
            _add_descriptive(kg, seen, item_uri, "physicalAppearance", entry["physical_appearance"], triples)
        if entry.get("gm_description"):
            _add_descriptive(kg, seen, item_uri, "gmDescription", entry["gm_description"], triples)
        if entry.get("lore_context"):
            _add_descriptive(kg, seen, item_uri, "loreContext", entry["lore_context"], triples)
        if entry.get("located_in_hint"):
            # rpg:locatedIn documents Actor as its domain, but RDFS domain/range
            # here is documentation, not an enforced constraint - reusing it for
            # Item avoids a near-duplicate predicate for the same relationship.
            location_uri = place_uri(kg, entry["located_in_hint"], triples)
            triples.append(f"{item_uri} rpg:locatedIn {location_uri} .")
        if entry.get("owner_actor_hint"):
            owner_uri = kg.get_or_create_uri(entry["owner_actor_hint"], "Actor", triples)
            triples.append(f"{owner_uri} rpg:owns {item_uri} .")
        if entry.get("scene_context"):
            scene_uri = kg.get_or_create_uri(entry["scene_context"], "Scene", triples)
            triples.append(f"{scene_uri} rpg:contains {item_uri} .")


def map_session_events(entries: list, kg, triples: list) -> None:
    seen = set()
    for entry in entries:
        name = entry.get("event_name")
        if not name:
            continue
        event_uri = kg.get_or_create_uri(name, "SessionEvent", triples)
        if entry.get("description"):
            _add_descriptive(kg, seen, event_uri, "gmDescription", entry["description"], triples)
        if entry.get("outcome"):
            triples.append(_literal_triple(event_uri, "outcome", entry["outcome"]))
        if entry.get("is_resolved") is not None:
            triples.append(_bool_triple(event_uri, "isResolved", bool(entry["is_resolved"])))
        if entry.get("location_name"):
            location_uri = place_uri(kg, entry["location_name"], triples)
            triples.append(f"{event_uri} rpg:occurredAt {location_uri} .")
        if entry.get("caused_event"):
            target_uri = kg.get_or_create_uri(entry["caused_event"], "SessionEvent", triples)
            triples.append(f"{event_uri} rpg:caused {target_uri} .")
        if entry.get("depends_on_event"):
            target_uri = kg.get_or_create_uri(entry["depends_on_event"], "SessionEvent", triples)
            triples.append(f"{event_uri} rpg:dependsOn {target_uri} .")
        for actor_name in entry.get("participating_actors") or []:
            actor_uri = kg.get_or_create_uri(actor_name, "Actor", triples)
            triples.append(f"{actor_uri} rpg:participatedIn {event_uri} .")


def map_plot_hooks(entries: list, kg, triples: list) -> None:
    seen = set()
    for entry in entries:
        name = entry.get("hook_name")
        if not name:
            continue
        hook_uri = kg.get_or_create_uri(name, "PlotHook", triples)
        if entry.get("hook_description"):
            _add_descriptive(kg, seen, hook_uri, "gmDescription", entry["hook_description"], triples)
        if entry.get("status"):
            triples.append(_literal_triple(hook_uri, "status", entry["status"]))

        if entry.get("hook_giver_hint"):
            giver_uri = kg.get_or_create_uri(entry["hook_giver_hint"], "Actor", triples)
            triples.append(f"{hook_uri} rpg:hookGiver {giver_uri} .")
        if entry.get("involves_actor_hint"):
            actor_uri = kg.get_or_create_uri(entry["involves_actor_hint"], "Actor", triples)
            triples.append(f"{hook_uri} rpg:involvesActor {actor_uri} .")
        if entry.get("involves_location_hint"):
            location_uri = place_uri(kg, entry["involves_location_hint"], triples)
            triples.append(f"{hook_uri} rpg:involvesLocation {location_uri} .")
        if entry.get("involves_item_hint"):
            item_uri = kg.get_or_create_uri(entry["involves_item_hint"], "Item", triples)
            triples.append(f"{hook_uri} rpg:involvesItem {item_uri} .")
