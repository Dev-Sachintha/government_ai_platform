"""Modern UI theme for the Government AI Workspace (works in light and dark mode)."""
import streamlit as st

_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --ui-accent: #6366f1;
  --ui-accent-2: #8b5cf6;
  --ui-accent-3: #06b6d4;
  --ui-radius: 14px;
  --ui-border: rgba(128,128,128,0.22);
  --ui-surface: rgba(128,128,128,0.07);
  --ui-shadow: 0 6px 24px rgba(99,102,241,0.12);
}

html, body, [class*="css"], .stApp, button, input, textarea, select {
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif !important;
}
/* keep Streamlit's icon font working */
[data-testid="stIconMaterial"], .material-icons, [class*="material-symbols"] {
  font-family: 'Material Symbols Rounded', 'Material Icons' !important;
}

.block-container { padding-top: 1.6rem; padding-bottom: 4rem; max-width: 1280px; }
header[data-testid="stHeader"] { background: transparent; }

/* ---------- Hero banner ---------- */
.ui-hero {
  background: linear-gradient(120deg, var(--ui-accent) 0%, var(--ui-accent-2) 55%, var(--ui-accent-3) 130%);
  color: #fff; border-radius: 20px; padding: 22px 28px; margin: 0 0 18px 0;
  box-shadow: var(--ui-shadow); display: flex; align-items: center; gap: 16px;
}
.ui-hero .ui-hero-icon {
  font-size: 34px; background: rgba(255,255,255,0.18); width: 58px; height: 58px;
  display: flex; align-items: center; justify-content: center; border-radius: 16px;
  backdrop-filter: blur(6px);
}
.ui-hero h1 { margin: 0; padding: 0; font-size: 1.55rem; font-weight: 700; color: #fff; letter-spacing: -0.01em; }
.ui-hero p  { margin: 2px 0 0 0; opacity: .88; font-size: .92rem; color: #fff; }
.ui-hero .ui-chip {
  margin-left: auto; background: rgba(255,255,255,0.2); padding: 6px 14px; border-radius: 999px;
  font-size: .8rem; font-weight: 600; white-space: nowrap;
}

/* ---------- Tabs as pills ---------- */
div[data-baseweb="tab-list"] {
  gap: 6px; background: var(--ui-surface); padding: 6px; border-radius: 999px;
  border: 1px solid var(--ui-border); width: fit-content; max-width: 100%; overflow-x: auto;
}
button[data-baseweb="tab"] {
  border-radius: 999px !important; padding: 8px 18px !important; height: auto !important;
  font-weight: 600; transition: all .2s ease; background: transparent;
}
button[data-baseweb="tab"]:hover { background: rgba(99,102,241,0.12); }
button[data-baseweb="tab"][aria-selected="true"] {
  background: linear-gradient(120deg, var(--ui-accent), var(--ui-accent-2)) !important; color: #fff !important;
  box-shadow: 0 4px 14px rgba(99,102,241,0.35);
}
div[data-baseweb="tab-highlight"], div[data-baseweb="tab-border"] { display: none !important; }

/* ---------- Buttons ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
  border-radius: 10px; font-weight: 600; border: 1px solid var(--ui-border);
  transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
  transform: translateY(-1px); box-shadow: var(--ui-shadow); border-color: var(--ui-accent);
}
button[kind="primary"], button[data-testid="stBaseButton-primary"], button[data-testid="stBaseButton-primaryFormSubmit"] {
  background: linear-gradient(120deg, var(--ui-accent), var(--ui-accent-2)) !important;
  border: none !important; color: #fff !important;
}

/* ---------- Inputs ---------- */
.stTextInput input, .stTextArea textarea, .stNumberInput input, div[data-baseweb="select"] > div {
  border-radius: 10px !important;
}
.stTextInput input:focus, .stTextArea textarea:focus { box-shadow: 0 0 0 3px rgba(99,102,241,0.25) !important; }

/* ---------- Chat ---------- */
[data-testid="stChatMessage"] {
  background: var(--ui-surface); border: 1px solid var(--ui-border); border-radius: var(--ui-radius);
  padding: 14px 18px; margin-bottom: 12px; animation: uiFade .25s ease;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
  background: linear-gradient(120deg, rgba(99,102,241,0.14), rgba(139,92,246,0.10));
  border-color: rgba(99,102,241,0.3);
}
[data-testid="stChatInput"] {
  border-radius: 999px !important; border: 1px solid var(--ui-border) !important;
  box-shadow: var(--ui-shadow); overflow: hidden;
}
[data-testid="stChatInput"]:focus-within { border-color: var(--ui-accent) !important; }
@keyframes uiFade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

/* ---------- Cards: expanders, forms, metrics, alerts ---------- */
[data-testid="stExpander"], [data-testid="stForm"] {
  border: 1px solid var(--ui-border) !important; border-radius: var(--ui-radius) !important;
  background: var(--ui-surface);
}
[data-testid="stMetric"] {
  background: var(--ui-surface); border: 1px solid var(--ui-border); border-radius: var(--ui-radius); padding: 14px 16px;
}
[data-testid="stAlert"] { border-radius: 12px; }
[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; border: 1px solid var(--ui-border); }
[data-testid="stFileUploaderDropzone"] {
  border: 2px dashed rgba(99,102,241,0.45); border-radius: var(--ui-radius); background: rgba(99,102,241,0.05);
}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"] { border-right: 1px solid var(--ui-border); }
[data-testid="stSidebar"] .stButton > button { width: 100%; text-align: left; }

/* ---------- Login ---------- */
.ui-login { text-align: center; margin: 6vh 0 18px 0; }
.ui-login .ui-login-icon {
  width: 76px; height: 76px; margin: 0 auto 14px auto; border-radius: 22px; font-size: 38px;
  display: flex; align-items: center; justify-content: center; color: #fff;
  background: linear-gradient(135deg, var(--ui-accent), var(--ui-accent-2)); box-shadow: var(--ui-shadow);
}
.ui-login h1 { font-size: 1.7rem; font-weight: 700; margin: 0; padding: 0; letter-spacing: -0.01em; }
.ui-login p { opacity: .7; margin: 6px 0 0 0; }

/* ---------- Scrollbars ---------- */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.35); border-radius: 10px; }
::-webkit-scrollbar-thumb:hover { background: rgba(99,102,241,0.6); }

@media (max-width: 768px) {
  .ui-hero { flex-wrap: wrap; padding: 16px; }
  .ui-hero .ui-chip { margin-left: 0; }
  .ui-hero h1 { font-size: 1.2rem; }
}
</style>
"""


def inject_modern_css():
    """Inject the global stylesheet (call once per run, right after set_page_config)."""
    st.markdown(_CSS, unsafe_allow_html=True)


def render_hero(username: str = "", role: str = ""):
    """Gradient header shown at the top of the main workspace."""
    chip = ""
    if username:
        chip = f"<div class='ui-chip'>👤 {username}" + (f" · {role.title()}" if role else "") + "</div>"
    st.markdown(
        f"""<div class="ui-hero">
            <div class="ui-hero-icon">🏛️</div>
            <div><h1>Government AI Workspace</h1>
            <p>Ask questions, search documents, and explore data with your AI assistant</p></div>
            {chip}
        </div>""",
        unsafe_allow_html=True,
    )


def render_login_header():
    st.markdown(
        """<div class="ui-login">
            <div class="ui-login-icon">🔐</div>
            <h1>Government AI Reporting Platform</h1>
            <p>Sign in to continue</p>
        </div>""",
        unsafe_allow_html=True,
    )
