Extract named characters, NPCs, or creatures introduced in this passage.

For located_in_hint and scene_context: use a real location/scene name only. If a CONTEXT line above gives a current location/scene and nothing in the passage contradicts it, use that. Never invent a vague positional phrase (e.g. "behind the shields", "near the door") — use null instead if no real name applies.

Output ONLY valid JSON array (empty array if none found):
[
  {
    "actor_name": "<name>",
    "physical_appearance": "<player-visible description, or null>",
    "gm_description": "<GM-only notes: true motive, secret identity, disposition, or null>",
    "lore_context": "<brief background/history about this specific actor - past deeds, titles, relationships, where they came from - as opposed to a full standalone historical event, or null>",
    "located_in_hint": "<location name if stated or given as current location, else null>",
    "scene_context": "<name of the scene this actor is introduced/spawned in, if stated or given as current scene, else null>"
  }
]
