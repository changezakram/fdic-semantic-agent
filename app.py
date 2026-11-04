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

with st.sidebar:
    st.divider()
    st.markdown("Built by **Changez Akram**  \n"
                "[changezakram.github.io](https://changezakram.github.io) · "
                "[Code on GitHub](https://github.com/changezakram/fdic-semantic-agent)")
    st.caption("A personal project using public FDIC call-report data. Not affiliated with any employer "
               "or with the FDIC. Views are my own. For research and illustration, not investment advice.")
