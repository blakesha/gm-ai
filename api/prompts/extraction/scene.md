Extract the encounter or narrative beat this passage frames - the SCENE itself, distinct from the physical location it takes place in (that's extracted separately). A scene is what happens here: an encounter, negotiation, ambush, discovery, or story beat that ties together whichever actors, items, read-aloud text, triggers, and background events are present in this passage.

Only emit a scene if this passage actually frames a discrete encounter/beat. A passage that's purely background lore, or a bare location description with nothing happening in it, should produce "scene_name": null - never invent a scene where nothing is actually staged.

Output ONLY valid JSON:
{
  "scene_name": "<name or best-guess label for this encounter/beat, or null if this passage doesn't frame a discrete scene>",
  "gm_description": "<GM notes on the encounter as a whole - pacing, tactics, stakes, why it matters - as opposed to the location's own structural secrets, or null>",
  "caused_event": "<name of a later scene this directly leads into, if evident, else null>",
  "depends_on_event": "<name of an earlier scene this depends on or follows from, if evident, else null>"
}
