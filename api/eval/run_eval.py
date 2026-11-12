"""Score extraction against the labelled chunks in eval/gold_*.json.

  python eval/run_eval.py --models qwen2.5:14b qwen3:14b [--no-context] [--limit N]

Runs the real pipeline (main.process_chunk) per chunk, so prompts, pass sets
per document type, section breadcrumb and document brief are all exercised.
Reports micro precision/recall/F1 per category (names matched fuzzily) and
count agreement for read-aloud/trigger. Raw output: /tmp/eval_<model>.json
"""
import argparse, json, re, sys, time
from difflib import SequenceMatcher
sys.path.insert(0, "/app")
import pymupdf
import main, doctypes

SAMPLE = json.load(open("/app/eval/sample.json"))
GOLD = {}
for name in ("module", "setting", "campaign", "bestiary"):
    GOLD.update(json.load(open(f"/app/eval/gold_{name}.json")))

NAME_FIELDS = {"locations": "location_name", "actors": "actor_name", "items": "item_name", "statblocks": "actor_name"}
PASS_FOR = {"locations": "location", "actors": "actor", "items": "item", "statblocks": "statblock",
            "readaloud": "readaloud", "triggers": "trigger"}


def norm(s):
    s = re.sub(r"[’'`]", "", (s or "").lower())
    s = re.sub(r"\b(the|a|an|of)\b", " ", s)
    s = re.sub(r"[^a-z0-9# ]", " ", s)
    toks = [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in s.split()]
    return " ".join(toks)


def same(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    ta, tb = set(a.split()), set(b.split())
    if ta <= tb or tb <= ta:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def match(pred, gold):
    """Greedy one-to-one match; returns (tp, fp, fn)."""
    left = list(gold)
    tp = 0
    for p in pred:
        hit = next((g for g in left if same(p, g)), None)
        if hit is not None:
            left.remove(hit)
            tp += 1
    return tp, len(pred) - tp, len(left)


def extract(model, use_context, limit):
    """Run the pipeline (filters OFF) and return raw per-chunk predictions."""
    main.OLLAMA_MODEL = model
    main.EXTRACT_FILTER = "none"
    main.GLEAN = set()  # raw = first-pass only; follow-up applied separately with --glean
    raw, secs = [], 0.0
    for doc in SAMPLE:
        dtype = doc["type"]
        brief = ""
        if use_context:
            pdf = pymupdf.open("/library/D&D/" + doc["pdf"])
            brief = main.make_brief(pdf, doc["pdf"].split("/")[-1][:-4])
        for c in doc["chunks"][:limit]:
            t0 = time.time()
            ex = main.process_chunk(c["text"], dtype, "AD&D 2nd Edition",
                                    section=c["section"] if use_context else None, brief=brief or None)
            secs += time.time() - t0
            raw.append({"id": c["id"], "type": dtype, "gold": GOLD[c["id"]], "seconds": time.time() - t0,
                        "pred": {"locations": [x.get("location_name") for x in ex["locations"]],
                                 "actors": [x.get("actor_name") for x in ex["actors"]],
                                 "items": [x.get("item_name") for x in ex["items"]],
                                 "statblocks": [x.get("actor_name") or x.get("name") for x in ex["statblocks"]],
                                 "readaloud": len(ex["readaloud"]), "triggers": len(ex["triggers"])}})
        print(f"  {model} {dtype} done", flush=True)
    return raw, secs


def refilter(raw, mode, filter_model):
    """Apply grounding / LLM-verify to saved predictions (names only is all
    the filters need) and return new raw records + seconds spent filtering."""
    import extract_filter as ef
    chunks = {c["id"]: c for d in SAMPLE for c in d["chunks"]}
    out, secs = [], 0.0
    for r in raw:
        c = chunks[r["id"]]
        ex = {cat: [{fld: n} for n in r["pred"][cat] if n] for cat, fld in ef.NAME_FIELD.items()}
        t0 = time.time()
        if mode in ("grounding", "both"):
            ef.apply_grounding(c["text"], c["section"], ex)
        if mode in ("llm", "both"):
            ef.apply_llm_verify(c["text"], ex, lambda sp, sc, ut: main.call_ollama(
                sp, sc, "", "", model=filter_model, raw_user=ut))
        secs += time.time() - t0
        pred = dict(r["pred"])
        for cat, fld in ef.NAME_FIELD.items():
            pred[cat] = [e[fld] for e in ex[cat]]
        out.append({**r, "pred": pred, "dropped": ex.get("_dropped", [])})
    return out, secs


def rerun_and_glean(raw, rerun_cats, glean_cats, model):
    """Re-extract some categories with the CURRENT prompt files and/or run the
    follow-up pass on top of the saved first-pass names. Returns raw records."""
    main.OLLAMA_MODEL = model
    chunks = {c["id"]: c for d in SAMPLE for c in d["chunks"]}
    briefs = {}
    one = {"actors": "actor", "items": "item", "locations": "location"}
    fld = {"actors": "actor_name", "items": "item_name", "locations": "location_name"}
    out, t0 = [], time.time()
    for r in raw:
        c = chunks[r["id"]]
        doc = next(d for d in SAMPLE if any(x["id"] == r["id"] for x in d["chunks"]))
        if doc["pdf"] not in briefs:
            briefs[doc["pdf"]] = main.make_brief(pymupdf.open("/library/D&D/" + doc["pdf"]), doc["pdf"].split("/")[-1][:-4])
        note = main._context_note(None, None, c["section"], briefs[doc["pdf"]])
        pred = dict(r["pred"])
        enabled = doctypes.PASSES[r["type"]]
        for cat in rerun_cats:
            if one[cat] in enabled:
                pred[cat] = [e.get(fld[cat]) for e in main.run_pass2_list(one[cat], c["text"], note)]
        for cat in glean_cats:
            if one[cat] in enabled:
                new = main.glean_pass(one[cat], c["text"], pred[cat], note)
                pred[cat] = pred[cat] + [e.get(fld[cat]) for e in new]
                pred.setdefault("glean_added", {})[cat] = [e.get(fld[cat]) for e in new]
        out.append({**r, "pred": pred})
    return out, time.time() - t0


def score(raw):
    totals = {}
    for r in raw:
        dtype, pred, gold = r["type"], r["pred"], r["gold"]
        enabled = doctypes.PASSES[dtype]
        for cat in NAME_FIELDS:
            if PASS_FOR[cat] not in enabled:
                continue
            tp, fp, fn = match(pred[cat], gold[cat])
            for key in (cat, f"{dtype}:{cat}"):
                t = totals.setdefault(key, [0, 0, 0])
                t[0] += tp; t[1] += fp; t[2] += fn
        for cat in ("readaloud", "triggers"):
            if PASS_FOR[cat] in enabled:
                p, g = pred[cat], gold[cat]
                totals.setdefault(f"count:{cat}", []).append(1.0 if p == g == 0 else min(p, g) / max(p, g))
    return totals


def prf(t):
    tp, fp, fn = t
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def report(tag, totals, extra=""):
    print(f"\n{tag} {extra}")
    print(f"{'category':22} {'P':>5} {'R':>5} {'F1':>5}   (tp/fp/fn)")
    cats = sorted(k for k in totals if not k.startswith("count:") and ":" not in k)
    for k in cats:
        p, r, f = prf(totals[k])
        print(f"{k:22} {p:5.2f} {r:5.2f} {f:5.2f}   {totals[k]}")
    tot = [sum(x) for x in zip(*[totals[k] for k in cats])]
    p, r, f = prf(tot)
    print(f"{'ALL names':22} {p:5.2f} {r:5.2f} {f:5.2f}   {tot}")
    for k in ("count:readaloud", "count:triggers"):
        v = totals.get(k, [])
        if v:
            print(f"{k:22} agreement {sum(v) / len(v):.2f}")
    for k in sorted(k for k in totals if ":" in k and not k.startswith("count:")):
        p, r, f = prf(totals[k])
        print(f"  {k:22} P{p:.2f} R{r:.2f} F1 {f:.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", help="extract with these models (filters off); saves /tmp/eval_<m>_raw.json")
    ap.add_argument("--no-context", action="store_true")
    ap.add_argument("--limit", type=int, default=99)
    ap.add_argument("--refilter", help="saved raw json to re-score with a filter instead of extracting")
    ap.add_argument("--mode", default="both", choices=["none", "grounding", "llm", "both"])
    ap.add_argument("--filter-model", default="qwen2.5:14b")
    ap.add_argument("--rerun", default="", help="comma list of actors,items,locations to re-extract with current prompts")
    ap.add_argument("--glean", default="", help="comma list of categories to run the follow-up pass on")
    ap.add_argument("--model", default="qwen2.5:14b", help="model for --rerun/--glean")
    a = ap.parse_args()
    if a.refilter and (a.rerun or a.glean):
        raw = json.load(open(a.refilter))
        raw, secs = rerun_and_glean(raw, [x for x in a.rerun.split(",") if x], [x for x in a.glean.split(",") if x], a.model)
        if a.mode != "none":
            raw, _ = refilter(raw, a.mode, a.filter_model)
        tag = f"rerun={a.rerun or '-'} glean={a.glean or '-'} filter={a.mode}"
        json.dump(raw, open(f"/tmp/eval_{a.model.replace(':', '_')}_{tag.replace(' ', '_').replace('=', '-').replace(',', '+')}.json", "w"), indent=1)
        report(f"{a.refilter.split('/')[-1]} {tag}", score(raw), f"(+{secs / len(raw):.1f} s/chunk)")
        added = [(r["id"][-12:], r["pred"].get("glean_added")) for r in raw if r["pred"].get("glean_added")]
        print("glean examples:", added[:6])
    elif a.refilter:
        raw = json.load(open(a.refilter))
        if a.mode != "none":
            raw, secs = refilter(raw, a.mode, a.filter_model)
            json.dump(raw, open(a.refilter.replace("_raw", f"_{a.mode}_{a.filter_model.replace(':', '_')}"), "w"), indent=1)
        else:
            secs = 0
        report(f"{a.refilter.split('/')[-1]} + filter={a.mode} by {a.filter_model}", score(raw),
               f"(+{secs / len(raw):.1f} s/chunk filtering)")
    else:
        for m in a.models:
            raw, secs = extract(m, not a.no_context, a.limit)
            json.dump(raw, open(f"/tmp/eval_{m.replace(':', '_')}_raw.json", "w"), indent=1)
            report(f"{m}{' (no ctx)' if a.no_context else ''} [no filter]", score(raw), f"{secs / len(raw):.1f} s/chunk")
