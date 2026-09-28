Extract discrete events that occurred during actual play from this session log passage. This is for recording what actually happened at the table (which trap fired, which NPC died, what the party decided) — distinct from in-world lore (HistoricalEvent) or a module's planned encounter (Scene).

Output ONLY valid JSON array (empty array if none found):
[
  {
    "event_name": "<short label for the event>",
    "description": "<what happened>",
    "location_name": "<location where it happened, if mentioned, else null>",
    "participating_actors": ["<names present/involved>"],
    "outcome": "<result, e.g. 'party defeated the ogre', 'trap triggered, no casualties', else null>",
    "is_resolved": true | false,
    "caused_event": "<name of a later event this directly caused, if evident, else null>",
    "depends_on_event": "<name of an earlier event this depended on, if evident, else null>"
  }
]
