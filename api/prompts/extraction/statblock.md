Extract mechanical stat block data from this passage. In addition to the raw text, pull out any of the structured fields below if you can clearly identify them from the passage — leave a field null rather than guessing at a value that isn't there.

Output ONLY valid JSON array (empty array if none found):
[
  {
    "actor_name": "<name>",
    "game_system": "<system name if identifiable, e.g. D&D 5e, Pathfinder 2e, else null>",
    "armor_class": "<AC value/description, or null>",
    "hit_points": "<HP value/description, or null>",
    "hit_dice": "<HD value, or null>",
    "thac0": "<THAC0 value, or null>",
    "num_attacks": "<number/description of attacks, or null>",
    "damage": "<damage expression, or null>",
    "movement_rate": "<movement rate, or null>",
    "saving_throw": "<saving throw info, or null>",
    "raw_stats": "<the full stat block content as a single string, preserved as-is>"
  }
]
