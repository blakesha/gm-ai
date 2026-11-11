Extract named characters, NPCs, or creatures introduced in this passage.

For located_in_hint and scene_context: use a real location/scene name only. If a CONTEXT line above gives a current location/scene and nothing in the passage contradicts it, use that. Never invent a vague positional phrase (e.g. "behind the shields", "near the door") — use null instead if no real name applies.

actor_kind: "npc" = a specific individual; "creature" = a monster or animal type or pack; "group" = a NAMED guild, army, faction, cult, family or other organization (e.g. "Zhentarim", "Red Ravens") - never a race, species or generic crowd ("elves", "guards", "gangs"; use "creature" for those or omit); "deity" = a god or power.

For a deity (actor_kind "deity"), set located_in_hint only if the passage says where the god is worshipped or has a shrine; otherwise null. Do NOT copy the current-location context onto a deity.

NAMING: use the person's or group's bare proper name, the same way every time: "Elminster", not "Lord Elminster" or "Elminster the Sage"; "Harpers", not "The Harpers (an organization)". Keep a title only if it is part of the name itself ("Lord of Waterdeep" as an office is fine; "Lord Mourngrym" is just "Mourngrym"). Never append commentary or explanations to a name, and never output "null" or "none" as a name or as affiliation_hint - use JSON null.

Output ONLY valid JSON array (empty array if none found):
[
  {
    "actor_name": "<name>",
    "actor_kind": "npc" | "creature" | "group" | "deity",
    "affiliation_hint": "<name of the organization or deity this actor belongs to or serves, if stated, else null>",
    "physical_appearance": "<player-visible description, or null>",
    "gm_description": "<GM-only notes: true motive, secret identity, disposition, or null>",
    "lore_context": "<brief background/history about this specific actor - past deeds, titles, relationships, where they came from - as opposed to a full standalone historical event, or null>",
    "located_in_hint": "<location name if stated or given as current location, else null>",
    "scene_context": "<name of the scene this actor is introduced/spawned in, if stated or given as current scene, else null>"
  }
]
