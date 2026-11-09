import streamlit as st
import os
import requests
from pathlib import Path

API_URL = os.environ.get("API_URL", "http://agent-api:8000")

st.set_page_config(page_title="AI Game Master", layout="wide")
st.title("🐉 AI Game Master")

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.header("Campaign Settings")
    game_system = st.selectbox("Game System", ["AD&D 2nd Edition", "One Page Rules"])
    st.divider()

    st.header("Automated Ingestion")
    DOC_TYPES = ["auto", "module", "campaign", "bestiary", "rules", "accessory", "setting", "session_transcript"]
    document_type = st.selectbox(
        "Content Type (for selected files)",
        DOC_TYPES,
        help="Controls which extraction passes run. Folders are always processed as 'auto', "
             "which detects each file's type from its name and first pages.",
    )
    st.caption(
        "Chunking, structural detection (boxed/italic read-aloud text, stat blocks), "
        "classification, and extraction all happen server-side."
    )
    st.caption("Files are processed in the background; you can close this page.")

    if "lib_path" not in st.session_state:
        st.session_state.lib_path = ""

    def api(method, path, **kw):
        try:
            r = requests.request(method, f"{API_URL}{path}", timeout=60, **kw)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            st.error(f"API error: {e}")
            return None

    with st.expander("Library", expanded=True):
        listing = api("GET", "/library", params={"path": st.session_state.lib_path})
        if listing:
            cur = listing["path"]
            st.code("/" + cur if cur != "." else "/", language="text")
            if cur not in ("", ".") and st.button("⬆ Up"):
                st.session_state.lib_path = str(Path(cur).parent) if str(Path(cur).parent) != "." else ""
                st.rerun()
            dirs = [e["name"] for e in listing["entries"] if e["type"] == "dir"]
            pdfs = [e["name"] for e in listing["entries"] if e["type"] == "pdf"]
            if dirs:
                pick = st.selectbox("Open folder", [""] + dirs, key=f"dir_{cur}")
                if pick:
                    st.session_state.lib_path = str(Path(cur) / pick) if cur not in ("", ".") else pick
                    st.rerun()
            selected = st.multiselect("PDFs in this folder", pdfs, key=f"pdf_{cur}")
            recursive = st.checkbox("Include subfolders when processing folder", value=True)
            force = st.checkbox("Re-process already-ingested files", value=False)
            base = "" if cur in ("", ".") else cur

            c1, c2 = st.columns(2)
            if c1.button("Process selected", disabled=not selected):
                res = api("POST", "/jobs", json={
                    "paths": [str(Path(base) / n) for n in selected], "document_type": document_type,
                    "game_system": game_system, "force": force})
                if res:
                    st.success(f"Queued {res['queued']}, skipped {res['skipped']} (already done/queued).")
            if c2.button("Process this folder (auto)"):
                res = api("POST", "/jobs", json={
                    "paths": [base], "document_type": "auto", "game_system": game_system,
                    "recursive": recursive, "force": force})
                if res:
                    st.success(f"Queued {res['queued']}, skipped {res['skipped']} (already done/queued).")

        up = st.file_uploader("Upload PDF(s) into this folder", type=["pdf"], accept_multiple_files=True)
        if up and st.button("Upload"):
            for f in up:
                api("POST", "/library/upload", files={"file": (f.name, f.getvalue(), "application/pdf")},
                    data={"directory": "" if st.session_state.lib_path in ("", ".") else st.session_state.lib_path})
            st.success(f"Uploaded {len(up)} file(s). Select them above and process.")

    @st.fragment(run_every=5)
    def jobs_panel():
        data = api("GET", "/jobs", params={"limit": 50})
        if not data:
            return
        rows = data["jobs"]
        active = [r for r in rows if r["status"] in ("queued", "running")]
        st.subheader(f"Jobs ({len(active)} active)")
        if active and st.button("Cancel all"):
            api("POST", "/jobs/cancel_all")
        for r in rows[:25]:
            total = r["chunks_total"] or 0
            frac = (r["chunks_done"] / total) if total else 0.0
            icon = {"queued": "⏳", "running": "⚙️", "done": "✅", "error": "❌", "cancelled": "⛔"}[r["status"]]
            kind = r["document_type"] if r["document_type"] != "auto" else f"auto→{r.get('detected_type') or '?'}"
            st.caption(f"{icon} {r['path']} [{kind}] — {r['chunks_done']}/{total or '?'} chunks"
                       + (f", {r['chunk_errors']} chunk errors" if r["chunk_errors"] else ""))
            if r["status"] == "running":
                st.progress(frac)
            if r["status"] in ("error", "cancelled") and st.button("Retry", key=f"retry_{r['id']}"):
                api("POST", f"/jobs/{r['id']}/retry")
            if r["last_error"] and r["status"] != "done":
                st.code(r["last_error"], language="text")

    jobs_panel()

    st.divider()

    with st.expander("Manual Text Ingestion"):
        manual_text = st.text_area("Raw Text Chunk")
        if st.button("Ingest Text"):
            with st.spinner("Extracting..."):
                res = requests.post(
                    f"{API_URL}/ingest",
                    json={"chunk_text": manual_text, "document_type": document_type, "game_system": game_system},
                )
                st.json(res.json())

for msg in st.session_state.messages:
    if msg["role"] in ["user", "assistant"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg.get("content", ""))

if prompt := st.chat_input("What do you do?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("The GM is checking the rules and lore..."):
            payload = {"game_system": game_system, "messages": st.session_state.messages}
            try:
                response = requests.post(f"{API_URL}/chat", json=payload).json()
                assistant_msg = response.get("message", {})
                content = assistant_msg.get("content", "*(The GM reviewed their notes but remained silent.)*")
                st.markdown(content)
                st.session_state.messages.append({"role": "assistant", "content": content})
            except Exception as e:
                st.error(f"Failed to communicate with Agent API: {e}")
