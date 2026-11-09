Extract named items, weapons, keys, or magical objects mentioned in this passage.

Scenery, furniture, architecture and room fixtures (walls, doors, tables, shelves, altars that are part of the room) are NOT items unless the passage treats them as notable or portable objects. Coins or gems are items only when given as a specific treasure find, not as a general mention.

For located_in_hint and scene_context: use a real location/scene name only. If a CONTEXT line above gives a current location/scene and nothing in the passage contradicts it, use that. Never invent a vague positional phrase (e.g. "on the shelf", "near the door") — use null instead if no real name applies.

Output ONLY valid JSON array (empty array if none found):
[
  {
    "item_name": "<name>",
    "item_type": "magic_item" | "treasure" | "weapon" | "armor" | "tool" | "container" | "document" | "consumable" | "other",
    "physical_appearance": "<player-visible description, or null>",
    "gm_description": "<GM-only notes: true properties, curses, mechanical effects, or null>",
    "lore_context": "<brief background/history about this specific item - who made it, who owned it, why it matters - as opposed to a full standalone historical event, or null>",
    "located_in_hint": "<location name if stated or given as current location, else null>",
    "owner_actor_hint": "<name of an actor carrying/guarding this item, if mentioned, else null>",
    "scene_context": "<name of the scene this item is found in, if stated or given as current scene, else null>"
  }
]
