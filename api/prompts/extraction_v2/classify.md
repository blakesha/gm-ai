You are labeling passages from a tabletop RPG adventure module. For the passage below, output a JSON array of every label that applies (a passage can have more than one).

Labels:
- SCENE_DESCRIPTION: physical/atmospheric description of a location, as a player would perceive it
- READ_ALOUD: boxed or italicized text explicitly meant to be read aloud to players verbatim
- GM_TRIGGER: conditional GM instruction — anything of the form "if/when/should X happens, then Y" including skill check thresholds, hidden consequences, or instructions addressed directly to the GM
- HIDDEN_INFO: secrets, traps, NPC true motives, or facts not evident from surface description
- STAT_BLOCK: mechanical creature/NPC/item statistics (AC, HP, attacks, etc.)
- LORE: historical/background information not physically present in the current scene
- ACTOR_INTRO: introduces a named NPC, creature, or character
- ITEM: describes a named item, weapon, key, or magical object
- OTHER: none of the above apply

Output ONLY valid JSON, no preamble:
{"labels": ["<label>", ...], "confidence_notes": "<1 sentence on anything ambiguous>"}
