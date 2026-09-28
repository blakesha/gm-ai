Extract named items, weapons, keys, or magical objects mentioned in this passage.

For located_in_hint and scene_context: use a real location/scene name only. If a CONTEXT line above gives a current location/scene and nothing in the passage contradicts it, use that. Never invent a vague positional phrase (e.g. "on the shelf", "near the door") — use null instead if no real name applies.

Output ONLY valid JSON array (empty array if none found):
[
  {
    "item_name": "<name>",
    "physical_appearance": "<player-visible description, or null>",
    "gm_description": "<GM-only notes: true properties, curses, mechanical effects, or null>",
    "located_in_hint": "<location name if stated or given as current location, else null>",
    "owner_actor_hint": "<name of an actor carrying/guarding this item, if mentioned, else null>",
    "scene_context": "<name of the scene this item is found in, if stated or given as current scene, else null>"
  }
]
