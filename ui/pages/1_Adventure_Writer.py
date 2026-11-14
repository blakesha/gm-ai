import os

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://agent-api:8000")

st.title("✍️ Adventure Writer")
st.caption("A writing partner for your own adventure. It checks the setting's knowledge graph for canon, "
           "invents new material with you, and keeps a Markdown document you can edit, download, "
           "and later ingest into canon.")


def api(method, path, **kw):
    try:
        r = requests.request(method, f"{API_URL}{path}", timeout=kw.pop("timeout", 60), **kw)
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


state = st.session_state
state.setdefault("author_chats", {})   # slug -> list of messages
state.setdefault("author_docs", {})    # slug -> current document text
state.setdefault("author_edit_version", 0)

with st.sidebar:
    st.header("Adventure")
    game_system = st.selectbox("Game System", ["AD&D 2nd Edition", "One Page Rules"])
    listing = api("GET", "/author/adventures") or {"adventures": []}
    adventures = listing["adventures"]
    labels = {a["slug"]: a["title"] for a in adventures}
    slug = st.selectbox("Open", list(labels), format_func=lambda s: labels[s]) if adventures else None

    with st.form("new_adventure", clear_on_submit=True):
        new_title = st.text_input("New adventure title")
        if st.form_submit_button("Create") and new_title.strip():
            created = api("POST", "/author/adventures", json={"title": new_title})
            if created:
                state.author_docs[created["slug"]] = created["content"]
                st.rerun()
    st.caption("Documents are saved on the server as Markdown, so you can close this page.")

if not slug:
    st.info("Create an adventure in the sidebar to begin.")
    st.stop()

if slug not in state.author_docs:
    loaded = api("GET", f"/author/adventures/{slug}")
    state.author_docs[slug] = loaded["content"] if loaded else ""
if slug not in state.author_chats:   # the server keeps each adventure's conversation, so you can come back to it
    saved = api("GET", f"/author/adventures/{slug}/chat")
    state.author_chats[slug] = saved["messages"] if saved else []
messages = state.author_chats[slug]

chat_col, doc_col = st.columns([3, 2], gap="large")

with doc_col:
    view_tab, edit_tab, memory_tab = st.tabs(["Document", "Edit", "Memory"])
    with view_tab:
        st.download_button("⬇ Download Markdown", state.author_docs[slug], file_name=f"{slug}.md",
                           mime="text/markdown")
        st.markdown(state.author_docs[slug])
    with edit_tab:
        edited = st.text_area("Markdown", state.author_docs[slug], height=600,
                              key=f"edit_{slug}_{state.author_edit_version}")
        if st.button("Save my edits", disabled=edited == state.author_docs[slug]):
            if api("PUT", f"/author/adventures/{slug}", json={"content": edited}):
                state.author_docs[slug] = edited
                state.author_edit_version += 1
                st.rerun()

    with memory_tab:
        st.caption("Characters, places, items and hooks recorded for this adventure. They live in its own "
                   "non-canon graph, linked to canon where they refer to the setting.")
        graph = api("GET", f"/author/adventures/{slug}/graph")
        if graph:
            counts = graph["counts"]
            st.write(", ".join(f"**{n}** {t}" for t, n in sorted(counts.items())) or "Nothing recorded yet.")
            by_type = {}
            for e in graph["entities"]:
                by_type.setdefault(e["type"], []).append(e["name"])
            for t, names in sorted(by_type.items()):
                with st.expander(f"{t} ({len(names)})"):
                    st.write("\n".join(f"- {n}" for n in names))
            st.code(graph["graph"], language="text")

with chat_col:
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("changed"):
                st.caption("📝 Saved to document: " + ", ".join(msg["changed"]))

    if prompt := st.chat_input("What shall we write?"):
        messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Checking canon and drafting..."):
                payload = {"slug": slug, "game_system": game_system,
                           "messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
                res = api("POST", "/author/chat", json=payload, timeout=900)
            if res:
                content = res["message"].get("content") or "*(No reply.)*"
                changed = res.get("changed") or []
                messages.append({"role": "assistant", "content": content, "changed": changed})
                state.author_docs[slug] = res["document"]
                state.author_edit_version += 1
                st.rerun()
