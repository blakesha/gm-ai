Extract location description from this passage. Split it into what a PLAYER would perceive (physical_appearance) versus what only the GM knows (gm_description) — traps, monster intentions, structural secrets, anything requiring a check or event to reveal.

Output ONLY valid JSON:
{
  "location_name": "<name or best-guess label>",
  "location_type": "World" | "Region" | "Location" | "Room",
  "physical_appearance": "<player-visible description, or null>",
  "gm_description": "<GM-only knowledge embedded in this passage, or null>",
  "lore_context": "<brief background/history about this specific location - former occupants, past events tied to the place, why it's named as it is - as opposed to a full standalone historical event, or null>",
  "parent_location_hint": "<name of containing region/building if mentioned, else null>"
}
