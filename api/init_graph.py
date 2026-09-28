import os
import requests
from pathlib import Path

OXIGRAPH_STORE_URL = os.getenv("OXIGRAPH_STORE_URL", "http://triplestore:7878/store?default")
BASE_DIR = Path(__file__).resolve().parent
ONTOLOGY_FILE = BASE_DIR / "prompts" / "ontology.ttl"

def seed_database():
    print(f"Loading {ONTOLOGY_FILE} into Oxigraph...")
    with open(ONTOLOGY_FILE, "rb") as f:
        headers = {"Content-Type": "text/turtle"}
        response = requests.post(OXIGRAPH_STORE_URL, data=f, headers=headers)
        
    if response.status_code in [200, 204]:
        print("Ontology successfully loaded into the Knowledge Graph.")
    else:
        print(f"Failed to load ontology. Status: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    seed_database()
