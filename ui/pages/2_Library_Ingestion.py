import datetime
from pathlib import Path

import requests
import streamlit as st

from common import API_URL, api

st.title("📚 Library & Ingestion")
st.caption("Add books to the knowledge graph. Chunking, structure detection, classification and extraction all "
           "happen server-side in the background, so you can close this page.")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def when(iso):
    return datetime.datetime.fromisoformat(iso).strftime("%a %H:%M") if iso else None


def window_banner(win):
    if not win["enabled"]:
        return "Ingestion runs whenever jobs are queued (no time window set)."
    cfg = win["config"]
    span = f"{cfg['start']}–{cfg['end']} {cfg['timezone']}"
    if win["open"]:
        return f"🟢 Window open ({span}). Jobs run until {when(win['next_change'])}, then pause and resume later."
    return f"⏸ Window closed ({span}). Queued and running jobs wait until {when(win['next_change'])}."


win = api("GET", "/ingest/window")

# ------------------------------------------------------------------ schedule
with st.expander("Ingestion schedule", expanded=bool(win and win["enabled"])):
    if win:
        cfg = win["config"]
        st.markdown(window_banner(win))
        enabled = st.checkbox("Only ingest during a time window", value=cfg["enabled"])
        c1, c2, c3 = st.columns(3)
        start = c1.time_input("Window opens", value=datetime.time(*map(int, cfg["start"].split(":"))), step=300)
        end = c2.time_input("Window closes", value=datetime.time(*map(int, cfg["end"].split(":"))), step=300)
        tz = c3.text_input("Timezone", value=cfg["timezone"], help="IANA name, e.g. Australia/Perth")
        days = st.multiselect("Days the window opens", list(range(7)), default=cfg["days"], format_func=lambda d: DAYS[d],
                              help="For an overnight window (e.g. 22:00–06:00) this is the evening it opens: "
                                   "Friday also covers the early hours of Saturday.")
        st.caption("A job that is running when the window closes pauses at the next chunk and continues from there "
                   "when it reopens; at most one chunk overruns. Closes earlier than it opens means overnight.")
        if st.button("Save schedule"):
            res = api("PUT", "/ingest/window", json={
                "enabled": enabled, "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                "days": days, "timezone": tz})
            if res:
                st.success("Schedule saved.")
                st.rerun()

# -------------------------------------------------------------------- settings
left, right = st.columns([2, 3], gap="large")

with left:
    st.subheader("Add to the graph")
    game_system = st.selectbox("Game System", ["AD&D 2nd Edition", "One Page Rules"])
    DOC_TYPES = ["auto", "module", "campaign", "bestiary", "rules", "accessory", "setting", "session_transcript"]
    document_type = st.selectbox(
        "Content Type (for selected files)", DOC_TYPES,
        help="Controls which extraction passes run. Folders are always processed as 'auto', "
             "which detects each file's type from its name and first pages.")

    if "lib_path" not in st.session_state:
        st.session_state.lib_path = ""

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

    with st.expander("Manual text ingestion"):
        manual_text = st.text_area("Raw text chunk")
        if st.button("Ingest text", disabled=not manual_text.strip()):
            with st.spinner("Extracting..."):
                res = requests.post(f"{API_URL}/ingest", json={
                    "chunk_text": manual_text, "document_type": document_type, "game_system": game_system})
                st.json(res.json())

# ------------------------------------------------------------------------ jobs
with right:
    @st.fragment(run_every=5)
    def jobs_panel():
        window = api("GET", "/ingest/window")
        paused = bool(window and window["enabled"] and not window["open"])
        data = api("GET", "/jobs", params={"limit": 50})
        if not data:
            return
        # /jobs lists newest first, so a running job can fall outside the first page; ask for it directly
        running = (api("GET", "/jobs", params={"status": "running"}) or {}).get("jobs", [])
        running_ids = {r["id"] for r in running}
        rows = running + [r for r in data["jobs"] if r["id"] not in running_ids]
        active = [r for r in rows if r["status"] in ("queued", "running")]
        st.subheader(f"Jobs ({len(active)} active)")
        if window:
            st.caption(window_banner(window))
        if active and st.button("Cancel all"):
            api("POST", "/jobs/cancel_all")
        if running:
            for r in running:
                total = r["chunks_total"] or 0
                kind = r["document_type"] if r["document_type"] != "auto" else f"auto→{r.get('detected_type') or '?'}"
                st.markdown(f"**⚙️ Now running:** {r['path']} [{kind}]")
                st.progress((r["chunks_done"] / total) if total else 0.0,
                            text=f"{r['chunks_done']}/{total or '?'} chunks")
        elif active:
            st.caption("Nothing running right now" + (" (window closed)." if paused else " - the next job starts shortly."))
        for r in rows[:25]:
            total = r["chunks_total"] or 0
            frac = (r["chunks_done"] / total) if total else 0.0
            icon = {"queued": "⏸" if paused else "⏳", "running": "⚙️", "done": "✅", "error": "❌", "cancelled": "⛔"}[r["status"]]
            kind = r["document_type"] if r["document_type"] != "auto" else f"auto→{r.get('detected_type') or '?'}"
            note = " — waiting for window" if paused and r["status"] == "queued" else ""
            st.caption(f"{icon} {r['path']} [{kind}] — {r['chunks_done']}/{total or '?'} chunks"
                       + (f", {r['chunk_errors']} chunk errors" if r["chunk_errors"] else "") + note)
            if r["status"] == "running":
                st.progress(frac)
            elif r["status"] == "queued" and r["chunks_done"]:
                st.progress(frac)   # a paused job shows how far it got
            if r["status"] in ("error", "cancelled") and st.button("Retry", key=f"retry_{r['id']}"):
                api("POST", f"/jobs/{r['id']}/retry")
            if r["last_error"] and r["status"] != "done":
                st.code(r["last_error"], language="text")

    jobs_panel()

# -------------------------------------------------------------------- ingested
st.divider()
st.subheader("Ingested books")
st.caption("Books present in the knowledge graph (read from the graph, not the job history).")
books = (api("GET", "/library/ingested") or {}).get("sources", [])
if not books:
    st.caption("Nothing has been ingested yet.")
else:
    rows = [{"Book": b["title"], "Entities": b["entities"]} for b in books]
    q = st.text_input("Filter", placeholder="Search by title")
    if q:
        rows = [r for r in rows if q.lower() in r["Book"].lower()]
    st.caption(f"{len(rows)} book(s)")
    st.dataframe(rows, use_container_width=True, hide_index=True)
