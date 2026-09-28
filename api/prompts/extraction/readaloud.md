Extract every passage of text in this chunk that is explicitly meant to be read aloud to players verbatim (boxed text, italicized narration, "Read the following:" style callouts).

Output ONLY valid JSON array (empty array if none found):
[
  {
    "label": "<a short (3-6 word) unique label summarizing this passage, e.g. 'Guardroom Entrance Warning' - NEVER the passage text itself or a long quote from it>",
    "text": "<the exact read-aloud text>",
    "scene_context": "<real scene name if stated, or the current one given in a CONTEXT line above if nothing contradicts it, else null - never invent a vague description>",
    "located_in_hint": "<real location/room name if stated, or the current one given in a CONTEXT line above, else null>",
    "is_player_facing": true
  }
]
