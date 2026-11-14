import hmac
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import sqlite3
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from db_utils import init_db, is_safe_select, log_event
from validation_utils import validate_and_restore_sqlite
import library
import llm
from chat_app import (
    init_chat_tables,
    render_ai_chat_interface,
    render_shared_thread_view,
    render_sidebar_threads,
)
from web_sources import add_or_refresh_source, delete_source, list_sources, validate_url

TIMEOUT_SECONDS = 300


def apply_user_friendly_styles():
    """Apply a clean, responsive and accessible visual layer to the workspace."""
    st.markdown(
        """
        <style>
        :root { --gov-accent: #2563eb; --gov-accent-soft: rgba(37,99,235,.10); }
        .block-container { padding-top: 1.35rem; padding-bottom: 3rem; max-width: 1500px; }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stSidebar"] { border-right: 1px solid rgba(128,128,128,.18); }
        [data-testid="stSidebar"] .block-container { padding-top: 1.2rem; }
        h1, h2, h3 { letter-spacing: -.025em; }
        h1 { font-weight: 750 !important; }
        [data-testid="stTabs"] [role="tab"] { border-radius: 10px 10px 0 0; padding: .7rem 1rem; font-weight: 600; }
        [data-testid="stTabs"] [aria-selected="true"] { color: var(--gov-accent); }
        [data-testid="stButton"] button, [data-testid="stDownloadButton"] button,
        [data-testid="stFormSubmitButton"] button { border-radius: 10px; font-weight: 600; min-height: 2.55rem; transition: transform .12s ease, box-shadow .12s ease; }
        [data-testid="stButton"] button:hover, [data-testid="stDownloadButton"] button:hover { transform: translateY(-1px); box-shadow: 0 4px 14px rgba(0,0,0,.08); }
        [data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea,
        [data-testid="stSelectbox"] div[data-baseweb="select"] { border-radius: 10px; }
        [data-testid="stChatInput"] textarea { border-radius: 14px; }
        [data-testid="stExpander"] { border-radius: 12px; }
        [data-testid="stAlert"] { border-radius: 12px; }
        [data-testid="stMetric"] { background: var(--gov-accent-soft); padding: .8rem 1rem; border-radius: 12px; }
        .gov-hero { padding: 1.2rem 1.35rem; border: 1px solid rgba(128,128,128,.2); border-radius: 16px;
                    background: linear-gradient(115deg, var(--gov-accent-soft), rgba(128,128,128,.035)); margin: .2rem 0 1rem; }
        .gov-hero-title { font-size: clamp(1.35rem, 2.5vw, 1.9rem); font-weight: 800; margin: 0; }
        .gov-hero-sub { opacity: .78; margin: .35rem 0 0; font-size: .96rem; }
        .gov-chip { display:inline-block; padding:.25rem .6rem; margin:.35rem .3rem 0 0; border-radius:999px;
                    background:var(--gov-accent-soft); font-size:.78rem; font-weight:600; }
        @media (max-width: 700px) {
          .block-container { padding-left: .8rem; padding-right: .8rem; }
          [data-testid="stTabs"] [role="tab"] { padding: .55rem .55rem; font-size: .82rem; }
        }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
        </style>
        """,
        unsafe_allow_html=True,
    )

DEFAULT_ADMIN_PASSWORD = "SecurePass123!"
DEFAULT_ANALYST_PASSWORD = "AnalystPass123!"


def _read_secrets(keys):
    """Read values from Streamlit Secrets safely without throwing attribute errors."""
    out = {}
    try:
        if hasattr(st, "secrets"):
            for key in keys:
                if key in st.secrets:
                    out[key] = st.secrets[key]
    except Exception:
        pass
    return out


def _load_users():
    sec = _read_secrets(["ADMIN_PASSWORD", "ANALYST_PASSWORD"])
    admin_pw = str(sec.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or "")
    analyst_pw = str(
        sec.get("ANALYST_PASSWORD") or os.environ.get("ANALYST_PASSWORD") or ""
    )
    using_defaults = not (admin_pw and analyst_pw)
    return (
        {
            "admin": (admin_pw or DEFAULT_ADMIN_PASSWORD, "admin"),
            "analyst": (analyst_pw or DEFAULT_ANALYST_PASSWORD, "analyst"),
        },
        using_defaults,
    )


MOCK_USERS, USING_DEFAULT_PASSWORDS = _load_users()


@st.cache_resource
def _bg_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=2)


def start_job(name, fn, *args):
    st.session_state.pop(f"result:{name}", None)
    st.session_state[f"job:{name}"] = {
        "future": _bg_executor().submit(fn, *args),
        "started": time.time(),
    }
    st.session_state["last_active"] = time.time()


def job_running(name) -> bool:
    return st.session_state.get(f"job:{name}") is not None


def poll_job(name, label):
    job = st.session_state.get(f"job:{name}")
    if job is None:
        return
    if job["future"].done():
        try:
            ok, msg = job["future"].result()
        except Exception as e:
            ok, msg = False, f"{label} failed: {e}"
        st.session_state[f"result:{name}"] = (ok, msg)
        st.session_state[f"job:{name}"] = None
        st.session_state["last_active"] = time.time()
        st.rerun()
    else:
        st.info(f"⏳ {label}... {int(time.time() - job['started'])}s")


def show_result(name):
    result = st.session_state.get(f"result:{name}")
    if result:
        (st.success if result[0] else st.error)(result[1])


def _run_ingest(db_path, username):
    try:
        from pdf_ingest import ingest_zip_pdfs_to_sqlite

        ingest_zip_pdfs_to_sqlite("Downloads.zip", db_path)
        init_db(db_path)
        log_event(db_path, username, "PDF_INGEST", "Downloads.zip")
        return True, "PDF documents successfully indexed in SQLite!"
    except Exception as e:
        return False, f"PDF ingest failed: {e}"


def _md_escape(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~])", r"\\\1", text or "")


def _make_backup_bytes(db_path):
    tmp = tempfile.mktemp(suffix=".db")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        with open(tmp, "rb") as fh:
            return fh.read()
    finally:
        os.remove(tmp)


def verify_credentials(user: str, pwd: str) -> str | None:
    if user in MOCK_USERS:
        expected_pwd, role = MOCK_USERS[user]
        if hmac.compare_digest(pwd.encode(), expected_pwd.encode()):
            return role
    return None


def check_session_timeout():
    if "authenticated" not in st.session_state or not st.session_state["authenticated"]:
        return False
    current_time = time.time()
    last_active = st.session_state.get("last_active", current_time)

    if current_time - last_active > TIMEOUT_SECONDS:
        st.session_state["authenticated"] = False
        st.session_state["auth_timeout_expired"] = True
        st.rerun()
    return True


def client_activity_and_shortcuts():
    """Tracks user activity and binds Cmd/Ctrl+K to trigger a New Chat."""
    tracker_js = """
    <script>
    (function() {
        let lastSent = Date.now();
        const throttleInterval = 15000;
        function notifyActivity() {
            const now = Date.now();
            if (now - lastSent > throttleInterval) {
                lastSent = now;
                const url = new URL(window.parent.location.href);
                url.searchParams.set('user_active_ts', now);
                window.parent.history.replaceState({}, '', url);
            }
        }
        window.parent.document.addEventListener('mousemove', notifyActivity);
        window.parent.document.addEventListener('click', notifyActivity);
        window.parent.document.addEventListener('keydown', function(e) {
            notifyActivity();
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                const buttons = Array.from(window.parent.document.querySelectorAll('button'));
                const newChatBtn = buttons.find(b => b.innerText && b.innerText.includes('New Chat'));
                if (newChatBtn) newChatBtn.click();
            }
        });
    })();
    </script>
    """
    st.html(tracker_js, unsafe_allow_javascript=True)

    if client_ts := st.query_params.get("user_active_ts"):
        last_recorded = st.session_state.get("client_last_active_ts", 0)
        if client_ts != last_recorded:
            st.session_state["client_last_active_ts"] = client_ts
            st.session_state["last_active"] = time.time()


def check_password():
    if st.session_state.get("auth_timeout_expired", False):
        st.warning("⚠️ Your session expired due to inactivity. Please log in again.")
        st.session_state["auth_timeout_expired"] = False

    def password_entered():
        user = st.session_state.get("login_username", "").strip()
        pwd = st.session_state.get("login_password", "")
        role = verify_credentials(user, pwd)
        if role:
            st.session_state["authenticated"] = True
            st.session_state["current_user"] = user
            st.session_state["role"] = role
            st.session_state["last_active"] = time.time()
            st.session_state["auth_error"] = False
            log_event("database.db", user, "LOGIN", f"role={role}")
        else:
            st.session_state["authenticated"] = False
            st.session_state["auth_error"] = True
            log_event("database.db", user or "?", "LOGIN_FAILED", "")

    if st.session_state.get("authenticated", False):
        return check_session_timeout()

    st.markdown(
        "<h1 style='text-align: center;'>🔐 Government AI Reporting Platform</h1>",
        unsafe_allow_html=True,
    )
    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        with st.form("login_form"):
            st.text_input("Username", key="login_username")
            st.text_input("Password", type="password", key="login_password")
            st.form_submit_button("Login", on_click=password_entered, type="primary")
            if st.session_state.get("auth_error", False):
                st.error("😕 Invalid username or password.")
    return False


def render_sidebar_controls(db_path, user_role):
    with st.sidebar:
        username = st.session_state.get("current_user", "User")
        render_sidebar_threads(db_path, username)

        st.markdown("---")
        with st.expander("⚙️ Session & System Admin", expanded=False):
            st.success(f"User: **{username}** | Role: **{user_role.upper()}**")

            elapsed = time.time() - st.session_state.get("last_active", time.time())
            remaining = max(0, int(TIMEOUT_SECONDS - elapsed))
            m, s = divmod(remaining, 60)
            st.info(f"⏱️ Session Timeout in: {m:02d}:{s:02d}")
            if st.button("🔄 Extend Session"):
                st.session_state["last_active"] = time.time()
                st.rerun()

            if user_role == "admin":
                if USING_DEFAULT_PASSWORDS:
                    st.warning("⚠️ Default passwords are in use. Set Secrets.")
                st.subheader("📁 Government Documents")
                if st.button(
                    "📥 Ingest PDFs from Downloads.zip", disabled=job_running("ingest")
                ):
                    if not os.path.exists("Downloads.zip"):
                        st.error("Downloads.zip not found in the app folder.")
                    else:
                        start_job("ingest", _run_ingest, db_path, username)
                        st.rerun()
                poll_job("ingest", "Ingesting PDFs")
                show_result("ingest")

                st.subheader("💾 Backup / Restore DB")
                if st.button("📦 Prepare backup"):
                    st.session_state["backup_bytes"] = _make_backup_bytes(db_path)
                    log_event(db_path, username, "DB_BACKUP", "")
                if st.session_state.get("backup_bytes"):
                    st.download_button(
                        "⬇️ Download backup",
                        data=st.session_state["backup_bytes"],
                        file_name=f"database_backup_{time.strftime('%Y%m%d_%H%M')}.db",
                        mime="application/octet-stream",
                    )

                up_file = st.file_uploader("Upload Backup", type=["zip", "db"])
                if up_file is not None and st.button("🛡️ Validate & Restore"):
                    success, msg = validate_and_restore_sqlite(up_file, db_path)
                    if success:
                        log_event(db_path, username, "DB_RESTORE", up_file.name)
                        st.success(msg)
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(msg)

            if st.button("🚪 Logout", use_container_width=True):
                log_event(db_path, username, "LOGOUT", "")
                st.session_state["authenticated"] = False
                st.rerun()


def render_web_sources_tab(db_path, user_role, username):
    st.header("🌐 Website Sources")
    st.caption(
        "The AI assistant reads the websites listed here when it answers questions in the chat tab."
    )
    is_admin = user_role == "admin"
    busy = job_running("web")

    if is_admin:
        with st.form("add_web_form", clear_on_submit=True):
            new_url = st.text_input(
                "Website link", placeholder="https://www.example.gov.lk/services"
            )
            submitted = st.form_submit_button("➕ Add & Fetch", disabled=busy)
        if submitted:
            ok, result = validate_url(new_url)
            if not ok:
                st.error(result)
            else:
                start_job("web", add_or_refresh_source, db_path, result, username)
                st.rerun()
        poll_job("web", "Fetching website")
        show_result("web")
    else:
        st.info(
            "Only admins can add or remove website links. You can ask the chat about them."
        )

    sources = list_sources(db_path)
    st.subheader(f"Added websites ({len(sources)})")
    if not sources:
        st.info("No websites added yet.")
    for src in sources:
        with st.container(border=True):
            if is_admin:
                info_col, refresh_col, remove_col = st.columns([6, 1, 1])
            else:
                info_col, refresh_col, remove_col = st.container(), None, None
            with info_col:
                st.markdown(f"**{_md_escape(src['title'] or src['url'])}**")
                st.text(src["url"])
                st.caption(
                    f"{src['chunk_count']} sections · last fetched {src['last_fetched']} UTC · "
                    f"added by {src['added_by']}"
                )
            if is_admin:
                with refresh_col:
                    if st.button(
                        "🔄",
                        key=f"web_refresh_{src['id']}",
                        help="Fetch this page again",
                        disabled=busy,
                    ):
                        start_job(
                            "web", add_or_refresh_source, db_path, src["url"], username
                        )
                        st.rerun()
                with remove_col:
                    if st.button(
                        "🗑️",
                        key=f"web_del_{src['id']}",
                        help="Remove this website",
                        disabled=busy,
                    ):
                        delete_source(db_path, src["id"], username)
                        st.rerun()


@st.cache_data(show_spinner=False, max_entries=32)
def _item_image(db_path: str, item_id: int):
    return library.get_item_image(db_path, item_id)


def render_library_tab(db_path, user_role, username, llm_cfg):
    st.header("🖼️ Image & PDF Library")
    st.caption(
        "Images and PDF pages added here can be found by the chat assistant, which shows "
        "the matching pictures. Try asking: “show me the passport application form”."
    )
    is_admin = user_role == "admin"
    busy = job_running("lib")
    vision_model = st.session_state.get("vision_model") or llm_cfg["vision_model"]

    if is_admin:
        with st.expander("🤖 AI description settings"):
            st.text_input(
                "Vision model", value=llm_cfg["vision_model"], key="vision_model"
            )
            if llm_cfg["provider"] == "ollama":
                st.caption(
                    "AI descriptions make pictures searchable by what they show (and read scanned PDF pages). "
                    "Install a vision model once in Git Bash, e.g.  ollama pull llava  "
                    "(smaller and faster:  ollama pull moondream)."
                )
            else:
                st.caption(
                    "AI descriptions use your hosted provider. Enter a model that accepts images "
                    "(or set LLM_VISION_MODEL in Secrets)."
                )
        with st.form("lib_upload_form", clear_on_submit=True):
            uploads = st.file_uploader(
                "Add images or PDF files",
                type=["png", "jpg", "jpeg", "webp", "gif", "pdf"],
                accept_multiple_files=True,
            )
            desc = st.text_area(
                "Description / keywords (optional, applied to every file in this upload)",
                placeholder="e.g. passport application form, Department of Immigration",
                height=80,
            )
            auto = st.checkbox(
                "Describe images with AI after upload (slower, needs a vision model)"
            )
            submitted = st.form_submit_button("⬆️ Upload", disabled=busy)
        if submitted:
            if not uploads:
                st.error("Please choose at least one file.")
            else:
                payload = [(f.name, f.getvalue()) for f in uploads]
                start_job(
                    "lib",
                    library.process_uploads,
                    db_path,
                    payload,
                    desc,
                    username,
                    auto,
                    llm_cfg,
                    vision_model,
                )
                st.rerun()
        poll_job("lib", "Processing files")
        show_result("lib")
    else:
        st.info(
            "Only admins can add or remove files. You can ask the chat to show them."
        )

    files = library.list_files(db_path)
    st.subheader(f"Library ({len(files)} files)")
    if not files:
        st.info("No files added yet.")

    for f in files:
        fid = f["id"]
        with st.container(border=True):
            if is_admin:
                info_col, ai_col, del_col = st.columns([6, 2, 1])
            else:
                info_col, ai_col, del_col = st.container(), None, None

            with info_col:
                st.markdown(f"**{_md_escape(f['title'])}**")
                if f["kind"] == "pdf":
                    scanned = f["items"] - f["text_pages"]
                    st.caption(
                        f"PDF · {f['items']} pages · {f['text_pages']} with text · "
                        f"{f['ai_pages']} read by AI · added by {f['added_by']}"
                    )
                    if scanned > f["ai_pages"]:
                        st.caption(
                            "⚠️ Some scanned pages have no searchable text yet. Use the 🤖 button."
                        )
                else:
                    st.caption(
                        f"Image · {'AI described' if f['ai_pages'] else 'no AI description'} · "
                        f"added by {f['added_by']}"
                    )
                if f["description"]:
                    st.text(f["description"])

            if is_admin:
                pending = library.pending_description_ids(db_path, fid)
                with ai_col:
                    if pending and st.button(
                        f"🤖 Describe ({len(pending)})",
                        key=f"lib_ai_{fid}",
                        disabled=busy,
                        help="Use the vision model to describe / read these items so the chatbot can find them",
                    ):
                        start_job(
                            "lib",
                            library.describe_items,
                            db_path,
                            pending,
                            llm_cfg,
                            vision_model,
                            username,
                        )
                        st.rerun()
                with del_col:
                    if st.button(
                        "🗑️",
                        key=f"lib_del_{fid}",
                        help="Remove this file",
                        disabled=busy,
                    ):
                        library.delete_file(db_path, fid, username)
                        _item_image.clear()
                        st.rerun()

            if st.checkbox("👁️ Preview", key=f"lib_prev_{fid}"):
                page = 1
                if f["items"] > 1:
                    page = st.number_input(
                        "Page", 1, f["items"], 1, key=f"lib_page_{fid}"
                    )
                item_id = library.get_item_id(db_path, fid, int(page))
                data = _item_image(db_path, item_id) if item_id else None
                if data:
                    st.image(data, width=480)
                else:
                    st.info(
                        "No picture available (PDF page pictures need the pypdfium2 package)."
                    )

            if is_admin:
                with st.expander("✏️ Edit title / description"):
                    new_title = st.text_input(
                        "Title", value=f["title"], key=f"lib_title_{fid}"
                    )
                    new_desc = st.text_area(
                        "Description / keywords (the chatbot searches this)",
                        value=f["description"],
                        key=f"lib_desc_{fid}",
                    )
                    if st.button("💾 Save", key=f"lib_save_{fid}"):
                        library.update_file(db_path, fid, new_title, new_desc, username)
                        st.rerun()


def main():
    st.set_page_config(
        page_title="Government AI Workspace", page_icon="💬", layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_user_friendly_styles()
    db_path = "database.db"
    init_db(db_path)
    init_chat_tables(db_path)

    # Read-only public share link view (bypasses login for shared snapshots)
    share_token = st.query_params.get("share")
    if share_token:
        render_shared_thread_view(db_path, share_token)
        return

    if not check_password():
        return

    # Pause 1-second auto-refresh while actively streaming an AI response so stream isn't cut off
    if not st.session_state.get("is_streaming", False):
        st_autorefresh(interval=1000, limit=None, key="timeout_ticker")
    client_activity_and_shortcuts()

    user_role = st.session_state.get("role", "analyst")
    username = st.session_state.get("current_user", "User")

    render_sidebar_controls(db_path, user_role)

    fallback_keys = [
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_VISION_MODEL",
    ]
    secret_keys = getattr(llm, "SECRET_KEYS", fallback_keys)
    llm_cfg = llm.load_config(_read_secrets(secret_keys))

    st.markdown(
        """
        <section class="gov-hero">
          <p class="gov-hero-title">🏛️ Government AI Workspace</p>
          <p class="gov-hero-sub">A simpler place to ask questions, explore official information, and work with documents and data.</p>
          <span class="gov-chip">💬 AI Assistant</span><span class="gov-chip">📚 Document Search</span>
          <span class="gov-chip">🔐 Role-based access</span>
        </section>
        """,
        unsafe_allow_html=True,
    )
    tab1, tab_web, tab_lib, tab2 = st.tabs(
        [
            "💬 Chat Assistant",
            "🌐 Website Sources",
            "📚 Document Library",
            "📊 Data Workbench",
        ]
    )
    with tab1:
        render_ai_chat_interface(db_path, llm_cfg, user_role, username)
    with tab_web:
        render_web_sources_tab(db_path, user_role, username)
    with tab_lib:
        render_library_tab(db_path, user_role, username, llm_cfg)
    with tab2:
        st.header("📊 Database Workbench")
        st.info("Run read-only SELECT queries against the database.")
        if user_role != "admin":
            st.warning("The Database Workbench is available to admins only.")
        else:
            sql = st.text_area(
                "SQL query",
                value="SELECT * FROM audit_logs ORDER BY id DESC LIMIT 50;",
                height=120,
            )
            if st.button("▶️ Run Query"):
                if not is_safe_select(sql):
                    st.error("Only a single read-only SELECT statement is allowed.")
                else:
                    try:
                        conn = sqlite3.connect(db_path)
                        try:
                            result_df = pd.read_sql_query(sql, conn)
                        finally:
                            conn.close()
                        log_event(db_path, username, "WORKBENCH_QUERY", sql[:500])
                        st.dataframe(result_df, width="stretch")
                    except Exception as e:
                        st.error(f"Query failed: {e}")


if __name__ == "__main__":
    main()
