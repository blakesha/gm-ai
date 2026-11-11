# RPG AI GM

A self-hosted AI Game Master and adventure-writing partner for tabletop RPGs, built around a **knowledge graph**
extracted from your own rulebooks, settings and modules. Everything runs locally: a local LLM (Ollama), a local
triplestore (Oxigraph) and a small FastAPI + Streamlit app.

It does three things:

1. **Ingests your PDFs** (settings, modules, campaigns, bestiaries, rules) into an RDF knowledge graph of places,
   people, factions, gods, items, events, plot hooks, read-aloud text and stat blocks, with the source book and page
   recorded for every fact.
2. **Runs a GM agent** that answers and narrates strictly from that graph, so it doesn't make up setting lore.
3. **Helps you write an adventure** with a writing-partner agent that treats the graph as read-only canon, keeps your
   new material in a separate non-canon graph, and maintains an editable Markdown document you can run or ingest later.

It was developed against AD&D 2nd Edition material (Forgotten Realms) but the pipeline is system-agnostic.

## Architecture

```
                    ┌────────────┐      ┌────────────────────────────────┐
  browser ─────────▶│  web-ui    │─────▶│  agent-api (FastAPI)           │
 (or Tailscale)     │ Streamlit  │      │  ingestion · GM agent · writer │
                    └────────────┘      └──────┬───────────────┬─────────┘
                                               │ SPARQL        │ chat/tools
                                       ┌───────▼──────┐  ┌─────▼─────┐
                                       │  triplestore │  │    llm    │
                                       │   Oxigraph   │  │  Ollama   │
                                       └──────────────┘  └───────────┘
   /library (your PDFs)  ──▶ ingestion jobs          /data: jobs.db, adventures/
```

| Service | Container | Port | Purpose |
|---|---|---|---|
| `web-ui` | `gm_ui` | 8501 | Streamlit app: GM chat, ingestion, and the **Adventure Writer** page |
| `agent-api` | `agent_api` | 8000 | FastAPI: ingestion pipeline, GM agent, adventure writer |
| `triplestore` | `rpg_graph_db` | 127.0.0.1:7879 | Oxigraph SPARQL store (canon graph + one named graph per adventure) |
| `llm` | `local_llm` | 127.0.0.1:11434 | Ollama (ROCm image; edit the compose file for NVIDIA/CPU) |
| `tailscale` | `rpg_tailscale` | - | Optional sidecar that serves the UI (443) and API (8443) on your tailnet |
| `postman-tester` | `postman_runner` | - | Holds the Postman/newman collection for API tests |

## Quick start

Requirements: Docker or Podman with compose, a GPU supported by Ollama (a 16 GB card runs the default 14B model),
and a folder of your PDFs.

```bash
cp .env.example .env
# edit .env:
#   LIBRARY_HOST_PATH  folder of your PDFs, mounted read/write at /library
#   TS_AUTHKEY         Tailscale auth key (only needed for the sidecar)
docker compose up -d            # or: podman compose up -d

# pull the main model (Ollama is inside the llm container)
docker exec local_llm ollama pull qwen2.5:14b
```

Open http://localhost:8501. In the sidebar, browse the library, choose a PDF (or a folder) and press
**Process**. Ingestion runs in the background; the Jobs panel shows progress and you can close the page.
Then ask the GM about what you ingested, or open **Adventure Writer** from the page list.

> **Changing configuration** (anything in `.env` or `docker-compose.yml`): containers have to be recreated to pick
> up new environment variables, and `agent-api` has dependents. With Podman, `podman rm -f --depend agent_api`
> followed by `podman compose up -d --no-deps agent-api web-ui tailscale` works. Named volumes (graph, models,
> jobs, adventures, Tailscale identity) are preserved. Code under `api/` and `ui/` is bind-mounted, so Python and
> prompt changes only need `restart agent_api`.

## Ingestion

PDFs are processed server-side by a background job queue (`api/jobs.py`, SQLite in `/data/jobs.db`).

1. **Chunking** (`chunker.py`): structural, PDF-aware splitting at headings and keyed areas ("Room #9A"), detecting
   boxed or italic read-aloud text and stat blocks, with a section breadcrumb on each chunk.
2. **Document type** (`doctypes.py`): `auto` infers `module`, `campaign`, `setting`, `bestiary`, `rules`,
   `accessory` or `session_transcript` from the path and first pages. Each type runs a different set of passes.
3. **Extraction passes** (`prompts/extraction/*.md` + JSON schemas, called through Ollama with constrained output):
   location, scene, statblock, read-aloud, trigger, lore, actor, item, plot hook. A `setting` run covers location,
   actor, lore, item and plot hook; modules and campaigns run everything.
4. **Precision filters** (`extract_filter.py`): names must be grounded in the passage; generic nouns are dropped.
5. **Mapping** (`mapper.py`): deterministic conversion of the extracted JSON to RDF triples. No LLM involved. Names
   are resolved to existing entities (`kg.py`), including variants like "Shadowdale village" or "Lord Elminster",
   and provenance (source document, page, section) is attached to everything.

### Maintenance scripts (run inside the `agent_api` container)

```bash
docker exec agent_api python cleanup_graph.py              # dry run: junk, names, worlds, orgs, retype, parents, deities
docker exec agent_api python cleanup_graph.py --apply
docker exec agent_api python resolve_duplicates.py --exact # dry run: safe merges of same-name entities
docker exec agent_api python resolve_duplicates.py --exact --apply
docker exec agent_api python init_graph.py                 # (re)load the ontology into the store
```

`cleanup_graph.py` repairs systematic extraction noise (placeholder "null" entities, commentary in names, mistyped
worlds, generic groups typed as organizations, unparented places, gods "located" in places). Everything is a dry run
unless `--apply` is passed. There is no undo, so snapshot first:

```bash
curl -H 'Accept: application/n-quads' http://localhost:7879/store > backup.nq
```

## The knowledge graph

The ontology is `api/prompts/ontology.ttl`; the triplestore does **no inference**, so each entity carries one type.

- **Places:** `World > Region > Settlement > Structure > Room`, plus `Landmark` for everything else, all
  subclasses of `Location`. Find all places with `?p a/rdfs:subClassOf* rpg:Location`.
- **Actors:** `Actor`, with `Organization` and `Deity` also typed `Actor`. `memberOf` for factions, `worshippedIn` for gods.
- **Other entities:** `Item`, `HistoricalEvent`, `Scene`, `PlotHook`, `StatBlock`, `ReadAloudText`, `Trigger`, `HiddenInfo`.
- **Provenance:** `rpg:foundIn` (source document), `rpg:foundOnPage`, `rpg:foundInSection`.

Query it from the shell:

```bash
./scripts/kg-query.sh scripts/queries/overview.rq
./scripts/kg-query.sh "SELECT ?n WHERE { ?r a <http://example.org/rpg-ontology#Region> ; <http://example.org/rpg-ontology#name> ?n } LIMIT 20"
```

### Canon versus adventure graphs

The ingested books live in the default graph ("canon"). Each adventure you write gets its own named graph,
`urn:rpg:adventure:<slug>`, whose entities are flagged `rpg:isCanon false` and sourced to the adventure through
`rpg:foundIn`. Canon queries (the GM agent, ingestion) never see it, so invented material can't be mistaken for
setting lore, while one query can still join both with `GRAPH <urn:rpg:adventure:...> { ... }`.

## GM agent (`POST /chat`)

A small local model is unreliable at deciding what to look up, so the agent is built to be grounded:

- **Pre-fetch** (`context.py`): entities named in the player's message are found, and their full records
  (descriptions, source page, containers, contents, events, members) are put in the prompt before the model runs.
- **Strict grounding:** the prompt requires answers to come only from graph results and to say plainly when the
  graph has nothing. SPARQL tool errors are returned to the model with the query that failed.
- **Narration model (optional):** if `NARRATION_MODEL` is set, a second, creative model rewrites the grounded answer
  in a Game Master voice. In `auto` mode only generative turns (actions, scene description) are narrated; factual
  lookups skip it. The grounded text is returned alongside as `grounded`.

## Adventure Writer (`/author/*`, page "Adventure Writer")

A co-author for building your own adventure:

- Keeps one **Markdown document** per adventure (premise, tone, hooks, background, outline, NPCs, locations,
  encounters, read-aloud, treasure), laid out like a published module so it can be ingested into canon later.
  You can view, edit and download it in the page.
- Treats canon as **read-only**: it looks up existing places, people and factions and uses their exact names, but
  invents freely for new material.
- **Graph memory:** `record_actor/location/item/plot_hook` tools save what you agree to in the adventure's
  non-canon graph, linked to canon nodes by name instead of duplicating them. Each also adds a stub to the document.
- Hands **prose** (read-aloud text, descriptions, rumors) to the creative model via `write_prose`.
- Saves the chat on the server, so you can close the page and return to the adventure later.

## Configuration

Set in `.env` (passed through `docker-compose.yml`).

| Variable | Default | Purpose |
|---|---|---|
| `LIBRARY_HOST_PATH` | `/mnt/storage/share/RPG` | Host folder of PDFs, mounted at `/library` |
| `TS_AUTHKEY` | - | Tailscale auth key (sidecar only) |
| `OLLAMA_MODEL` | `qwen2.5:14b` | Model for extraction, tools and grounded answers |
| `OLLAMA_NUM_CTX` | `16384` | Context window for the main model |
| `NARRATION_MODEL` | empty (off) | Creative model for GM-voice narration and adventure prose |
| `NARRATION_MODE` | `auto` | `auto` (scenes only), `always`, or `off` |
| `NARRATION_TEMPERATURE` / `NARRATION_NUM_CTX` | `0.7` / `4096` | Narration sampling and context size |
| `EXTRACT_FILTER` | `grounding` | `none`, `grounding`, `llm` or `both` |
| `OLLAMA_NUM_PREDICT` | `3000` | Token cap per extraction call |
| `ADVENTURES_DIR` | `/data/adventures` | Where adventure documents and chats are stored |

Model notes: the repo's setup uses `qwen2.5:14b` for everything structured. For narration it is configured for a
12B roleplay model (e.g. Wayfarer 12B Q4_K_M via `ollama pull hf.co/LatitudeGames/Wayfarer-12B-GGUF:Q4_K_M`). On a
16 GB GPU the two models don't stay resident together, so Ollama swaps them (a few seconds per narrated reply).

## Evaluation and benchmarking

- `api/eval/run_eval.py` scores extraction against labelled chunks in `api/eval/gold_*.json`:
  `docker exec agent_api python eval/run_eval.py --models qwen2.5:14b qwen3:14b`
- `api/bench_models.py` compares models on the same chunks (speed, JSON failures, entity counts).
- `postman/rpg_api_collection.json` is a Postman collection for the API.

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /library`, `POST /library/upload` | Browse and upload PDFs |
| `POST /jobs`, `GET /jobs`, `POST /jobs/{id}/cancel`, `/retry`, `/cancel_all` | Background ingestion queue |
| `POST /ingest`, `POST /ingest_pdf` | Ingest raw text / a single PDF directly |
| `GET /document_types` | Available document types |
| `POST /chat` | GM agent |
| `GET/POST /author/adventures`, `GET/PUT /author/adventures/{slug}` | List, create, read, save adventure documents |
| `GET /author/adventures/{slug}/chat`, `/graph` | Saved conversation; what is recorded in its graph |
| `POST /author/chat` | Adventure writing partner |

## Repository layout

```
api/                  FastAPI app
  main.py             endpoints, ingestion orchestration, GM agent
  author.py           adventure writer (document, tools, agent loop)
  adventure_graph.py  per-adventure non-canon graph memory
  context.py          pre-fetch of graph records for the prompt
  kg.py               SPARQL client, entity search and name resolution
  mapper.py           extracted JSON -> RDF
  chunker.py, doctypes.py, extract_filter.py, stat_parser.py, jobs.py
  cleanup_graph.py, resolve_duplicates.py, init_graph.py
  prompts/            agent, narration and author prompts, ontology, extraction prompts + schemas
  eval/               extraction evaluation harness
ui/                   Streamlit app (app.py = GM + ingestion; pages/ = Adventure Writer)
scripts/              kg-query.sh and example SPARQL
postman/              API test collection
tailscale/            serve config for the sidecar
```

## Known limitations

- Extraction from a 14B local model is imperfect: expect some mistyped entities and noisy names. The cleanup
  scripts fix the common systematic cases; they are heuristics, so review dry runs.
- The GM agent sticks to the graph far better than an unconstrained model, but it can still paraphrase loosely or
  add atmosphere, especially when narrated.
- Place hierarchy (`locatedIn`) is incomplete for gazetteer-style text where the source gives no explicit containment.
- Adventure promotion into canon is not automated; the adventure document can be ingested like any other source.

## License

Apache License 2.0, see [LICENSE](LICENSE).
