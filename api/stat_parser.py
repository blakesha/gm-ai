"""Deterministic stat-block parsing for known game systems.

Stat blocks in most tabletop modules follow a rigid, system-specific
abbreviation grammar (e.g. AD&D 2e's "AC 5; MV 9; HD 4+4; hp 22; THAC0 17;
#AT 1; Dmg 1d8"). Regex can pull that apart reliably and cheaply, so it runs
before any LLM call. `parse()` returns None when it isn't confident it found
a real stat line for the given system — the caller should then fall back to
the Pass 2d LLM prompt, which captures the same fields plus a raw-text catch-all.
"""
import re

# Field name -> regex whose first group is the value.
ADND2E_FIELD_PATTERNS = {
    "armor_class": r"\bAC\s*[:\-]?\s*([^\n;,]+)",
    "movement_rate": r"\bMV\s*[:\-]?\s*([^\n;,]+)",
    "hit_dice": r"\bHD\s*[:\-]?\s*([^\n;,]+)",
    "hit_points": r"\bhp\s*[:\-]?\s*([^\n;,]+)",
    "thac0": r"\bTHAC0\s*[:\-]?\s*(\d+)",
    "num_attacks": r"#\s*AT\s*[:\-]?\s*([^\n;,]+)",
    "damage": r"\bDmg\s*[:\-]?\s*([^\n;,]+)",
    "saving_throw": r"\b(?:SV|Save)\s*[:\-]?\s*([^\n;,]+)",
}

# "One Page Rules"-style unit stat lines, e.g. "Quality 4+ / Defense 5+ / Tough 3".
OPR_FIELD_PATTERNS = {
    "quality": r"\bQuality\s*[:\-]?\s*(\d\+?)",
    "defense": r"\bDefense\s*[:\-]?\s*(\d\+?)",
    "hit_points": r"\b(?:Tough|Wounds|HP)\s*[:\-]?\s*(\d+)",
}

SYSTEM_PATTERNS = {
    "AD&D 2nd Edition": ADND2E_FIELD_PATTERNS,
    "One Page Rules": OPR_FIELD_PATTERNS,
}

# Below this many matched fields, treat it as noise rather than a real stat
# block and let the LLM take a pass instead.
MIN_FIELDS_FOR_CONFIDENCE = 3


def _guess_actor_name(text: str):
    """The line immediately above a stat line is almost always the creature's
    name, usually in caps or title case with no digits."""
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if 2 <= len(line) <= 40 and not re.search(r"\d", line) and (line.isupper() or line.istitle()):
            return line.title() if line.isupper() else line
        break
    return None


def parse(text: str, game_system: str):
    patterns = SYSTEM_PATTERNS.get(game_system)
    if not patterns:
        return None

    fields = {}
    for field, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            fields[field] = match.group(1).strip().rstrip(".")

    if len(fields) < MIN_FIELDS_FOR_CONFIDENCE:
        return None

    actor_name = _guess_actor_name(text)
    if not actor_name:
        # A shared placeholder name (e.g. "Unknown Creature") would collapse
        # every unrelated monster this happens to on one KG entity - better to
        # fall back to the LLM pass, which has context to actually name it.
        return None

    fields["actor_name"] = actor_name
    fields["game_system"] = game_system
    fields["raw_stats"] = text.strip()
    return fields
