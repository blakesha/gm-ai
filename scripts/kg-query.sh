#!/usr/bin/env bash
# Run a SPARQL query against the Oxigraph triplestore and pretty-print the
# JSON results. Usage:
#   ./scripts/kg-query.sh scripts/queries/overview.rq
#   ./scripts/kg-query.sh "SELECT * WHERE { ?s ?p ?o } LIMIT 10"
set -euo pipefail

SPARQL_QUERY_URL="${SPARQL_QUERY_URL:-http://localhost:7879/query}"

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <query.rq | 'SPARQL query string'>" >&2
  exit 1
fi

if [ -f "$1" ]; then
  QUERY=$(cat "$1")
else
  QUERY="$1"
fi

curl -s "$SPARQL_QUERY_URL" \
  --data-urlencode "query=$QUERY" \
  -H "Accept: application/sparql-results+json" | jq .
