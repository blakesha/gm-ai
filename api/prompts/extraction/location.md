Extract EVERY distinct named location from this passage — each room, area, building, venue, shop, tavern, street, district, town, region or landmark that the passage names or clearly describes as a place. A passage often covers several (a town's list of taverns, a dungeon's adjoining rooms): return one entry for each, not just the main one. Do not return generic features (a wall, a door, a shelf, a table) or places mentioned only in passing as a direction or distance ("two days from X").

For each, split what a PLAYER would perceive (physical_appearance) from what only the GM knows (gm_description) — traps, monster intentions, structural secrets, anything requiring a check or event to reveal. exits: list only connections the passage states (a door, stair, passage or path leading to another NAMED or numbered place); use an empty array if none. Never use a bare direction or "the corridor" as leads_to.

If the passage gives only a name for a place, return the name with null descriptions.

Output ONLY a valid JSON array (empty array if the passage describes no place):
[
  {
    "location_name": "<name as written; for numbered keyed areas keep the key, e.g. \"Room #9A: Living Room\">",
    "location_type": "World" | "Region" | "Location" | "Room",
    "physical_appearance": "<player-visible description, or null>",
    "gm_description": "<GM-only knowledge embedded in this passage, or null>",
    "lore_context": "<brief background/history about this specific location - former occupants, past events tied to the place, why it's named as it is - as opposed to a full standalone historical event, or null>",
    "parent_location_hint": "<name of containing region/building if mentioned, else null>",
    "exits": [ { "leads_to": "<name or key of another place this one directly connects to, e.g. Room #12>", "direction": "<north/south/up/down/etc. or null>" } ]
  }
]
