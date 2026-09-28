Extract plot hooks from this passage: rumors, bounties, patron requests, faction goals, or other unresolved narrative threads a GM could build a new adventure around. This is distinct from LORE (something that already happened) and from a GM_TRIGGER (a conditional tied to a specific written encounter) - a plot hook is a forward-looking, open-ended opportunity, whether or not the source material ever resolves it.

Look specifically for:
- Rumors or gossip ("some say...", "word has it...")
- A patron or NPC offering work, a bounty, or a reward
- An unexplained disappearance, mystery, or unfinished business
- A faction's stated or implied goal that the party could get pulled into
- Sidebar "adventure hooks" or "rumor table" content

Do NOT include:
- Historical background with no present-day opportunity attached (extract that as LORE)
- A conditional instruction bound to a specific room/encounter (extract that as a GM_TRIGGER)

Output ONLY valid JSON array (empty array if none found):
[
  {
    "hook_name": "<short label>",
    "hook_description": "<the hook itself, in enough detail for a GM to build on>",
    "hook_giver_hint": "<actor who offers or reveals this, if any, else null>",
    "involves_actor_hint": "<key actor/antagonist this hook centers on, if any, else null>",
    "involves_location_hint": "<location this hook points toward, if any, else null>",
    "involves_item_hint": "<item, treasure, or reward tied to this hook, if any, else null>"
  }
]
