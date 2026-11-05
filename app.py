"""FDIC bank analytics on a governed semantic layer.

    streamlit run app.py

Pages:
  Ask the data    natural-language questions answered through the semantic layer (ask.py)
  The experiment  the three-setup accuracy test and its recorded answers (experiment.py)
"""
import importlib
import sys
from pathlib import Path

import streamlit as st

# After a git push, Streamlit Cloud reruns the pages but can keep older copies of helper modules in memory,
# which breaks imports of anything new (seen as an ImportError). Reload a helper whenever its file changed.
_HELPERS = ("semantic_layer", "prompts", "charts", "analyst")
_seen = sys.modules.setdefault("_app_module_mtimes", type(sys)("_app_module_mtimes")).__dict__
for _name in _HELPERS:
    _mod = sys.modules.get(_name)
    _file = Path(__file__).with_name(f"{_name}.py")
    if _mod is not None and _file.exists():
        _mtime = _file.stat().st_mtime
        if _seen.get(_name) not in (None, _mtime):
            importlib.reload(_mod)
        _seen[_name] = _mtime

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
