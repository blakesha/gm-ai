#!/usr/bin/env python3
"""One-off backfill so existing data matches what ingestion now writes.

  1. Superclass types: every Room/Region/... also `a rpg:Location`, every
     Scene/HistoricalEvent/SessionEvent also `a rpg:Event`, etc. (kg.SUPERTYPES).
  2. Symmetric edges: rpg:connectsTo asserted in both directions.
  3. Legacy global flags: `rpg:isResolved` removed from PlotHooks (hook
     resolution is now per session, see api/hooks.py).

Applies to the canon (default) graph and every adventure graph. Idempotent.
Usage: scripts/backfill_inference.py [--dry-run]
Env:   SPARQL_QUERY_URL / SPARQL_UPDATE_URL (default http://localhost:7879/...)
"""
import os
import sys

import requests

QUERY_URL = os.getenv("SPARQL_QUERY_URL", "http://localhost:7879/query")
UPDATE_URL = os.getenv("SPARQL_UPDATE_URL", "http://localhost:7879/update")
P = "PREFIX rpg: <http://example.org/rpg-ontology#>\n"
PLACES = ["World", "Region", "Settlement", "Structure", "Room", "Landmark"]
SUPERTYPES = {**{t: "Location" for t in PLACES}, "Organization": "Actor", "Deity": "Actor",
              "HistoricalEvent": "Event", "SessionEvent": "Event", "Scene": "Event",
              "Adventure": "SourceDocument"}


def select(q):
    r = requests.post(QUERY_URL, data={"query": q}, headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def update(q):
    requests.post(UPDATE_URL, data={"update": q}).raise_for_status()


def graphs():
    rows = select("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    return [None] + [r["g"]["value"] for r in rows if r["g"]["value"].startswith("urn:rpg:adventure:")]


def wrap(g, body):
    return f"GRAPH <{g}> {{ {body} }}" if g else body


def count(g, body):
    return int(select(P + f"SELECT (COUNT(*) AS ?n) WHERE {{ {wrap(g, body)} }}")[0]["n"]["value"])


def main():
    dry = "--dry-run" in sys.argv
    for g in graphs():
        label = g or "canon (default graph)"
        for sub, sup in SUPERTYPES.items():
            body = f"?x a rpg:{sub} . FILTER NOT EXISTS {{ ?x a rpg:{sup} }}"
            n = count(g, body)
            if n:
                print(f"[{label}] {sub} -> {sup}: {n}")
                if not dry:
                    update(P + (f"INSERT {{ {wrap(g, f'?x a rpg:{sup}')} }} WHERE {{ {wrap(g, body)} }}"))
        body = "?a rpg:connectsTo ?b . FILTER NOT EXISTS { ?b rpg:connectsTo ?a }"
        n = count(g, body)
        if n:
            print(f"[{label}] reverse connectsTo edges: {n}")
            if not dry:
                update(P + f"INSERT {{ {wrap(g, '?b rpg:connectsTo ?a')} }} WHERE {{ {wrap(g, body)} }}")
        body = "?h a rpg:PlotHook ; rpg:isResolved ?v"
        n = count(g, body)
        if n:
            print(f"[{label}] PlotHook isResolved flags: {n}")
            if not dry:
                update(P + f"DELETE {{ {wrap(g, '?h rpg:isResolved ?v')} }} WHERE {{ {wrap(g, body)} }}")
    print("dry run, nothing written" if dry else "done")


if __name__ == "__main__":
    main()
