import time

import streamlit as st

from common import api

st.title("🐉 AI Game Master")

state = st.session_state


def transcript_markdown(title, messages):
    lines = [f"# {title}", ""]
    for m in messages:
        who = "**Player**" if m["role"] == "user" else "**GM**"
        lines += [f"{who}: {m['content']}", ""]
    return "\n".join(lines)


with st.sidebar:
    st.header("Game")
    game_system = st.selectbox("Game System", ["AD&D 2nd Edition", "One Page Rules"])
    st.divider()

    st.header("Sessions")
    listing = (api("GET", "/sessions") or {"sessions": []})["sessions"]
    ids = [s["id"] for s in listing]
    labels = {s["id"]: f"{s['title']} · {time.strftime('%d %b %H:%M', time.localtime(s['updated']))}" for s in listing}
    if state.get("gm_session_id") not in ids:
        state.gm_session_id = ids[0] if ids else None

    if st.button("➕ New session", use_container_width=True):
        created = api("POST", "/sessions", json={"game_system": game_system})
        if created:
            state.gm_session_id = created["id"]
            st.rerun()
    if ids:
        chosen = st.selectbox("Open", ids, index=ids.index(state.gm_session_id), format_func=lambda i: labels[i])
        if chosen != state.gm_session_id:
            state.gm_session_id = chosen
            st.rerun()
    st.caption("Every conversation is recorded on the server. Library and ingestion are on their own page.")

# load the current session's transcript when it changes
if state.gm_session_id and state.get("gm_loaded") != state.gm_session_id:
    session = api("GET", f"/sessions/{state.gm_session_id}")
    state.gm_messages = session["messages"] if session else []
    state.gm_title = session["title"] if session else ""
    state.gm_loaded = state.gm_session_id
messages = state.get("gm_messages", []) if state.gm_session_id else []

if state.gm_session_id:
    with st.sidebar:
        with st.expander("Session options"):
            new_title = st.text_input("Title", value=state.get("gm_title", ""), key=f"title_{state.gm_session_id}")
            if st.button("Rename", disabled=not new_title.strip() or new_title == state.get("gm_title")):
                if api("PUT", f"/sessions/{state.gm_session_id}", json={"title": new_title}):
                    state.gm_loaded = None
                    st.rerun()
            st.download_button("⬇ Download transcript", transcript_markdown(state.get("gm_title", "Session"), messages),
                               file_name=f"{state.gm_session_id}.md", mime="text/markdown", disabled=not messages)
            confirm = st.checkbox("Confirm delete")
            if st.button("🗑 Delete session", disabled=not confirm):
                if api("DELETE", f"/sessions/{state.gm_session_id}"):
                    state.gm_session_id, state.gm_loaded = None, None
                    st.rerun()

for msg in messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg.get("content", ""))
        if msg.get("grounded"):
            with st.expander("Facts before narration"):
                st.markdown(msg["grounded"])

if prompt := st.chat_input("What do you do?"):
    if not state.gm_session_id:   # first message: start a recorded session
        created = api("POST", "/sessions", json={"game_system": game_system})
        if not created:
            st.stop()
        state.gm_session_id, state.gm_loaded, state.gm_messages = created["id"], created["id"], []
        messages = state.gm_messages
    messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("The GM is checking the rules and lore..."):
            payload = {"game_system": game_system, "session_id": state.gm_session_id,
                       "messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
            res = api("POST", "/chat", json=payload, timeout=900)
        if res:
            reply = res.get("message", {})
            entry = {"role": "assistant", "content": reply.get("content") or "*(The GM reviewed their notes but remained silent.)*"}
            if res.get("grounded"):
                entry["grounded"] = res["grounded"]
            messages.append(entry)
            state.gm_loaded = None   # reload next run so the title and sidebar reflect the server's record
            st.rerun()
