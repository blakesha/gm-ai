"""Pre-fetch graph context for the GM agent.

Small local models are unreliable at deciding *what* to query: they pick the
wrong same-named node, stop after one lookup, or paraphrase loosely. So before
the model runs, we spot the entities the player named, pull each one's full
record (descriptions, source page, containers, contents, events, members) and
hand that over as a ready-made block. The model can still call tools for more.
"""
import re
from collections import defaultdict

from kg import PREFIX

NS = "http://example.org/rpg-ontology#"
LOW_PRIORITY = {"Scene", "HistoricalEvent", "PlotHook", "SourceDocument", "Trigger", "HiddenInfo", "ReadAloudText"}
DESCRIPTIVE = ("physicalAppearance", "gmDescription", "loreContext")
SKIP_OUT = {"type", "name", "alias", "foundIn", "foundInSection", "foundOnPage", *DESCRIPTIVE}
MIN_SCORE = 70.0
MAX_ENTITIES = 6
MAX_PER_PHRASE = 4
MAX_LIST = 12
MAX_TEXT = 900

QUESTION_WORDS = {"what", "who", "where", "when", "why", "how", "tell", "describe", "explain", "show", "list",
                  "give", "find", "is", "are", "was", "were", "do", "does", "did", "can", "could", "would",
                  "should", "the", "a", "an", "about", "me", "i", "we", "you", "my", "our", "please", "know",
                  "which", "there", "any", "some", "of", "in", "on", "to", "and", "or", "it", "its", "with"}
CAP_PHRASE = re.compile(
    r"[A-Z][\w'’\-]*(?:\s+(?:of|the|de|du|von|van|in|and|on)\s+[A-Z][\w'’\-]*|\s+[A-Z][\w'’\-]*)*")


def candidate_phrases(text: str) -> list:
    """Proper-name-looking phrases from the message, longest first. Falls back
    to the message minus question words when nothing is capitalised."""
    phrases = []
    for m in CAP_PHRASE.finditer(text):
        words = m.group(0).split()
        while words and words[0].lower() in QUESTION_WORDS:  # drop leading "What"/"Tell"
            words.pop(0)
        if words:
            phrases.append(" ".join(words))
    if not phrases:
        words = [w for w in re.findall(r"[\w'’\-]+", text) if w.lower() not in QUESTION_WORDS]
        if 0 < len(words) <= 4:
            phrases.append(" ".join(words))
    seen, out = set(), []
    for p in sorted(phrases, key=len, reverse=True):
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def pick_entities(kg, text: str, graph: str = None) -> list:
    """Every top-scoring, non-bookkeeping node per phrase. Several nodes often
    share one name (a Room and a Structure both called "Old Skull Inn"), and
    each carries different facts, so all of them go into the context."""
    chosen, used = [], set()
    for phrase in candidate_phrases(text):
        results = [r for r in kg.search_entities(phrase, None, 12, graph=graph) if r["score"] >= MIN_SCORE]
        if not results:
            continue
        top = results[0]["score"]
        near = [r for r in results if r["score"] >= top - 1e-6 and r["type"] not in LOW_PRIORITY]
        if not near:  # only bookkeeping nodes matched: fall back to the best one
            near = results[:1]
        near.sort(key=lambda r: r.get("has_description", False), reverse=True)
        for r in near[:MAX_PER_PHRASE]:
            if r["uri"] not in used and len(chosen) < MAX_ENTITIES:
                used.add(r["uri"])
                chosen.append(r)
        if len(chosen) >= MAX_ENTITIES:
            break
    return chosen


def _full(uri: str) -> str:
    return f"<{NS}{uri.split(':', 1)[-1]}>"


def _short(value: str) -> str:
    return value.split("#")[-1]


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rstrip() + "..."


def _rows(kg, query: str) -> list:
    bindings = kg.execute_sparql(PREFIX + query).get("results", {}).get("bindings", [])
    return [{k: v["value"] for k, v in b.items()} for b in bindings]


def _both(pattern: str, graph: str = None, mark: bool = False) -> str:
    """`pattern` over canon (default graph), plus the adventure graph if given.
    With mark=True the adventure branch also binds ?src so callers can tell them apart."""
    if not graph:
        return f"{{ {pattern} }}"
    bind = ' BIND("adv" AS ?src)' if mark else ""
    return f"{{ {pattern} }} UNION {{ GRAPH <{graph}> {{ {pattern} }}{bind} }}"


def _name_of(var: str, graph: str = None) -> str:
    return f"OPTIONAL {{ {_both(f'{var} rpg:name ?{var[1:]}name', graph)} }}"


def entity_block(kg, entity: dict, graph: str = None) -> str:
    e = _full(entity["uri"])
    is_adv = entity["uri"].split(":", 1)[-1].startswith("A_")
    tag = "  [ADVENTURE - non-canon, written by the author]" if is_adv else ""
    lines = [f"## {entity['name']}  ({entity['type']}, uri {entity['uri']}){tag}"]

    out = _rows(kg, f"SELECT ?p ?o ?on WHERE {{ {_both(f'{e} ?p ?o', graph)} "
                    f"OPTIONAL {{ {_both('?o rpg:name ?on', graph)} }} }}")
    by_pred = defaultdict(list)
    for r in out:
        by_pred[_short(r["p"])].append(r)
    for pred in DESCRIPTIVE:
        seen = set()
        for r in by_pred.get(pred, []):
            if r["o"] not in seen:
                seen.add(r["o"])
                lines.append(f"- {pred}: {_clip(r['o'])}")
    aliases = sorted({r["o"] for r in by_pred.get("alias", [])})
    if aliases:
        lines.append(f"- also called: {', '.join(aliases[:8])}")
    for pred, items in by_pred.items():
        if pred in SKIP_OUT or pred in ("isCanon",):
            continue
        vals = sorted({r.get("on") or _short(r["o"]) for r in items})
        lines.append(f"- {pred}: {', '.join(v[:80] for v in vals[:MAX_LIST])}" + (f" (+{len(vals) - MAX_LIST} more)" if len(vals) > MAX_LIST else ""))
    pages = sorted({r["o"] for r in by_pred.get("foundOnPage", [])}, key=lambda x: int(x) if x.isdigit() else 0)
    src = sorted({_short(r["o"]) for r in by_pred.get("foundIn", [])})
    if pages or src:
        lines.append(f"- source: {', '.join(s.replace('SourceDocument_', '').replace('_', ' ') for s in src)}"
                     + (f", page(s) {', '.join(pages)}" if pages else ""))

    inc = _rows(kg, f"""SELECT ?p ?t ?n ?src WHERE {{ {_both(f'?s ?p {e} . ?s a ?t', graph, mark=True)}
                        OPTIONAL {{ {_both('?s rpg:name ?n', graph)} }}
                        FILTER(?p != rpg:foundIn) }}""")
    grouped = defaultdict(lambda: defaultdict(set))
    for r in inc:
        label = (r.get("n") or "?") + (" [adventure]" if r.get("src") else "")
        grouped[_short(r["p"])][_short(r["t"])].add(label)
    for pred, by_type in grouped.items():
        # Organizations/Deities are also typed Actor: show each name once, under the specific type
        specific = set().union(*(names for t, names in by_type.items() if t in ("Organization", "Deity")), set())
        if "Actor" in by_type:
            by_type["Actor"] = by_type["Actor"] - specific
            if not by_type["Actor"]:
                del by_type["Actor"]
        for t, names in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            names = sorted(n[:80] for n in names)
            lines.append(f"- {t} via {pred} ({len(names)}): {', '.join(names[:MAX_LIST])}"
                         + (f", +{len(names) - MAX_LIST} more" if len(names) > MAX_LIST else ""))
    return "\n".join(lines)


def retrieve_context(kg, user_text: str, graph: str = None) -> str:
    """Return a prompt section with each named entity's record, or ''."""
    try:
        entities = pick_entities(kg, user_text, graph)
        if not entities:
            return ""
        blocks = [entity_block(kg, e, graph) for e in entities]
    except Exception as exc:  # retrieval must never break the chat
        print(f"context retrieval failed: {exc}")
        return ""
    print("context entities:", [(e["name"], e["type"]) for e in entities])
    note = ("Records marked [ADVENTURE] (and names tagged [adventure]) are the author's own non-canon material for "
            "the adventure being written; everything else is canon. " if graph else "")
    return ("# Retrieved Graph Context (automatic)\n" + note +
            "These records were pulled from the knowledge graph for the entities the player just named. "
            "Base your answer on them. State what they contain and what they don't; do not add outside detail. "
            "Several records may share a name; use all of them. When describing a place, include the named people "
            "and notable objects recorded there, and do not add furnishings, smells, sounds or other details that "
            "the records do not give. "
            "Run further queries only if the player asks for something not covered here.\n\n"
            + "\n\n".join(blocks))
