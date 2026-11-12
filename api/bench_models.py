"""Compare Ollama models on the same document chunks.

  python bench_models.py "<rel.pdf>::module" "<rel.pdf>::setting" ... -- model1 model2 [--chunks 10]

Runs every module extraction pass on evenly-spaced chunks per model and
reports speed, JSON/schema failures, whether the prompt hit the context
limit, and entity counts. Raw outputs go to /tmp/bench_<model>.json so
you can eyeball quality. Runs inside the agent-api container.
"""
import json, sys, time, requests
import pymupdf
import chunker, main

PASSES_LIST = ["readaloud", "trigger", "lore", "actor", "item", "plot_hook"]  # + "statblock" added by caller
PASSES_OBJ = ["location", "scene"]


def unload(model):
    requests.post(main.LLM_ENDPOINT.replace("/api/chat", "/api/generate"),
                  json={"model": model, "keep_alive": 0}, timeout=120)


def run_model(model, chunks, passes):
    out = {"chunks": [], "fail": 0, "calls": 0, "stats": {}}
    t0 = time.time()
    for i, text in enumerate(chunks):
        res = {}
        for name in passes:
            out["calls"] += 1
            try:
                r = main.call_ollama(main._load_prompt(name), main._load_schema(name), text, "", model, out["stats"])
                res[name] = r
            except Exception as e:
                out["fail"] += 1
                res[name] = {"_error": str(e)[:200]}
        out["chunks"].append(res)
        print(f"  {model} chunk {i+1}/{len(chunks)} done ({time.time()-t0:.0f}s)", flush=True)
    out["seconds"] = time.time() - t0
    return out


def counts(out):
    c = {}
    for res in out["chunks"]:
        for n, r in res.items():
            if isinstance(r, list):
                c[n] = c.get(n, 0) + len(r)
            elif isinstance(r, dict) and r.get("location_name") or isinstance(r, dict) and r.get("scene_name"):
                c[n] = c.get(n, 0) + 1
    return c


if __name__ == "__main__":
    import doctypes
    args = sys.argv[1:]
    n = 10
    if "--chunks" in args:
        k = args.index("--chunks"); n = int(args[k + 1]); del args[k:k + 2]
    # docs look like "path.pdf::type"; models are everything after "--"
    sep = args.index("--")
    docs = [a.rsplit("::", 1) for a in args[:sep]]
    models = args[sep + 1:]
    samples = []
    for path, dtype in docs:
        doc = pymupdf.open(str(main.LIBRARY_DIR / "D&D" / path))
        allc = [c["text"] for c in chunker.chunk_document(doc)]
        step = max(1, len(allc) // n)
        samples.append((path.split("/")[-1][:28], dtype, allc[::step][:n]))
        print(f"{path}: {len(allc)} chunks, testing {len(samples[-1][2])} as {dtype}")
    rows = []
    for m in models:
        for label, dtype, chunks in samples:
            print(f"== {m} on {label} ({dtype})", flush=True)
            enabled = doctypes.PASSES[dtype]
            out = run_model(m, chunks, [p for p in PASSES_OBJ + PASSES_LIST + ["statblock"] if p in enabled])
            json.dump(out, open(f"/tmp/bench_{m.replace(':','_')}_{dtype}.json", "w"), indent=1)
            eval_s = out["stats"].get("eval_duration", 0) / 1e9 or 1
            rows.append((m, label, dtype, out["seconds"] / len(chunks), out["stats"].get("eval_count", 0) / eval_s,
                         out["fail"], out["calls"], out["stats"].get("max_prompt_tokens", 0), counts(out)))
        unload(m)
    print(f"\n{'model':16} {'doc':28} {'type':8} {'s/chunk':>7} {'tok/s':>6} {'fail':>8} {'maxprompt':>9}  entities")
    for m, label, dtype, spc, tps, f, calls, mp, c in rows:
        print(f"{m:16} {label:28} {dtype:8} {spc:7.1f} {tps:6.1f} {f:>3}/{calls:<4} {mp:9}  {sum(c.values())} {c}")
