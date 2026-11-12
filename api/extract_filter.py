"""Post-extraction precision filters.

Small local models either miss entities (low recall) or, tuned to catch
everything, pad the output with generic nouns and invented names (low
precision). These filters let a high-recall model be used:

1. grounding  - deterministic: a name must actually appear in the passage
                (or its section label); stat blocks need real stat labels;
                bare generic nouns ("coins", "door") are dropped.
2. llm        - one verification call per chunk that lists the candidates
                and removes only those that are clearly not a real entry of
                their category. Unsure -> keep.

Mode comes from EXTRACT_FILTER: "none" | "grounding" | "llm" | "both".
"""
import re
from typing import Callable, Optional

NAME_FIELD = {"locations": "location_name", "actors": "actor_name", "items": "item_name", "statblocks": "actor_name"}

GENERIC = {
    "coin", "gem", "handle", "lock", "door", "wall", "floor", "ceiling", "room", "chamber", "corridor", "hall",
    "area", "passage", "stair", "stairs", "window", "table", "chair", "bench", "shelf", "pc", "party", "player",
    "character", "creature", "monster", "person", "people", "man", "woman", "figure", "it", "he", "she", "they",
    "item", "object", "treasure", "weapon", "armor", "spell", "dm", "gm", "god", "gods", "deity", "darkness",
    "light", "water", "stone", "rock", "dust", "torch", "rope", "key", "chest", "box", "bag",
}
STAT_LABELS = re.compile(r"\b(AC|HP|hp|HD|MV|THAC0|THACO|Dmg|Int|AL|SZ|ML|XP|AT|#AT|SA|SD|STR|DEX|CON|WIS|CHA|Armor Class|Hit Dice|Hit Points)\b")
STAT_MIN_LABELS = 2
STOP = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "at", "by", "with", "from"}


def _norm(s: str) -> str:
    s = re.sub(r"[’'`\x91\x92]", "", (s or "").lower())
    return re.sub(r"[^a-z0-9# ]", " ", s)


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def _tokens(s: str) -> list:
    return [_stem(t) for t in _norm(s).split() if t not in STOP]


def grounded(name: str, haystack_tokens: set, haystack_norm: str, threshold: float = 0.6) -> bool:
    toks = _tokens(name)
    if not toks:
        return False
    if _norm(name).strip() in haystack_norm:
        return True
    hits = sum(1 for t in toks if t in haystack_tokens)
    return hits / len(toks) >= threshold


def apply_grounding(passage: str, section: Optional[str], extracted: dict) -> dict:
    """Mutates and returns `extracted`; returns drop counts in extracted['_dropped']."""
    haystack = f"{passage} {section or ''}"
    h_norm = _norm(haystack)
    h_tokens = {_stem(t) for t in h_norm.split()}
    dropped = []
    for cat, field in NAME_FIELD.items():
        kept = []
        for entry in extracted.get(cat, []):
            name = entry.get(field) or entry.get("name") or ""
            toks = _tokens(name)
            if not toks or (len(toks) == 1 and toks[0] in GENERIC) or not grounded(name, h_tokens, h_norm):
                dropped.append((cat, name))
                continue
            kept.append(entry)
        extracted[cat] = kept
    # a stat block needs real stat labels in the passage, not just a creature name
    if extracted.get("statblocks") and len(STAT_LABELS.findall(passage)) < STAT_MIN_LABELS:
        dropped.extend(("statblocks", e.get("actor_name", "")) for e in extracted["statblocks"])
        extracted["statblocks"] = []
    extracted["_dropped"] = dropped
    return extracted


VERIFY_PROMPT = """You are cleaning up entities extracted from a tabletop RPG module passage. Below are the PASSAGE and a numbered list of CANDIDATES, each tagged with a category. Identify candidates that are clearly WRONG for their category, so they can be removed.

Category rules:
- locations: a named or clearly identifiable place (room, area, building, venue, town, region, landmark). Wrong: furniture/fixtures/architecture (a wall, door, shelf, table), a direction or distance, an object, a creature.
- actors: a specific character, a kind of creature, or a named group/organization that appears in or acts in the passage. Wrong: pronouns, objects, places, spells, abstract nouns, generic words ("creature", "figure", "party", "PCs").
- items: a distinct, notable physical object (treasure, magic item, key prop, weapon, container). Wrong: scenery or room features, coins/gems/gold named only as a generic amount, creatures, deities, places, spells, body parts, duplicates of another candidate.
- statblocks: a creature that has a game stat block (AC, HD, hp, THAC0, attacks...) in the passage. Wrong: a creature only mentioned in prose with no stats.

Be conservative: if a candidate could plausibly be right, keep it. Only list ones that are clearly wrong or exact duplicates of another candidate.

Output ONLY JSON: {"remove": [<candidate numbers to remove>]}"""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"remove": {"type": "array", "items": {"type": "integer"}}},
    "required": ["remove"],
}


def apply_llm_verify(passage: str, extracted: dict, call: Callable,
                     categories=("actors", "items")) -> dict:
    """`call(system_prompt, schema, user_text) -> parsed JSON`."""
    cands = []  # (category, index-in-list, name)
    for cat, field in NAME_FIELD.items():
        if cat not in categories:  # locations/statblocks: verifier removed too many real ones
            continue
        for i, e in enumerate(extracted.get(cat, [])):
            cands.append((cat, i, e.get(field) or e.get("name") or ""))
    if not cands:
        return extracted
    listing = "\n".join(f"{n}. [{cat}] {name}" for n, (cat, _, name) in enumerate(cands, 1))
    result = call(VERIFY_PROMPT, VERIFY_SCHEMA, f'PASSAGE:\n"""\n{passage}\n"""\n\nCANDIDATES:\n{listing}')
    remove = {n for n in (result.get("remove") if isinstance(result, dict) else []) or [] if isinstance(n, int)}
    drop = {(cats, idx) for n, (cats, idx, _) in enumerate(cands, 1) if n in remove}
    for cat in categories:
        extracted[cat] = [e for i, e in enumerate(extracted.get(cat, [])) if (cat, i) not in drop]
    extracted.setdefault("_dropped", []).extend((cats, name) for n, (cats, _, name) in enumerate(cands, 1) if n in remove)
    return extracted
