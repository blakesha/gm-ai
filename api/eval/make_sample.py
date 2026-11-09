"""Sample content-rich chunks from library PDFs for hand/Claude labelling.
  python eval/make_sample.py out.json N  "rel.pdf::type" ...
Skips the first/last 8% of chunks (front/back matter), needs >=120 words."""
import json, random, sys
sys.path.insert(0, "/app")
import pymupdf, chunker
out, n, specs = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
docs = []
for spec in specs:
    rel, dtype = spec.rsplit("::", 1)
    cs = list(chunker.chunk_document(pymupdf.open("/library/D&D/" + rel)))
    lo, hi = int(len(cs) * .08), int(len(cs) * .92)
    pool = [(i, c) for i, c in enumerate(cs[lo:hi], lo) if len(c["text"].split()) >= 120]
    random.Random(42).shuffle(pool)
    pick = sorted(pool[:n])
    docs.append({"pdf": rel, "type": dtype, "chunks": [
        {"id": f"{rel.split('/')[-1][:20]}#{i}", "index": i, "section": c["section"], "text": c["text"], "gold": None}
        for i, c in pick]})
json.dump(docs, open(out, "w"), indent=1, ensure_ascii=False)
print([(d["type"], len(d["chunks"])) for d in docs])
