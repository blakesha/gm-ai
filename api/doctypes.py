"""Document types: which extraction passes run for each, plus auto-detection.

`auto` is resolved per file (from its path and first pages) when the job
runs, and the resolved type is stored on the job as `detected_type`.
"""
import re

ALL_PASSES = {"location", "scene", "statblock", "readaloud", "trigger", "lore", "actor", "item", "plot_hook"}

PASSES = {
    "module": ALL_PASSES,
    "campaign": ALL_PASSES,
    "bestiary": {"statblock", "actor", "lore"},
    "rules": {"lore", "item", "statblock"},
    "accessory": {"lore", "item", "statblock"},
    "setting": {"location", "actor", "lore", "plot_hook"},
    "session_transcript": set(),  # handled separately (session_event pass only)
}
TYPES = ["auto"] + list(PASSES)

# (type, keywords) - filename/folder hits weigh 3, first-pages text hits weigh 1
KEYWORDS = [
    ("bestiary", ["bestiary", "monster manual", "monstrous", "menagerie", "creature compendium", "monsters of"]),
    ("rules", ["player's handbook", "players handbook", "core rulebook", "rulebook", "dungeon master's guide",
               "gamemaster's guide", "game master's guide", "rules", "handbook", "quickstart rules"]),
    ("accessory", ["screen", "accessory", "deck", "tarot", "map pack", "character sheet", "cheat sheet",
                   "cards", "tokens", "supplement", "toolkit"]),
    ("setting", ["setting", "gazetteer", "atlas", "world guide", "sourcebook", "almanac", "lands of"]),
    ("campaign", ["campaign", "adventure path", "saga", "chronicle", "season"]),
    ("session_transcript", ["transcript", "session recap", "session log", "session notes"]),
    ("module", ["module", "adventure", "scenario", "dungeon", "one-shot", "oneshot", "level "]),
]


def detect(path: str, first_pages_text: str) -> str:
    name = path.lower()
    text = first_pages_text.lower()
    best, best_score = "module", 0
    for doc_type, words in KEYWORDS:
        score = sum(3 for w in words if re.search(rf"\b{re.escape(w)}", name))
        score += sum(1 for w in words if re.search(rf"\b{re.escape(w)}", text))
        if score > best_score:
            best, best_score = doc_type, score
    return best


# Wording hints added to the context of every extraction call for that type.
HINTS = {
    "module": "An adventure: areas are keyed by number (e.g. Room #N); read-aloud text is boxed or italic; stat blocks follow encounters.",
    "campaign": "A multi-part campaign: areas, NPCs, factions and set-piece encounters; read-aloud text is boxed or italic; stat blocks follow encounters.",
    "bestiary": "A monster reference: each entry describes one creature or creature family. The creature is an actor and its numbers are a stat block; habitat, society and ecology text describes that creature, not named people or places.",
    "setting": "A setting guide: places, organizations and notable people are described in prose, with few stat blocks and almost no read-aloud boxes. Shops, taverns and landmarks listed under a town are separate locations.",
    "rules": "Rules or reference text: extract only specific named items, creatures or table entries; rules concepts are not actors, items or locations.",
    "accessory": "A player/GM accessory (tables, cards, screens, sheets): extract only specific named items, creatures or table entries; rules concepts are not actors, items or locations.",
}
