"""Entry point: a router that gives each page a descriptive menu label."""
import streamlit as st

st.set_page_config(page_title="AI Game Master", page_icon="🐉", layout="wide")

pages = [
    st.Page("gm_chat.py", title="Game Master", icon="🐉", default=True),
    st.Page("pages/3_World_Memory.py", title="World Memory & Canon", icon="🧠"),
    st.Page("pages/1_Adventure_Writer.py", title="Adventure Writer", icon="✍️"),
    st.Page("pages/2_Library_Ingestion.py", title="Library & Ingestion", icon="📚"),
]
st.navigation(pages).run()
