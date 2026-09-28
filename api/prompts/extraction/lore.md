Extract background lore or historical events referenced in this passage — information about the past, not the current physical scene.

Output ONLY valid JSON array (empty array if none found):
[
  {
    "event_name": "<short label for the event>",
    "description": "<what happened>",
    "caused_event": "<name of an event this caused, if mentioned, else null>",
    "depends_on_event": "<name of an event this depended on, if mentioned, else null>",
    "related_location": "<location name if mentioned, else null>",
    "related_actors": ["<names mentioned, if any>"]
  }
]
