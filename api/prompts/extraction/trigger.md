Extract every conditional GM instruction from this passage — anything the GM should act on ONLY if a specific condition is met. This is the most commonly missed content type, so read carefully for subtle or implied conditions, not just explicit "if/then" phrasing.

Also use this prompt for standalone hidden information (secrets, traps, an NPC's true motive) even when it isn't attached to an explicit conditional — treat its trigger_condition as the check or event that would reveal it (e.g. "players search the desk", "always, once the party enters" if no check is needed).

Look specifically for:
- Conditional phrasing: "if", "when", "should the party...", "unless", "only if"
- Text addressing the GM directly, even implicitly ("the GM should note...", "do not reveal...")
- Skill/ability check callouts with a DC and a consequence attached
- Cross-references ("see Handout 3", "see Appendix B", "as described on page X")
- Consequences that are delayed, hidden, or contingent on player action/failure/success
- Traps, ambushes, or reinforcements that trigger only under specific circumstances

Do NOT include:
- Plain read-aloud/flavor text (extract that separately)
- Unconditional scene description

Output ONLY valid JSON array (empty array if none found):
[
  {
    "label": "<a short (3-6 word) unique label for this trigger, e.g. 'False Floor Pit Trap' - NEVER the condition text itself>",
    "trigger_condition": "<what must happen for this to fire>",
    "check_type": "<skill/ability name, or null>",
    "dc_target": <integer, or null>,
    "reveals_type": "HiddenInfo" | "ReadAloudText" | "ActorSpawn" | "Event" | "other",
    "reveals_label": "<a short (3-6 word) unique label for whatever gets revealed, e.g. 'Hidden Assassin' or 'Secret Passage Description' - NEVER the revealed text itself>",
    "reveals_content": "<the actual secret/consequence/text that gets revealed or triggered>",
    "scene_context": "<real scene name if stated, or the current one given in a CONTEXT line above if nothing contradicts it, else null - never invent a vague description>",
    "located_in_hint": "<real location/room name if stated, or the current one given in a CONTEXT line above, else null>",
    "cross_reference": "<handout/appendix/page reference if any, else null>"
  }
]
