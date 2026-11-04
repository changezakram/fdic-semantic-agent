"""FDIC bank analytics on a governed semantic layer.

    streamlit run app.py

Pages:
  Ask the data    natural-language questions answered through the semantic layer (ask.py)
  The experiment  the three-setup accuracy test and its recorded answers (experiment.py)
"""
import streamlit as st

st.set_page_config(page_title="Bank Analytics on a Semantic Layer", page_icon=":bank:", layout="wide")
pages = st.navigation([
    st.Page("ask.py", title="Ask the data", icon=":material/chat:", default=True),
    st.Page("experiment.py", title="The experiment", icon=":material/science:"),
])
pages.run()
