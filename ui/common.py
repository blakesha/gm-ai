import os

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://agent-api:8000")


def api(method, path, **kw):
    """Call the agent API; show an error in the page and return None on failure."""
    timeout = kw.pop("timeout", 60)
    try:
        r = requests.request(method, f"{API_URL}{path}", timeout=timeout, **kw)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        try:
            st.error(e.response.json().get("detail", str(e)))
        except Exception:
            st.error(f"API error: {e}")
    except Exception as e:
        st.error(f"API error: {e}")
    return None
