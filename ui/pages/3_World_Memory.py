import time

import pandas as pd
import streamlit as st

from common import api

st.title("🧠 World Memory & Canon")
st.caption("Everything that happens in a play session (or an adventure you are writing) is kept in its own "
           "non-canon memory: new characters and places, changes to the world, and a log of events. "
           "Review it here and choose what becomes permanent canon.")

kind = st.radio("Memory of", ["Play session", "Adventure"], horizontal=True)
if kind == "Play session":
    listing = (api("GET", "/sessions") or {"sessions": []})["sessions"]
    labels = {s["id"]: f"{s['title']} · {time.strftime('%d %b %H:%M', time.localtime(s['updated']))}" for s in listing}
    base = "/sessions"
else:
    listing = (api("GET", "/author/adventures") or {"adventures": []})["adventures"]
    labels = {a["slug"]: a["title"] for a in listing}
    base = "/author/adventures"
if not labels:
    st.info("Nothing to show yet.")
    st.stop()
ident = st.selectbox("Open", list(labels), format_func=lambda i: labels[i])

mem = api("GET", f"{base}/{ident}/memory")
if not mem:
    st.stop()


def picker(rows, columns, key):
    """A table with a Select column; returns the selected row dicts."""
    df = pd.DataFrame(rows, columns=["uri"] + columns)
    df.insert(0, "Select", False)
    edited = st.data_editor(df, hide_index=True, use_container_width=True, key=key,
                            disabled=["uri"] + columns, column_config={"uri": None})
    return [rows[i] for i in edited.index[edited["Select"]]]


entities_tab, state_tab, events_tab = st.tabs(
    [f"New entities ({len(mem['entities'])})", f"World changes ({len(mem['state'])})",
     f"Event log ({len(mem['events'])})"])

with entities_tab:
    st.caption("Created in this memory only, so they are **not canon** yet. Marking one canon copies it into the "
               "shared setting graph for every session and removes it from this memory.")
    if not mem["entities"]:
        st.write("No new entities.")
    else:
        chosen = picker(mem["entities"], ["type", "name"], f"ent_{ident}")
        sure = st.checkbox("I understand marking canon changes the shared setting", key=f"sure_ent_{ident}")
        c1, c2 = st.columns(2)
        if c1.button("✅ Mark selected as canon", disabled=not (chosen and sure), key=f"promote_{ident}"):
            res = api("POST", f"{base}/{ident}/memory/promote", json={"uris": [e["uri"] for e in chosen]})
            if res:
                st.success(f"Promoted {res['promoted']} entit{'y' if res['promoted'] == 1 else 'ies'} to canon.")
                for r in res.get("left_behind", []):
                    st.warning(f"{r['entity']} → {r['predicate']} → {r['points_to']} stays in memory "
                               "(the target is not canon). Promote it too to link them.")
                if not res.get("left_behind"):
                    st.rerun()
        if c2.button("🗑 Discard selected", disabled=not chosen, key=f"discard_{ident}"):
            if api("POST", f"{base}/{ident}/memory/discard", json={"uris": [e["uri"] for e in chosen]}):
                st.rerun()

with state_tab:
    st.caption("Changes to entities that already exist in canon (a character died, an item moved...). Canon is "
               "unchanged until you promote a change.")
    if not mem["state"]:
        st.write("No world changes.")
    else:
        chosen = picker(mem["state"], ["name", "predicate", "value"], f"st_{ident}")
        sure = st.checkbox("I understand promoting overwrites canon", key=f"sure_st_{ident}")
        c1, c2 = st.columns(2)
        items = [{"uri": s["uri"], "predicate": s["predicate"]} for s in chosen]
        if c1.button("✅ Make selected canon", disabled=not (chosen and sure), key=f"promote_st_{ident}"):
            if api("POST", f"{base}/{ident}/memory/promote-state", json={"items": items}):
                st.rerun()
        if c2.button("↩ Revert selected", disabled=not chosen, key=f"revert_st_{ident}"):
            if api("POST", f"{base}/{ident}/memory/revert-state", json={"items": items}):
                st.rerun()
    if mem.get("hooks_resolved"):
        st.subheader("Plot hooks resolved here")
        st.table(pd.DataFrame(mem["hooks_resolved"])[["name", "outcome"]])
        st.caption("Hook resolutions only apply to this memory; the hooks stay reusable in canon.")

with events_tab:
    if not mem["events"]:
        st.write("No events recorded yet.")
    else:
        for e in mem["events"]:
            when = time.strftime("%d %b %H:%M", time.localtime(e["time"])) if e.get("time") else ""
            st.markdown(f"**{e['name']}** {when}")
            if e.get("description"):
                st.write(e["description"])
            if e.get("outcome"):
                st.caption("Outcome: " + e["outcome"])
