import streamlit as st
import requests

API_URL = "http://agent-api:8000"

st.set_page_config(page_title="AI Game Master", layout="wide")
st.title("🐉 AI Game Master")

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.header("Campaign Settings")
    game_system = st.selectbox("Game System", ["AD&D 2nd Edition", "One Page Rules"])
    st.divider()

    st.header("Automated Ingestion")
    document_type = st.radio(
        "Content Type",
        ["module", "session_transcript"],
        format_func=lambda v: "Adventure Module" if v == "module" else "Session Transcript",
    )
    st.caption(
        "Chunking, structural detection (boxed/italic read-aloud text, stat blocks), "
        "classification, and extraction all happen server-side."
    )
    uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])

    if uploaded_file and st.button("Ingest PDF"):
        with st.spinner("Chunking and extracting - this runs several LLM passes per chunk, so it can take a while..."):
            try:
                res = requests.post(
                    f"{API_URL}/ingest_pdf",
                    files={"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")},
                    data={"document_type": document_type, "game_system": game_system},
                )
                data = res.json()
            except Exception as e:
                st.error(f"Request failed: {e}")
                data = None

        if data:
            if data.get("status") == "success":
                results = data.get("results", [])
                errors = [r for r in results if r.get("status") == "error"]
                st.success(f"Processed {len(results)} chunks ({len(results) - len(errors)} succeeded).")
                if errors:
                    with st.expander("View Extraction Errors", expanded=True):
                        for err in errors:
                            st.code(f"Chunk {err['chunk_index']}: {err['details']}", language="text")
            else:
                st.error(data.get("details", "Ingestion failed."))

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
