import hmac
import os
import re
import tempfile
import time
import hashlib
import secrets
import streamlit.components.v1 as components
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import sqlite3
import requests
import html
import streamlit as st
import persist
import ui_theme
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
from holomotion_tab import render_holomotion_tab
from scan_ui import render_scan_tab
from math_links import MATH_LINKS, add_many_sources
from web_sources import add_or_refresh_source, delete_source, list_sources, validate_url

TIMEOUT_SECONDS = 300

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


def save_now():
    """Push the DB to permanent storage right away (after an add / delete / edit)."""
    cfg = st.session_state.get("persist_cfg")
    if cfg:
        persist.maybe_save(cfg, "database.db", force=True)


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
        save_now()
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


def _password_digest(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 260000).hex()


_APP_USERS_SQL = """
    CREATE TABLE app_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL COLLATE NOCASE UNIQUE,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'analyst', 'user')),
        created_by TEXT NOT NULL DEFAULT 'system',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        active INTEGER NOT NULL DEFAULT 1
    )
"""


def _migrate_app_users(db_path: str) -> None:
    """Older databases only allowed the roles admin/analyst. Rebuild the table so ordinary 'user' accounts work.
    Existing accounts, passwords and ids are kept."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='app_users'").fetchone()
        if not row or "'user'" in (row[0] or ""):
            return
        conn.execute("BEGIN")
        try:
            conn.execute("ALTER TABLE app_users RENAME TO app_users_old")
            conn.execute(_APP_USERS_SQL)
            conn.execute(
                "INSERT INTO app_users (id,username,password_hash,password_salt,role,created_by,created_at,active) "
                "SELECT id,username,password_hash,password_salt,role,created_by,created_at,active FROM app_users_old"
            )
            conn.execute("DROP TABLE app_users_old")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _seed_default_users(db_path: str) -> None:
    """Create the two initial accounts once; existing database accounts are preserved."""
    conn = sqlite3.connect(db_path)
    try:
        # Create the user table here as well as in init_db(). This makes startup
        # resilient when Streamlit Cloud is running an older db_utils.py module.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'analyst', 'user')),
                created_by TEXT NOT NULL DEFAULT 'system',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        for username, (password, role) in MOCK_USERS.items():
            exists = conn.execute("SELECT 1 FROM app_users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
            if not exists:
                salt = secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO app_users (username,password_hash,password_salt,role,created_by) VALUES (?,?,?,?,?)",
                    (username, _password_digest(password, salt), salt, role, "system"),
                )
        conn.commit()
    finally:
        conn.close()
    _migrate_app_users(db_path)


def verify_credentials(user: str, pwd: str) -> str | None:
    conn = sqlite3.connect("database.db")
    try:
        row = conn.execute(
            "SELECT password_hash,password_salt,role FROM app_users WHERE username = ? COLLATE NOCASE AND active = 1",
            (user,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        candidate = _password_digest(pwd, row[1])
        return row[2] if hmac.compare_digest(candidate, row[0]) else None
    return None


def render_user_management(db_path: str, username: str, role: str) -> None:
    st.header("👥 User Management")
    st.caption(
        "Admin and Analyst accounts can add ordinary **User** accounts. Users can use only "
        "AI Workspace & Chat, Scan and HoloMotion 3D; every other part of the app is hidden from them."
    )
    # Who may create which role (an Analyst cannot create an Admin, so nobody can promote themselves).
    creatable = ["user", "analyst", "admin"] if role == "admin" else ["user", "analyst"]
    with st.form("create_platform_user", clear_on_submit=True):
        new_username = st.text_input("New username", max_chars=40).strip()
        new_password = st.text_input("Temporary password", type="password")
        confirm_password = st.text_input("Confirm password", type="password")
        new_role = st.selectbox(
            "Account role", creatable, index=0,
            help="user = AI Workspace & Chat, Scan, HoloMotion 3D only. analyst = most tabs. admin = everything.",
        )
        submitted = st.form_submit_button("➕ Create account", type="primary")
    if submitted:
        if role not in ("admin", "analyst"):
            st.error("You are not allowed to create users.")
        elif new_role not in creatable:
            st.error("You are not allowed to create that kind of account.")
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", new_username):
            st.error("Username must be 3–40 characters: letters, numbers, dot, underscore or hyphen.")
        elif len(new_password) < 10:
            st.error("Password must contain at least 10 characters.")
        elif new_password != confirm_password:
            st.error("Passwords do not match.")
        else:
            salt = secrets.token_hex(16)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "INSERT INTO app_users (username,password_hash,password_salt,role,created_by) VALUES (?,?,?,?,?)",
                    (new_username, _password_digest(new_password, salt), salt, new_role, username),
                )
                conn.commit()
                log_event(db_path, username, "USER_CREATED", f"username={new_username}; role={new_role}")
                save_now()
                st.session_state["user_mgmt_flash"] = f"Account '{new_username}' ({new_role}) created. Give them the username and the temporary password."
            except sqlite3.IntegrityError:
                st.error("That username already exists.")
            except sqlite3.Error as exc:
                st.error(f"Could not create the account: {exc}")
            finally:
                conn.close()
            if st.session_state.get("user_mgmt_flash"):
                st.rerun()
    flash = st.session_state.pop("user_mgmt_flash", None)
    if flash:
        st.success(flash)

    st.subheader("Registered users")
    conn = sqlite3.connect(db_path)
    try:
        users = pd.read_sql_query(
            "SELECT username, role, created_by, created_at, active FROM app_users ORDER BY username COLLATE NOCASE", conn
        )
    finally:
        conn.close()
    shown = users.copy()
    shown["active"] = shown["active"].map({1: "yes", 0: "disabled"})
    st.dataframe(shown, use_container_width=True, hide_index=True)

    # Enable / disable an account (a disabled account cannot log in). Admin: anyone; Analyst: ordinary users only.
    manageable = [
        r for r in users.itertuples()
        if r.username.lower() != username.lower() and (role == "admin" or r.role == "user")
    ]
    if manageable:
        with st.expander("Disable or re-enable an account"):
            labels = {f"{r.username} ({r.role}, {'active' if r.active else 'disabled'})": r for r in manageable}
            choice = st.selectbox("Account", list(labels.keys()), key="um_pick")
            target = labels[choice]
            new_state = 0 if target.active else 1
            if st.button("🔒 Disable this account" if target.active else "🔓 Re-enable this account", key="um_toggle"):
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("UPDATE app_users SET active=? WHERE username=? COLLATE NOCASE", (new_state, target.username))
                    conn.commit()
                finally:
                    conn.close()
                log_event(db_path, username, "USER_ENABLED" if new_state else "USER_DISABLED", f"username={target.username}")
                save_now()
                st.rerun()


def _render_local_screen_preview() -> None:
    st.info("Screen sharing starts only after you select a screen/window and approve the browser prompt. Nothing is captured or transmitted to the server by this preview.")
    monitor_html = r'''<!doctype html><html><head><style>
      body{font-family:Arial,sans-serif;margin:0;color:var(--text-color,#222)}
      button{padding:10px 16px;border:0;border-radius:8px;background:#1769aa;color:white;font-weight:600;cursor:pointer;margin:4px}
      button.stop{background:#a83232} video{width:100%;max-height:520px;background:#111;border-radius:10px;margin-top:10px}
      #status{font-size:13px;margin:8px 4px}
    </style></head><body>
      <button id="start">▶ Start Screen Monitor</button><button id="stop" class="stop" disabled>■ Stop</button>
      <div id="status">Not sharing. Click Start and choose a screen or window.</div><video id="preview" autoplay playsinline muted></video>
      <script>
      const start=document.getElementById('start'), stop=document.getElementById('stop'), video=document.getElementById('preview'), status=document.getElementById('status'); let stream=null;
      function finish(){if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}video.srcObject=null;start.disabled=false;stop.disabled=true;status.textContent='Screen sharing stopped.';}
      start.addEventListener('click',async()=>{try{if(!navigator.mediaDevices?.getDisplayMedia)throw new Error('Screen capture is not supported. Use Chrome or Edge over HTTPS or localhost.');stream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:false});video.srcObject=stream;start.disabled=true;stop.disabled=false;status.textContent='Screen preview active in this browser only.';stream.getVideoTracks()[0].addEventListener('ended',finish);}catch(e){status.textContent='Could not start: '+e.message;}});
      stop.addEventListener('click',finish);window.addEventListener('beforeunload',finish);
      </script></body></html>'''
    components.html(monitor_html, height=610, scrolling=False)



def _screen_share_reason() -> str:
    """Why screen sharing is being asked for. An org can override this (Streamlit Secrets:
    SCREEN_MONITOR_REASON) instead of editing code, so the real reason is always visible here."""
    sec = _read_secrets(["SCREEN_MONITOR_REASON"])
    custom = str(sec.get("SCREEN_MONITOR_REASON") or os.environ.get("SCREEN_MONITOR_REASON") or "").strip()
    return custom or (
        "To help with remote troubleshooting and to check in on shared/public terminals "
        "(for example, a help-desk or front-counter PC) without someone having to be there in person."
    )


def _render_share_invite(kind: str, link_label: str, link_url: str, steps: list[str]) -> None:
    """One consistent 'Share your screen?' onboarding card: link, reason, and a clear opt-out.
    Used for both the computer and the phone monitor tabs so the invitation always looks the same."""
    with st.container(border=True):
        st.markdown(f"#### 🔗 Share your {kind} screen?")
        st.write(
            f"An admin or analyst can see your {kind} screen **only after you choose to share it here** - "
            "nothing is visible before that, and nothing else about your account or access changes "
            "whether you share it or not."
        )
        st.markdown(f"**Why it's being asked:** {_screen_share_reason()}")
        if link_url:
            c1, c2 = st.columns([1, 2])
            with c1:
                st.link_button(link_label, link_url)
            with c2:
                st.code(link_url, language=None)
        if steps:
            st.markdown("\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1)))
        st.markdown(
            "**If you'd rather not:** that's completely fine - just don't open the link (or press "
            "Stop if you already started). Declining does not lock you out of this app or anything else; "
            "nobody is notified, and no part of the platform changes for you either way."
        )


def render_screen_monitor(key_prefix: str = "tab", username: str = "User") -> None:
    """Admin/analyst view of OTHER computers whose users chose to share their screen."""
    st.header("🖥️ Screen Monitor")
    st.caption("Computers that are sharing their screen with you • live snapshots every few seconds")
    base_url, api_key = _mobile_monitor_config()
    if not base_url or not api_key:
        st.warning("The monitor service is not configured yet. Set MOBILE_MONITOR_API_URL and MOBILE_MONITOR_API_KEY in Streamlit Cloud → Manage app → Settings → Secrets (the same service the phone monitor uses).")
    else:
        st.markdown("#### ⚡ Auto Screen Monitoring")
        st.caption(f"Signed-in platform account: **{username}**. The saved pairing key will be supplied securely; you will not need to type it again.")
        launch_state_key = f"{key_prefix}_pc_auto_launch_url"
        if st.button("⚡ Auto Screen Monitoring", key=f"{key_prefix}_pc_auto_start", type="primary"):
            try:
                launch = requests.post(
                    f"{base_url}/v1/pc/launch",
                    headers={"X-Monitor-Key": api_key},
                    json={"login_user": str(username)[:80]},
                    timeout=10,
                )
                launch.raise_for_status()
                launch_url = str(launch.json().get("url") or "")
                if not launch_url:
                    raise ValueError("The monitor service did not return a launch URL.")
                if launch_url.startswith("/"):
                    launch_url = base_url + launch_url
                st.session_state[launch_state_key] = launch_url
                st.success("Your secure sharing link is ready. Open it on the computer you want to share.")
            except Exception as exc:
                st.error(f"Could not prepare automatic screen monitoring: {exc}")
        if st.session_state.get(launch_state_key):
            st.link_button("🖥️ Open Auto Screen Monitoring", st.session_state[launch_state_key], use_container_width=True)
            st.caption("The link expires after 5 minutes and can be used once. On the PC, click Start and approve the browser's screen-sharing prompt.")
        st.divider()
        _pc_monitor_live(base_url, api_key, key_prefix)
    with st.expander("Preview THIS computer's screen (stays in your own browser)"):
        _render_local_screen_preview()


@st.fragment(run_every=3)
def _pc_monitor_live(base_url: str, api_key: str, key_prefix: str) -> None:
    """Live list + latest picture of the computers that are sharing (re-runs by itself, not the whole page)."""
    headers = {"X-Monitor-Key": api_key}
    try:
        response = requests.get(f"{base_url}/v1/devices", headers=headers, timeout=8)
        if response.status_code == 401:
            st.error("The monitor service rejected the key. MOBILE_MONITOR_API_KEY in the Streamlit secrets must equal the one on the service.")
            return
        response.raise_for_status()
        computers = [d for d in response.json().get("devices", []) if d.get("kind") == "pc"]
    except Exception as exc:
        st.error(f"Could not connect to the monitor service: {exc}")
        st.caption("A free Render service sleeps when idle and can take about a minute to wake up. This view retries automatically.")
        return

    if not computers:
        st.caption("No computer is sharing its screen with you right now.")
        _render_share_invite(
            kind="computer",
            link_label="🖥️ Open the sharing page",
            link_url=f"{base_url}/pc",
            steps=[
                "Open the link above in Chrome or Edge, on the computer you want to share.",
                "Type a name for the computer and enter the pairing key.",
                "Press **Start sharing this screen**, then choose a screen/window and approve the browser prompt.",
                "It appears here within a few seconds. Press **Stop sharing** at any time to end it.",
            ],
        )
        return

    import pandas as pd
    st.dataframe(
        pd.DataFrame([{
            "Computer": d.get("device_name") or d.get("device_id", "Unknown"),
            "Logged-in account": d.get("login_user") or "—",
            "System": d.get("manufacturer", "—"),
            "Browser": d.get("model", "—"),
            "Screen": d.get("screen_resolution", "—"),
            "Status": "Online" if d.get("online") else "Offline",
            "Last seen (UTC)": d.get("last_seen", "—"),
        } for d in computers]),
        use_container_width=True, hide_index=True,
    )
    choices = {f"{d.get('device_name') or d.get('device_id')}": d.get("device_id") for d in computers}
    label = st.selectbox("Select a computer", list(choices.keys()), key=f"{key_prefix}_pc_select")
    selected_id = choices[label]
    try:
        frame = requests.get(f"{base_url}/v1/devices/{selected_id}/frame", headers=headers, timeout=10)
        if frame.status_code == 200:
            st.image(frame.content, caption=f"{label} • latest picture (updates every few seconds)", use_container_width=True)
        elif frame.status_code == 404:
            st.info("Connected, but no picture yet. The person must press Start sharing and choose a screen.")
        else:
            st.warning(f"Picture unavailable (HTTP {frame.status_code}).")
    except Exception as exc:
        st.warning(f"Could not load the picture: {exc}")
    if st.button("🗑️ Disconnect and delete this computer's picture", key=f"{key_prefix}_pc_forget"):
        try:
            requests.delete(f"{base_url}/v1/devices/{selected_id}", headers=headers, timeout=8)
        except Exception:
            pass
        st.rerun(scope="fragment")
    st.caption("View only: no remote control, no sound, no camera, no microphone, no keyboard or file access.")


def _mobile_monitor_config():
    """Return the separately hosted Android monitor service settings."""
    sec = _read_secrets(["MOBILE_MONITOR_API_URL", "MOBILE_MONITOR_API_KEY"])
    base_url = str(sec.get("MOBILE_MONITOR_API_URL") or os.environ.get("MOBILE_MONITOR_API_URL") or "").strip().rstrip("/")
    api_key = str(sec.get("MOBILE_MONITOR_API_KEY") or os.environ.get("MOBILE_MONITOR_API_KEY") or "").strip()
    return base_url, api_key


def render_mobile_phone_monitor(db_path: str, username: str, key_prefix: str = "tab") -> None:
    """Admin/analyst dashboard for explicitly consented Android companion devices.

    key_prefix keeps widget ids unique because this view can be drawn twice (sidebar panel + tab).
    """
    st.header("📱 Mobile Phone Monitor")
    st.caption("Connected Android devices • live screen snapshots • device telemetry")
    st.info(
        "Phone owners must install the companion app, enter the service URL and pairing key, "
        "and explicitly approve Android's screen-capture permission. Monitoring stops when "
        "they stop sharing or revoke permission."
    )
    base_url, api_key = _mobile_monitor_config()
    if not base_url or not api_key:
        st.warning("Mobile monitoring service is not configured yet. Set MOBILE_MONITOR_API_URL and MOBILE_MONITOR_API_KEY in Streamlit Cloud → Manage app → Settings → Secrets.")
        with st.expander("Configuration example"):
            st.code('MOBILE_MONITOR_API_URL = "https://your-monitor-service.example.com"\nMOBILE_MONITOR_API_KEY = "use-a-long-random-secret"', language="toml")
        st.markdown("The companion API is included in `mobile_monitor_service/`. Deploy it separately (for example, on a private HTTPS Render/Railway service), then add its URL and the same secret here.")
        return
    _mobile_monitor_live(base_url, api_key, key_prefix)


@st.fragment(run_every=4)
def _mobile_monitor_live(base_url: str, api_key: str, key_prefix: str) -> None:
    """Live part of the dashboard. It re-runs by itself every 4 s WITHOUT re-running the whole page
    (a whole-page st_autorefresh made buttons elsewhere lose clicks and slowed every page run)."""
    headers = {"X-Monitor-Key": api_key}
    try:
        response = requests.get(f"{base_url}/v1/devices", headers=headers, timeout=8)
        if response.status_code == 401:
            st.error("The monitor service rejected the key. MOBILE_MONITOR_API_KEY in the Streamlit secrets must equal the one set on the monitor service.")
            return
        response.raise_for_status()
        devices = [d for d in response.json().get("devices", []) if d.get("kind", "android") != "pc"]
    except Exception as exc:
        st.error(f"Could not connect to the mobile monitoring service: {exc}")
        st.caption("A free Render service sleeps when idle and can take about a minute to wake up. This view retries automatically.")
        return

    if not devices:
        st.caption("No phone is sharing its screen with you right now.")
        _render_share_invite(
            kind="phone",
            link_label="📱 How to share (companion app)",
            link_url="",
            steps=[
                "Install the Government AI Mobile Monitor companion app (ask your admin for the APK).",
                "Open it and enter the service URL and the pairing key your admin gave you.",
                "Press **TEST CONNECTION** first - it tells you if the URL or key is wrong.",
                "Press **START SCREEN SHARING** and approve Android's permission prompt.",
                "It appears here within a few seconds. Press **STOP SCREEN SHARING** on the phone at any time.",
            ],
        )
        with st.expander("Phone not showing up? Check these"):
            try:
                ok = requests.get(f"{base_url}/health", timeout=5).status_code == 200
            except Exception:
                ok = False
            st.markdown(
                f"- Monitor service reachable from this app: **{'yes ✅' if ok else 'no ❌ (it may be waking up)'}**\n"
                "- The pairing key must be identical in 3 places: the phone app, `MOBILE_MONITOR_API_KEY` on Render, "
                "and `MOBILE_MONITOR_API_KEY` in the Streamlit secrets."
            )
        return

    import pandas as pd
    rows = []
    for device in devices:
        rows.append({
            "Device": device.get("device_name") or device.get("device_id", "Unknown"),
            "Model": device.get("model", "—"),
            "Android": device.get("android_version", "—"),
            "Battery": f"{device.get('battery_percent')}%" if device.get("battery_percent") is not None else "—",
            "Network": device.get("network_type", "—"),
            "Screen": device.get("screen_resolution", "—"),
            "Status": "Online" if device.get("online") else "Offline",
            "Last seen (UTC)": device.get("last_seen", "—"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    choices = {
        f"{d.get('device_name') or d.get('device_id')} ({d.get('model', 'Android')})": d.get("device_id")
        for d in devices
    }
    selected_label = st.selectbox("Select Android phone", list(choices.keys()), key=f"{key_prefix}_mm_device")
    selected_id = choices[selected_label]
    selected = next((d for d in devices if d.get("device_id") == selected_id), {})
    left, right = st.columns(2)
    with left:
        st.metric("Battery", f"{selected.get('battery_percent')}%" if selected.get("battery_percent") is not None else "Unknown")
        st.write("**Connection:**", "Online" if selected.get("online") else "Offline")
        st.write("**Manufacturer:**", selected.get("manufacturer", "—"))
        st.write("**Android version:**", selected.get("android_version", "—"))
        st.write("**Resolution:**", selected.get("screen_resolution", "—"))
    with right:
        st.write("**Network:**", selected.get("network_type", "—"))
        st.write("**Device ID:**", selected_id)
        st.write("**Last seen (UTC):**", selected.get("last_seen", "—"))
        st.caption("Screen images are short-lived snapshots held by the monitor service in memory; they are not archived by this dashboard.")
    try:
        frame_response = requests.get(f"{base_url}/v1/devices/{selected_id}/frame", headers=headers, timeout=10)
        if frame_response.status_code == 200:
            st.subheader("📺 Phone Screen Preview")
            st.image(frame_response.content, caption="Latest screen frame • updates every few seconds", use_container_width=True)
        elif frame_response.status_code == 404:
            st.info("No screen frame received yet. On the phone, press Start Screen Sharing and approve Android's permission prompt.")
        else:
            st.warning(f"Screen preview unavailable (HTTP {frame_response.status_code}).")
    except Exception as exc:
        st.warning(f"Could not load the screen preview: {exc}")
    st.caption("For privacy, this dashboard does not provide remote control, microphone access, camera access, or hidden monitoring.")



@st.fragment(run_every=5)
def render_pc_system_monitor(key_prefix: str = "pcmon") -> None:
    # Dashboard for metrics voluntarily sent by installed PC monitoring agents.
    st.header("📈 PC System Monitoring")
    st.caption("Hardware • Network • System activity from computers whose owners started the monitoring agent")
    base_url, api_key = _mobile_monitor_config()
    if not base_url or not api_key:
        st.warning("Configure MOBILE_MONITOR_API_URL and MOBILE_MONITOR_API_KEY in Streamlit secrets before connecting PCs.")
        return
    headers = {"X-Monitor-Key": api_key}
    try:
        response = requests.get(f"{base_url}/v1/devices", headers=headers, timeout=8)
        response.raise_for_status()
        devices = [d for d in response.json().get("devices", []) if d.get("kind") == "pc"]
    except Exception as exc:
        st.error(f"Could not connect to the monitoring service: {exc}")
        return
    if not devices:
        st.info("No PC monitoring agents are connected yet.")
        with st.expander("Connect a Windows PC", expanded=True):
            st.markdown(
                "1. Install Python 3.10+ on the computer being monitored.\n"
                "2. Copy `mobile_monitor_service/pc_monitor_agent.py` to that computer.\n"
                "3. Install its dependency with `py -m pip install psutil requests`.\n"
                "4. Start it with the service URL and the same pairing key used by this dashboard.\n"
                "5. Keep the agent running while monitoring is required. Press Ctrl+C to stop."
            )
            st.code('''py -m pip install psutil requests
py pc_monitor_agent.py --url "https://YOUR-MONITOR-SERVICE" --key "YOUR-PAIRING-KEY" --name "Office PC"''', language="powershell")
        st.caption("The agent sends telemetry only while running. It does not capture screens, audio, keystrokes or allow remote control.")
        return

    choices = {f"{d.get('device_name', 'PC')} — {'Online' if d.get('online') else 'Offline'}": d["device_id"] for d in devices}
    selected_label = st.selectbox("Select computer", list(choices), key=f"{key_prefix}_device")
    selected_id = choices[selected_label]
    try:
        result = requests.get(f"{base_url}/v1/pc/metrics", params={"device_id": selected_id}, headers=headers, timeout=8)
        if result.status_code == 404:
            st.warning("This computer is registered but has not sent telemetry yet. Start or restart its PC monitoring agent.")
            return
        result.raise_for_status()
        data = result.json().get("metrics", {})
    except Exception as exc:
        st.error(f"Could not load PC metrics: {exc}")
        return

    st.caption(f"Computer: {data.get('hostname', '—')} · {data.get('platform', '—')} · Last update: {data.get('captured_at', '—')}")
    st.subheader("1. Hardware Monitoring")
    c = st.columns(4)
    c[0].metric("CPU Usage", f"{data.get('cpu_percent', 0):.1f}%")
    c[1].metric("RAM Usage", f"{data.get('ram_percent', 0):.1f}%", f"{data.get('ram_used_mb', 0)} / {data.get('ram_total_mb', 0)} MB")
    c[2].metric("Disk Usage", f"{data.get('disk_percent', 0):.1f}%", f"{data.get('disk_used_gb', 0)} / {data.get('disk_total_gb', 0)} GB")
    temp = data.get("cpu_temperature_c")
    c[3].metric("CPU Temperature", f"{temp:.1f} °C" if isinstance(temp, (int, float)) else "Unavailable")
    gpu = data.get("gpu")
    if gpu:
        st.metric(f"GPU Usage · {gpu.get('name', 'GPU')}", f"{gpu.get('usage_percent', 0):.1f}%")
        if "memory_used_mb" in gpu:
            g1, g2 = st.columns(2)
            g1.metric("GPU Memory", f"{gpu.get('memory_used_mb', 0):.0f} / {gpu.get('memory_total_mb', 0):.0f} MB")
            g2.metric("GPU Temperature", f"{gpu.get('temperature_c', 0):.1f} °C")
    else:
        st.caption("GPU telemetry is unavailable. Windows GPU usage uses GPU Engine counters when supported; NVIDIA systems can use nvidia-smi. CPU temperature depends on hardware/OS sensor support.")

    st.subheader("2. Network Monitoring")
    n1, n2, n3, n4 = st.columns(4)
    n1.metric("Connection", "Connected" if data.get("internet_connected") else "No active interface")
    n2.metric("Download Speed", f"{data.get('download_bytes_per_sec', 0) / 1048576:.2f} MB/s")
    n3.metric("Upload Speed", f"{data.get('upload_bytes_per_sec', 0) / 1048576:.2f} MB/s")
    n4.metric("Total Traffic", f"↓ {data.get('download_total_mb', 0):.1f} / ↑ {data.get('upload_total_mb', 0):.1f} MB")

    st.subheader("3. System Activity Monitoring")
    st.write(f"**Uptime:** {int(data.get('uptime_seconds', 0) // 3600)} hours · **CPU cores:** {data.get('cpu_count', '—')}")
    st.markdown("**Top running processes**")
    processes = data.get("processes", [])
    if processes:
        st.dataframe(pd.DataFrame(processes), use_container_width=True, hide_index=True)
    else:
        st.caption("No process details received.")
    st.markdown("**Recent Windows error events (last 24 hours)**")
    errors = data.get("recent_errors", [])
    if errors:
        st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
    else:
        st.info("No recent Windows error events were reported, or this operating system does not provide Windows Event Logs.")
    st.caption("Metrics are latest snapshots held in the monitoring service's memory; historical charts are not enabled. Some temperature/GPU sensors are hardware-dependent.")


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

    ui_theme.render_login_header()
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
        if user_role in ("admin", "analyst"):
            if st.button("🖥️ Screen Monitor", use_container_width=True, key="sidebar_screen_monitor"):
                st.session_state["show_screen_monitor_panel"] = not st.session_state.get("show_screen_monitor_panel", False)
            if st.button("📱 Mobile Phone Monitor", use_container_width=True, key="sidebar_mobile_monitor"):
                st.session_state["show_mobile_monitor_panel"] = not st.session_state.get("show_mobile_monitor_panel", False)

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

                st.subheader("☁️ Permanent storage")
                pcfg = st.session_state.get("persist_cfg")
                pst = persist.status()
                if not pcfg:
                    st.error(
                        "Not configured: websites, library files and chats are LOST when the "
                        "app restarts. Add the PERSIST_GITHUB_* secrets (see README)."
                    )
                else:
                    if pst["last_error"]:
                        st.warning(pst["last_error"])
                    elif pst["last_saved"]:
                        st.success(f"Auto-saved at {pst['last_saved']}")
                        if pst["cloud_summary"]:
                            st.caption(f"Cloud copy: {pst['cloud_summary']}")
                    else:
                        st.info(pst["restored_msg"] or "Auto-save is on.")
                        if pst["cloud_summary"]:
                            st.caption(f"Restored: {pst['cloud_summary']}")
                    if st.button("☁️ Save now", key="persist_save_now"):
                        persist.maybe_save(pcfg, db_path, force=True)
                        st.rerun()

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
    if is_admin and not st.session_state.get("persist_cfg"):
        st.warning(
            "⚠️ Permanent storage is not set up, so anything added here disappears when the "
            "app restarts. Open the sidebar > Session & System Admin > ☁️ Permanent storage."
        )

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
        with st.expander("📐 Advanced Maths links (one-click)", expanded=False):
            st.caption(
                "Ready-made list of advanced-mathematics websites. Pick the ones you want and the AI "
                "will read them when you ask maths questions in the chat."
            )
            labels = {f"{g} · {t}": u for g, t, u in MATH_LINKS}
            chosen = st.multiselect(
                "Maths websites",
                options=list(labels.keys()),
                default=list(labels.keys()),
                key="math_links_pick",
            )
            if st.button("➕ Add selected Maths websites", disabled=busy or not chosen, key="math_links_add"):
                start_job("web", add_many_sources, db_path, [labels[c] for c in chosen], username)
                st.rerun()
            st.caption("Fetching many sites can take a minute or two. The result appears below.")
        poll_job("web", "Fetching website(s)")
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
                        save_now()
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
    if is_admin and not st.session_state.get("persist_cfg"):
        st.warning(
            "⚠️ Permanent storage is not set up, so anything added here disappears when the "
            "app restarts. Open the sidebar > Session & System Admin > ☁️ Permanent storage."
        )
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
                        save_now()
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
                        save_now()
                        st.rerun()


def main():
    st.set_page_config(
        page_title="Government AI Workspace", page_icon="💬", layout="wide"
    )
    ui_theme.inject_modern_css()
    db_path = "database.db"
    persist_cfg = persist.get_config(_read_secrets(persist.SECRET_KEYS))
    persist.restore_if_needed(persist_cfg, db_path)   # cold start: bring back saved data
    init_db(db_path)
    _seed_default_users(db_path)
    init_chat_tables(db_path)
    persist.maybe_save(persist_cfg, db_path)           # auto-save after any change
    st.session_state["persist_cfg"] = persist_cfg

    # Read-only public share link view (bypasses login for shared snapshots)
    share_token = st.query_params.get("share")
    if share_token:
        render_shared_thread_view(db_path, share_token)
        return

    if not check_password():
        return

    # Background ticker. It used to re-run the WHOLE page every second (st_autorefresh); a re-run that arrives
    # while the page is still processing a click makes Streamlit drop that click (buttons / forms "do nothing").
    # A fragment only re-runs itself and asks for a full re-run only when something really needs it.
    if not st.session_state.get("is_streaming", False):
        _busy = any(
            k.startswith(("job:", "scanjob:")) and v is not None for k, v in list(st.session_state.items())
        )

        @st.fragment(run_every=1 if _busy else 10)
        def _ticker():
            if client_ts := st.query_params.get("user_active_ts"):     # activity reported by the browser
                if client_ts != st.session_state.get("client_last_active_ts", 0):
                    st.session_state["client_last_active_ts"] = client_ts
                    st.session_state["last_active"] = time.time()
            expired = time.time() - st.session_state.get("last_active", time.time()) > TIMEOUT_SECONDS
            finished = tuple(sorted(
                k for k, v in list(st.session_state.items())
                if k.startswith(("job:", "scanjob:")) and isinstance(v, dict) and v["future"].done()
            ))
            new_finish = bool(finished) and finished != st.session_state.get("_ticker_seen_fin")
            st.session_state["_ticker_seen_fin"] = finished
            if expired or new_finish:
                st.rerun()   # full re-run: log the user out / show the finished job's result

        _ticker()
    client_activity_and_shortcuts()

    user_role = st.session_state.get("role", "user")
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

    ui_theme.render_hero(html.escape(str(username)), str(user_role))
    if st.session_state.get("show_screen_monitor_panel") and user_role in ("admin", "analyst"):
        render_screen_monitor(key_prefix="panel", username=username)
        st.divider()
    if st.session_state.get("show_mobile_monitor_panel") and user_role in ("admin", "analyst"):
        render_mobile_phone_monitor(db_path, username, key_prefix="panel")
        st.divider()

    is_staff = user_role in ("admin", "analyst")
    _vision_model = st.session_state.get("vision_model") or llm_cfg["vision_model"]

    # Ordinary users see ONLY: AI Workspace & Chat, Scan, HoloMotion 3D. The other tabs are not even created,
    # so there is nothing for them to open (the role comes from the login, not from anything the user can edit).
    tab_defs = [
        ("chat", "💬 AI Workspace & Chat", True),
        ("web", "🌐 Website Sources", is_staff),
        ("lib", "🖼️ Image & PDF Library", is_staff),
        ("scan", "📷 Scan", True),
        ("holo", "🖐️ HoloMotion 3D", True),
        ("db", "📊 Database Workbench", is_staff),
        ("users", "👥 User Management", is_staff),
        ("monitor", "🖥️ Screen Monitor", is_staff),
        ("phone", "📱 Mobile Phone Monitor", is_staff),
        ("pcmetrics", "📈 PC System Monitoring", is_staff),
    ]
    visible = [(k, label) for k, label, allowed in tab_defs if allowed]
    tabs = dict(zip([k for k, _ in visible], st.tabs([label for _, label in visible])))

    with tabs["chat"]:
        render_ai_chat_interface(db_path, llm_cfg, user_role, username)
    with tabs["scan"]:
        render_scan_tab(db_path, user_role, username, llm_cfg, _vision_model, on_change=save_now)
    with tabs["holo"]:
        render_holomotion_tab(db_path, llm_cfg, _vision_model, on_change=save_now)

    if is_staff:
        with tabs["web"]:
            render_web_sources_tab(db_path, user_role, username)
        with tabs["lib"]:
            render_library_tab(db_path, user_role, username, llm_cfg)
        with tabs["db"]:
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
        with tabs["users"]:
            render_user_management(db_path, username, user_role)
        with tabs["monitor"]:
            render_screen_monitor(username=username)
        with tabs["phone"]:
            render_mobile_phone_monitor(db_path, username)
        with tabs["pcmetrics"]:
            render_pc_system_monitor()


if __name__ == "__main__":
    main()
