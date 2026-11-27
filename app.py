import hmac
import json
import os
import re
import tempfile
import time
import hashlib
import secrets
import streamlit.components.v1 as components
from avatar_js import AVATAR_JS
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import sqlite3
import requests
import html
import streamlit as st
import persist
import auth_security
import ui_theme
from streamlit_autorefresh import st_autorefresh

from db_utils import init_db, is_safe_select, log_event, update_user_presence, remove_user_presence
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


LOGIN_RATE_WINDOW_SECONDS = 15 * 60
LOGIN_RATE_BLOCK_SECONDS = 30 * 60
LOGIN_RATE_MAX_FAILURES = 5


def _login_guard_key(username: str, role: str | None = None) -> tuple[str, str]:
    """Staff are rate-limited per account; ordinary users are rate-limited by client IP."""
    if role in ("admin", "analyst"):
        normalized = (username or "?").strip().casefold()[:100]
        return hashlib.sha256(("staff:" + normalized).encode("utf-8")).hexdigest(), "account"

    try:
        import ipaddress
        # Prefer Streamlit's connection metadata over forwarded headers.
        candidate = getattr(st.context, "ip_address", None)
        if not candidate:
            headers = st.context.headers
            forwarded = str(headers.get("X-Forwarded-For", "")).split(",")[0].strip()
            candidate = forwarded or str(headers.get("X-Real-IP", "")).strip()
        if candidate and str(candidate).lower() not in ("unknown", "none"):
            normalized_ip = str(ipaddress.ip_address(str(candidate).strip()))
            return hashlib.sha256(("ip:" + normalized_ip).encode("utf-8")).hexdigest(), "ip"
    except Exception:
        pass
    # Avoid treating every visitor as one shared identifier when a proxy hides client metadata.
    normalized = (username or "?").strip().casefold()[:100]
    return hashlib.sha256(("user:" + normalized).encode("utf-8")).hexdigest(), "username"


def _get_login_role(username: str) -> str | None:
    """Read the active account role before applying the role-specific login guard."""
    conn = sqlite3.connect("database.db")
    try:
        row = conn.execute(
            "SELECT role FROM app_users WHERE username = ? COLLATE NOCASE AND active = 1",
            (username,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _login_guard_state(db_path: str, guard_key: str) -> tuple[bool, int, int]:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_guard (
                guard_key TEXT PRIMARY KEY,
                key_type TEXT NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                window_start REAL NOT NULL,
                blocked_until REAL NOT NULL DEFAULT 0,
                last_attempt REAL NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT failures, window_start, blocked_until FROM login_guard WHERE guard_key=?",
            (guard_key,),
        ).fetchone()
        conn.commit()
        if not row:
            return False, 0, 0
        now = time.time()
        blocked_until = int(row[2] or 0)
        return blocked_until > now, int(row[0]), max(0, blocked_until - int(now))
    finally:
        conn.close()


def _record_login_failure(db_path: str, guard_key: str, key_type: str) -> tuple[int, bool]:
    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_guard (
                guard_key TEXT PRIMARY KEY,
                key_type TEXT NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                window_start REAL NOT NULL,
                blocked_until REAL NOT NULL DEFAULT 0,
                last_attempt REAL NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT failures, window_start, blocked_until FROM login_guard WHERE guard_key=?",
            (guard_key,),
        ).fetchone()
        if row and row[2] and row[2] > now:
            return int(row[0]), True
        if not row or now - float(row[1]) > LOGIN_RATE_WINDOW_SECONDS:
            failures, window_start = 1, now
        else:
            failures, window_start = int(row[0]) + 1, float(row[1])
        blocked_until = now + LOGIN_RATE_BLOCK_SECONDS if failures >= LOGIN_RATE_MAX_FAILURES else 0
        conn.execute("""
            INSERT INTO login_guard(guard_key,key_type,failures,window_start,blocked_until,last_attempt)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(guard_key) DO UPDATE SET
                key_type=excluded.key_type, failures=excluded.failures,
                window_start=excluded.window_start, blocked_until=excluded.blocked_until,
                last_attempt=excluded.last_attempt
        """, (guard_key, key_type, failures, window_start, blocked_until, now))
        conn.commit()
        return failures, bool(blocked_until)
    finally:
        conn.close()


def _clear_login_guard(db_path: str, guard_key: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM login_guard WHERE guard_key=?", (guard_key,))
        conn.commit()
    finally:
        conn.close()


def _finish_staff_login(username: str, role: str) -> None:
    ip_hash = auth_security.client_ip_hash()
    new_ip = auth_security.record_staff_ip("database.db", username, ip_hash)
    st.session_state["authenticated"] = True
    st.session_state["current_user"] = username
    st.session_state["role"] = role
    st.session_state["last_active"] = time.time()
    st.session_state["auth_error"] = False
    st.session_state.pop("auth_error_message", None)
    st.session_state.pop("staff_2fa_pending", None)
    st.session_state.pop("staff_2fa_secret_pending", None)
    st.session_state.pop("staff_2fa_failures", None)
    log_event("database.db", username, "LOGIN", f"role={role}; two_factor=verified")
    if new_ip:
        log_event("database.db", username, "SUSPICIOUS_LOGIN_NEW_IP", "Staff login from a previously unseen client IP.")
        auth_security.send_security_alert(
            "New staff login location", username,
            "A successful staff login was completed from a client IP not previously recorded for this account."
        )



def _email_code_digest(email, code):
    return hashlib.sha256(("govai-email-login:" + str(email).strip().lower() + ":" + str(code).strip()).encode()).hexdigest()


def _render_staff_recovery_email_setup(db_path, username):
    with st.expander("📧 Recovery Email Setup"):
        st.caption("Verify an email address to sign in later without remembering your username or password.")
        email = st.text_input("Recovery email address", key="staff_recovery_email_input").strip().lower()
        if st.button("Send email verification code", key="staff_send_email_verify"):
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                st.error("Enter a valid email address.")
            else:
                code = f"{secrets.randbelow(1000000):06d}"
                if auth_security.send_recovery_email_code(email, code):
                    st.session_state["staff_email_setup_pending"] = {"email": email, "digest": _email_code_digest(email, code), "expires": time.time() + 600, "username": username}
                    st.success("Verification code sent. Check your inbox and spam folder.")
                else:
                    st.error("Email could not be sent. Check SMTP settings in Streamlit Cloud Secrets.")
        pending = st.session_state.get("staff_email_setup_pending")
        if pending and pending.get("username", "").lower() == username.lower():
            if time.time() > pending.get("expires", 0):
                st.session_state.pop("staff_email_setup_pending", None)
                st.warning("Email verification code expired. Request a new one.")
            else:
                with st.form("staff_email_setup_verify_form"):
                    code = st.text_input("Email verification code", max_chars=6, placeholder="000000")
                    current_totp = st.text_input("Current Authenticator code", max_chars=6, placeholder="000000")
                    submitted = st.form_submit_button("Verify and save recovery email", type="primary")
                if submitted:
                    valid_email_code = len(code.strip()) == 6 and hmac.compare_digest(_email_code_digest(pending["email"], code.strip()), pending["digest"])
                    valid_totp = len(current_totp.strip()) == 6 and current_totp.strip().isdigit() and auth_security.verify_totp(db_path, username, current_totp.strip())
                    if valid_email_code and valid_totp and time.time() <= pending["expires"]:
                        auth_security.set_recovery_email(db_path, username, pending["email"])
                        st.session_state.pop("staff_email_setup_pending", None)
                        log_event(db_path, username, "STAFF_RECOVERY_EMAIL_VERIFIED", "Recovery email verified and saved.")
                        save_now()
                        st.success("Recovery email verified and saved.")
                        st.rerun()
                    else:
                        st.error("Email code or authenticator code is invalid or expired.")

def _render_staff_alternative_login():
    st.subheader("🔐 Try another option")
    st.caption("Any account with a verified recovery email can sign in using its Authenticator or a one-time email code.")
    email = st.text_input("Verified recovery email", key="alternative_login_email").strip().lower()
    method = st.radio("Verification method", ["Authenticator app (6-digit code)", "Recovery email (one-time code)"], key="alternative_login_method")
    if method.startswith("Authenticator"):
        st.caption("Authenticator codes refresh every 30 seconds and can be used only once. If you just used a code during setup or recovery, wait for the next code.")
        with st.form("alternative_totp_login_form"):
            code = st.text_input("6-digit Authenticator code", max_chars=6, placeholder="000000")
            submitted = st.form_submit_button("Verify and sign in", type="primary")
        if submitted:
            username, role = auth_security.get_staff_by_recovery_email("database.db", email)
            status = (
                auth_security.verify_totp_detailed("database.db", username, code.strip())
                if username and len(code.strip()) == 6 and code.strip().isdigit()
                else "invalid"
            )
            if status == "valid":
                _finish_staff_login(username, role)
                log_event("database.db", username, "LOGIN_RECOVERY_EMAIL_TOTP", "Signed in using verified recovery email and authenticator code.")
                st.rerun()
            elif status == "already_used":
                st.error("This authenticator code has already been used. Wait until your app shows the next 6-digit code, then submit it once.")
            else:
                st.error("The recovery email or authenticator code is invalid. Check that you selected the correct account and authenticator entry.")
    else:
        if st.button("Send one-time code to my recovery email", key="alternative_send_email_code"):
            username, role = auth_security.get_staff_by_recovery_email("database.db", email)
            if username:
                code = f"{secrets.randbelow(1000000):06d}"
                if auth_security.send_recovery_email_code(email, code):
                    st.session_state["alternative_email_otp"] = {"email": email, "username": username, "role": role, "digest": _email_code_digest(email, code), "expires": time.time() + 600, "attempts": 0}
                    st.success("If this address is registered, a one-time code has been sent. Check your inbox and spam folder.")
                else:
                    st.error("Email delivery is unavailable. Please contact the platform administrator.")
            else:
                st.info("If this address is registered, a one-time code has been sent.")
        pending = st.session_state.get("alternative_email_otp")
        if pending and pending.get("email") == email:
            if time.time() > pending.get("expires", 0):
                st.session_state.pop("alternative_email_otp", None)
                st.warning("Code expired. Request a new one.")
            else:
                with st.form("alternative_email_otp_form"):
                    code = st.text_input("Email one-time code", max_chars=6, placeholder="000000")
                    verify = st.form_submit_button("Verify and sign in", type="primary")
                if verify:
                    valid = len(code.strip()) == 6 and hmac.compare_digest(_email_code_digest(email, code.strip()), pending["digest"]) and time.time() <= pending["expires"] and pending["attempts"] < 5
                    if valid:
                        st.session_state.pop("alternative_email_otp", None)
                        _finish_staff_login(pending["username"], pending["role"])
                        log_event("database.db", pending["username"], "LOGIN_RECOVERY_EMAIL_OTP", "Signed in using verified recovery email one-time code.")
                        st.rerun()
                    else:
                        pending["attempts"] += 1
                        if pending["attempts"] >= 5:
                            st.session_state.pop("alternative_email_otp", None)
                        st.error("Invalid or expired email code.")

def check_password():
    if st.session_state.get("auth_timeout_expired", False):
        st.warning("⚠️ Your session expired due to inactivity. Please log in again.")
        st.session_state["auth_timeout_expired"] = False

    def password_entered():
        user = st.session_state.get("login_username", "").strip()
        pwd = st.session_state.get("login_password", "")
        account_role = _get_login_role(user)
        guard_key, key_type = _login_guard_key(user, account_role)
        blocked, failures, remaining = _login_guard_state("database.db", guard_key)
        if blocked:
            st.session_state["authenticated"] = False
            st.session_state["auth_error"] = True
            st.session_state["auth_error_message"] = (
                f"Too many failed attempts. This {'account' if key_type == 'account' else key_type} is temporarily blocked. "
                f"Try again in about {max(1, (remaining + 59) // 60)} minute(s)."
            )
            log_event("database.db", user or "?", "LOGIN_BLOCKED", f"type={key_type}; key={guard_key[:16]}")
            if key_type == "account":
                auth_security.send_security_alert("Staff account temporarily blocked", user, "The account reached the failed-password lockout threshold.")
            return

        role = verify_credentials(user, pwd)
        if role:
            _clear_login_guard("database.db", guard_key)
            st.session_state["auth_error"] = False
            st.session_state.pop("auth_error_message", None)
            if role in ("admin", "analyst", "user"):
                try:
                    auth_security.ensure_security_table("database.db")
                    if auth_security.totp_is_enabled("database.db", user):
                        st.session_state["staff_2fa_pending"] = {"username": user, "role": role, "mode": "verify"}
                    else:
                        secret = auth_security.new_totp_secret()
                        # The encryption key must exist before an enrollment is offered.
                        auth_security._fernet()
                        st.session_state["staff_2fa_secret_pending"] = secret
                        st.session_state["staff_2fa_pending"] = {"username": user, "role": role, "mode": "enroll"}
                except Exception as exc:
                    st.session_state["auth_error"] = True
                    st.session_state["auth_error_message"] = f"Account 2FA is not configured correctly. Contact the platform administrator. ({exc})"
                    return
                return
            st.session_state["authenticated"] = True
            st.session_state["current_user"] = user
            st.session_state["role"] = role
            st.session_state["last_active"] = time.time()
            log_event("database.db", user, "LOGIN", f"role={role}")
        else:
            count, newly_blocked = _record_login_failure("database.db", guard_key, key_type)
            st.session_state["authenticated"] = False
            st.session_state["auth_error"] = True
            if newly_blocked:
                st.session_state["auth_error_message"] = (
                    f"Too many failed attempts ({count}). This {key_type} is blocked for 30 minutes."
                )
                log_event(
                    "database.db", user or "?",
                    "LOGIN_BLOCKED" if key_type == "account" else "LOGIN_IP_BLOCKED",
                    f"type={key_type}; key={guard_key[:16]}; failures={count}",
                )
            else:
                st.session_state["auth_error_message"] = "Invalid username or password."
            log_event("database.db", user or "?", "LOGIN_FAILED", f"type={key_type}; key={guard_key[:16]}; failures={count}")
            if key_type == "account" and (count >= 3 or newly_blocked):
                auth_security.send_security_alert(
                    "Repeated failed staff login", user,
                    f"Failed password attempts: {count}. Account lockout: {'yes' if newly_blocked else 'no'}."
                )

    if st.session_state.get("authenticated", False):
        return check_session_timeout()

    pending = st.session_state.get("staff_2fa_pending")
    if pending:
        username, role = pending["username"], pending["role"]
        otp_guard_key, otp_guard_type = _login_guard_key(username, role)
        otp_blocked, _, otp_remaining = _login_guard_state("database.db", otp_guard_key)
        if otp_blocked:
            st.error(f"Too many invalid authenticator codes. This account is temporarily blocked. Try again in about {max(1, (otp_remaining + 59) // 60)} minute(s).")
            return False
        st.subheader("🔐 Account two-factor verification")
        if pending["mode"] == "enroll":
            secret = st.session_state.get("staff_2fa_secret_pending", "")
            st.info("Set up a time-based authenticator (TOTP). Compatible apps include Google Authenticator, Microsoft Authenticator, Apple Passwords, 1Password, Bitwarden, Authy and Aegis.")
            st.markdown("**Account:** " + username)
            st.markdown("**Setup key (keep private):**")
            st.code(secret)
            st.markdown("**Scan this QR code with your authenticator app:**")
            try:
                qr_image = auth_security.provisioning_qr_code(username, secret)
                st.image(qr_image, caption="Government AI Platform — account 2FA setup", width=230)
            except Exception as exc:
                st.warning(f"QR code could not be displayed. Use the setup key below instead. ({exc})")
            with st.expander("Manual setup (if QR scanning is unavailable)"):
                st.markdown("**Setup key (keep private):**")
                st.code(secret)
                st.markdown("**Authenticator setup URI:**")
                st.code(auth_security.provisioning_uri(username, secret))
            st.caption("After scanning, verify your recovery email below, then enter the current 6-digit code from your authenticator app. This setup applies to Admin, Analyst and User accounts.")
            with st.form("first_2fa_recovery_email_send_form"):
                setup_email = st.text_input("Recovery email address", placeholder="name@example.com")
                send_setup_email = st.form_submit_button("Send email verification code")
            if send_setup_email:
                setup_email = setup_email.strip().lower()
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", setup_email):
                    st.error("Enter a valid email address.")
                else:
                    email_code = f"{secrets.randbelow(1000000):06d}"
                    if auth_security.send_recovery_email_code(setup_email, email_code):
                        st.session_state["staff_enroll_email_pending"] = {"email": setup_email, "digest": _email_code_digest(setup_email, email_code), "expires": time.time() + 600, "username": username}
                        st.success("Verification code sent. Check your inbox and spam folder.")
                    else:
                        st.error("Email could not be sent. Configure SMTP settings in Streamlit Cloud Secrets.")
            email_pending = st.session_state.get("staff_enroll_email_pending")
            if email_pending and email_pending.get("username", "").lower() == username.lower() and time.time() <= email_pending.get("expires", 0):
                with st.form("first_2fa_recovery_email_verify_form"):
                    email_code_input = st.text_input("Email verification code", max_chars=6, placeholder="000000")
                    verify_email = st.form_submit_button("Verify recovery email")
                if verify_email:
                    if len(email_code_input.strip()) == 6 and hmac.compare_digest(_email_code_digest(email_pending["email"], email_code_input.strip()), email_pending["digest"]):
                        st.session_state["staff_enroll_email_verified"] = email_pending["email"]
                        st.session_state.pop("staff_enroll_email_pending", None)
                        st.success("Recovery email verified.")
                        st.rerun()
                    else:
                        st.error("Invalid email verification code.")
            elif email_pending and time.time() > email_pending.get("expires", 0):
                st.session_state.pop("staff_enroll_email_pending", None)
                st.warning("Email code expired. Send a new one.")
            if st.session_state.get("staff_enroll_email_verified"):
                st.success("Verified recovery email: " + st.session_state["staff_enroll_email_verified"])
        verification_method = "Authenticator app"
        if pending["mode"] == "verify":
            verification_options = ["Authenticator app", "Backup Code"]
            verification_method = st.radio(
                "Choose a verification method",
                verification_options,
                horizontal=False,
                key="login_2fa_method",
            )
        else:
            st.write(f"Set up Authenticator 2FA for **{username}** using the 6-digit code.")
        code_max = 8 if verification_method == "Backup Code" else 10
        code_label = "8-digit backup code" if verification_method == "Backup Code" else "Verification code"
        with st.form("staff_2fa_form"):
            code = st.text_input(code_label, max_chars=code_max, placeholder="00000000" if verification_method == "Backup Code" else "000000")
            submitted = st.form_submit_button("Verify and continue", type="primary")
        if submitted:
            valid = False
            totp_status = "invalid"
            if pending["mode"] == "enroll" and len(code.strip()) == 6 and code.strip().isdigit():
                valid = auth_security.enable_totp(
                    "database.db", username,
                    st.session_state.get("staff_2fa_secret_pending", ""), code.strip()
                )
            elif pending["mode"] == "verify":
                if verification_method == "Backup Code":
                    valid = auth_security.consume_backup_code("database.db", username, code.strip())
                elif len(code.strip()) == 6 and code.strip().isdigit():
                    totp_status = auth_security.verify_totp_detailed("database.db", username, code.strip())
                    valid = totp_status == "valid"
            if valid and pending["mode"] == "enroll" and not st.session_state.get("staff_enroll_email_verified"):
                st.error("Verify your recovery email before enabling staff 2FA.")
                valid = False
            if valid:
                _clear_login_guard("database.db", otp_guard_key)
                if pending["mode"] == "enroll":
                    auth_security.set_recovery_email("database.db", username, st.session_state["staff_enroll_email_verified"])
                    st.session_state.pop("staff_enroll_email_verified", None)
                    st.session_state["staff_recovery_codes_once"] = auth_security.generate_recovery_codes("database.db", username)
                    log_event("database.db", username, "STAFF_RECOVERY_CODES_CREATED", "Initial one-time recovery codes generated.")
                    save_now()
                _finish_staff_login(username, role)
                st.rerun()
            else:
                attempts, otp_newly_blocked = _record_login_failure("database.db", otp_guard_key, otp_guard_type)
                if totp_status == "already_used":
                    st.error("This authenticator code has already been used. Wait for the next 30-second code and submit it once.")
                else:
                    st.error("Invalid or expired verification code. Check the selected method and code, then try again.")
                if attempts >= 3:
                    log_event("database.db", username, "STAFF_2FA_FAILED", f"Invalid authenticator codes: {attempts}.")
                if attempts == 3 or otp_newly_blocked:
                    auth_security.send_security_alert(
                        "Repeated failed 2FA verification", username,
                        f"Invalid or expired authenticator codes: {attempts}. Account lockout: {'yes' if otp_newly_blocked else 'no'}."
                    )
        if st.button("Cancel sign-in", key="cancel_staff_2fa"):
            st.session_state.pop("staff_2fa_pending", None)
            st.session_state.pop("staff_2fa_secret_pending", None)
            st.session_state.pop("staff_2fa_failures", None)
            st.session_state.pop("login_password", None)
            st.rerun()
        return False

    ui_theme.render_login_header()
    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        with st.form("login_form"):
            st.text_input("Username", key="login_username")
            st.text_input("Password", type="password", key="login_password")
            st.form_submit_button("Login", on_click=password_entered, type="primary")
            if st.session_state.get("auth_error", False):
                st.error(st.session_state.get("auth_error_message", "😕 Invalid username or password."))
        with st.expander("Try another option"):
            _render_staff_alternative_login()
    return False



def _show_staff_recovery_codes():
    codes = st.session_state.get("staff_recovery_codes_once")
    if codes:
        with st.sidebar.expander("🔑 Save your new recovery codes", expanded=True):
            st.warning("These are 10 backup codes, each 8 digits and usable once. Save them securely; they are shown only now.")
            st.code("\\n".join(codes))
            if st.button("I have saved these codes", key="dismiss_staff_recovery_codes"):
                st.session_state.pop("staff_recovery_codes_once", None)
                st.rerun()


def _render_staff_recovery_management(db_path, username):
    with st.expander("🔑 2FA Backup Codes"):
        st.caption("Generate 10 new one-time 8-digit backup codes. This replaces and invalidates all previously generated codes.")
        st.warning("Confirm your identity with the current authenticator code before replacing recovery codes.")
        with st.form("regenerate_staff_recovery_codes_form"):
            current_totp = st.text_input("Current 6-digit authenticator code", max_chars=6, placeholder="000000")
            regenerate = st.form_submit_button("Generate / Regenerate Recovery Codes", type="primary")
        if regenerate:
            if len(current_totp.strip()) == 6 and current_totp.strip().isdigit() and auth_security.verify_totp(db_path, username, current_totp.strip()):
                codes = auth_security.generate_recovery_codes(db_path, username)
                st.session_state["staff_recovery_codes_once"] = codes
                log_event(db_path, username, "STAFF_RECOVERY_CODES_REGENERATED", "10 one-time recovery codes generated after TOTP verification.")
                save_now()
                st.rerun()
            else:
                st.error("Invalid or expired authenticator code. Backup codes were not changed.")

def render_sidebar_controls(db_path, user_role):
    with st.sidebar:
        username = st.session_state.get("current_user", "User")
        render_sidebar_threads(db_path, username)
        if st.button("🧭 Take a Tour", use_container_width=True, key="sidebar_take_product_tour"):
            st.session_state["product_tour_step"] = 0
            st.session_state["show_product_tour"] = True
        # Every account is required to enroll in 2FA. All roles must be able to
        # view their one-time recovery codes and manage their verified recovery email.
        _show_staff_recovery_codes()
        _render_staff_recovery_management(db_path, username)
        _render_staff_recovery_email_setup(db_path, username)

        # Monitoring controls remain restricted to Admin and Analyst.
        if user_role in ("admin", "analyst"):
            if st.button("🖥️ Screen Monitor", use_container_width=True, key="sidebar_screen_monitor"):
                st.session_state["show_screen_monitor_panel"] = not st.session_state.get("show_screen_monitor_panel", False)
            if st.button("📱 Mobile Phone Monitor", use_container_width=True, key="sidebar_mobile_monitor"):
                st.session_state["show_mobile_monitor_panel"] = not st.session_state.get("show_mobile_monitor_panel", False)

        st.markdown("---")
        with st.expander("⚙️ Session & System Admin", expanded=False):
            st.success(f"User: **{username}** | Role: **{user_role.upper()}**")

            if user_role == "admin":
                with st.expander("📨 SMTP Email Delivery Test"):
                    st.caption("Send a test message to verify the configured SMTP connection. Never enter SMTP passwords here.")
                    test_email = st.text_input("Test recipient email", key="smtp_test_recipient").strip()
                    if st.button("Send SMTP test email", key="smtp_test_send"):
                        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", test_email):
                            st.error("Enter a valid recipient email address.")
                        elif auth_security.send_smtp_test_email(test_email):
                            log_event(db_path, username, "SMTP_TEST_EMAIL_SENT", "Admin-triggered SMTP test email sent.")
                            st.success("SMTP test email accepted by the mail server. Check Inbox and Spam/Junk.")
                        else:
                            log_event(db_path, username, "SMTP_TEST_EMAIL_FAILED", "Admin-triggered SMTP test email failed; see server logs.")
                            st.error("SMTP test failed. Check Streamlit Cloud logs for the safe diagnostic error.")

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
                remove_user_presence(db_path, st.session_state.get("analytics_session_id", ""))
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



def render_admin_dashboard(db_path: str):
    if st.session_state.get("role") != "admin":
        st.error("Admin permission required.")
        log_event(db_path, st.session_state.get("current_user", "?"), "BLOCKED_ADMIN_DASHBOARD", "")
        return
    @st.fragment(run_every=15)
    def live():
        import plotly.express as px
        st.caption("Live refresh: 15 seconds. Search charts store extracted keywords only, not full prompts or attachments.")
        period = st.selectbox("Report period", ["Daily", "Monthly", "Yearly"], key="analytics_period")
        interval = {"Daily":"-30 days","Monthly":"-365 days","Yearly":"-5 years"}[period]
        con=sqlite3.connect(db_path)
        events=pd.read_sql_query("SELECT timestamp,username,role,event_type,details FROM usage_events WHERE timestamp >= datetime('now', ?) ORDER BY timestamp",con,params=(interval,))
        online=pd.read_sql_query("SELECT username,role,last_seen FROM user_sessions WHERE last_seen >= datetime('now','-90 seconds') ORDER BY last_seen DESC",con)
        failed=con.execute("SELECT COUNT(*) FROM audit_logs WHERE action='LOGIN_FAILED' AND timestamp >= datetime('now','-24 hours')").fetchone()[0]
        disabled=con.execute("SELECT COUNT(*) FROM app_users WHERE active=0").fetchone()[0]
        total=con.execute("SELECT COUNT(*) FROM app_users").fetchone()[0]
        con.close()
        cols=st.columns(5)
        cols[0].metric("Online users",online.username.nunique() if not online.empty else 0)
        cols[1].metric("Sessions",len(online))
        cols[2].metric("Registered users",total)
        cols[3].metric("Failed logins (24h)",failed)
        cols[4].metric("Disabled accounts",disabled)
        if not events.empty:
            events["timestamp"]=pd.to_datetime(events.timestamp,errors="coerce")
            events=events.dropna(subset=["timestamp"])
            if period=="Daily": events["bucket"]=events.timestamp.dt.strftime("%Y-%m-%d")
            elif period=="Monthly": events["bucket"]=events.timestamp.dt.to_period("M").astype(str)
            else: events["bucket"]=events.timestamp.dt.year.astype(str)
            kind=st.selectbox("Chart type",["Line chart","Bar chart","Pie chart","Donut chart","Scatter plot","Histogram"],key="analytics_chart")
            if kind=="Line chart":
                data=events.groupby("bucket",as_index=False).size(); fig=px.line(data,x="bucket",y="size",markers=True,title="Usage over time")
            elif kind=="Bar chart":
                data=events.groupby("bucket",as_index=False).size(); fig=px.bar(data,x="bucket",y="size",title="Activity volume")
            elif kind in ["Pie chart","Donut chart"]:
                data=events.event_type.value_counts().rename_axis("event").reset_index(name="count"); fig=px.pie(data,names="event",values="count",hole=.48 if kind=="Donut chart" else 0,title="Event distribution")
            elif kind=="Scatter plot":
                data=events.groupby(events.timestamp.dt.date).agg(events=("event_type","size"),users=("username","nunique")).reset_index(); fig=px.scatter(data,x="users",y="events",title="Daily active users vs activity")
            else: fig=px.histogram(events,x="event_type",title="Event frequency")
            fig.update_layout(height=350); st.plotly_chart(fig,use_container_width=True)
            st.subheader("Most / least searched topics")
            search=events[events.event_type=="CHAT_SEARCH"]
            if not search.empty:
                terms=search.details.fillna("").value_counts().rename_axis("topic").reset_index(name="searches")
                left,right=st.columns(2)
                left.plotly_chart(px.bar(terms.head(10),x="searches",y="topic",orientation="h",title="Top searched"),use_container_width=True)
                right.plotly_chart(px.bar(terms.sort_values(["searches","topic"]).head(10),x="searches",y="topic",orientation="h",title="Least searched"),use_container_width=True)
            else: st.info("Search trends appear after chat searches are recorded.")
        else: st.info("No usage events recorded yet.")
        st.subheader("Currently online"); st.dataframe(online,use_container_width=True,hide_index=True)
        con=sqlite3.connect(db_path)
        security=pd.read_sql_query("SELECT timestamp,username,action,details FROM audit_logs WHERE action IN ('LOGIN_FAILED','LOGIN_BLOCKED','LOGIN_IP_BLOCKED','STAFF_2FA_FAILED','SUSPICIOUS_LOGIN_NEW_IP','BLOCKED_SQL','BLOCKED_ADMIN_DASHBOARD','USER_DISABLED') ORDER BY id DESC LIMIT 100",con)
        try:
            blocked_ips=pd.read_sql_query(
                "SELECT guard_key, key_type, failures, blocked_until, last_attempt FROM login_guard WHERE blocked_until > ? ORDER BY blocked_until DESC",
                con, params=(time.time(),)
            )
        except Exception:
            blocked_ips=pd.DataFrame(columns=["guard_key","key_type","failures","blocked_until","last_attempt"])
        st.subheader("Security events"); st.dataframe(security,use_container_width=True,hide_index=True)
        st.subheader("🚫 Active temporary blocks")
        if blocked_ips.empty:
            st.success("No active login blocks.")
        else:
            display_blocks=blocked_ips.copy()
            display_blocks["identifier"]=display_blocks.apply(lambda r: f"{r['key_type']} • {str(r['guard_key'])[:16]}…",axis=1)
            display_blocks["blocked_until"]=pd.to_datetime(display_blocks["blocked_until"],unit="s").dt.strftime("%Y-%m-%d %H:%M:%S")
            st.dataframe(display_blocks[["identifier","failures","blocked_until"]],use_container_width=True,hide_index=True)
            selected=st.selectbox("Select a block to release",blocked_ips["guard_key"].tolist(),format_func=lambda k: f"{blocked_ips.loc[blocked_ips.guard_key==k,'key_type'].iloc[0]} • {k[:16]}…",key="release_login_block")
            if st.button("🔓 Release selected block",key="release_login_block_btn"):
                con.execute("DELETE FROM login_guard WHERE guard_key=?",(selected,))
                con.commit()
                log_event(db_path,st.session_state.get("current_user","admin"),"LOGIN_BLOCK_RELEASED",f"key={selected[:16]}")
                st.success("Temporary block released.")
                st.rerun()
        con.close()
        st.caption("Automatic protection: ordinary User accounts are blocked by client IP after 5 failed attempts within 15 minutes (30-minute block). Admin and Analyst accounts are protected separately by account-based rate limiting, so a shared IP block will not lock staff out. IP addresses are hashed before storage.")
    live()



def _tour_status(db_path, username):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS app_tour_status (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            completed INTEGER NOT NULL DEFAULT 0,
            completed_at REAL
        )""")
        row = conn.execute("SELECT completed FROM app_tour_status WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        conn.commit()
        return bool(row and row[0])
    finally:
        conn.close()


def _finish_tour(db_path, username):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS app_tour_status (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            completed INTEGER NOT NULL DEFAULT 0,
            completed_at REAL
        )""")
        conn.execute("""INSERT INTO app_tour_status(username,completed,completed_at)
            VALUES(?,1,?) ON CONFLICT(username) DO UPDATE SET
            completed=1,completed_at=excluded.completed_at""", (username,time.time()))
        conn.commit()
    finally:
        conn.close()


@st.dialog("👋 Government AI Platform — Interactive Tour", width="large")
def _show_product_tour(db_path, username, role):
    """Step-by-step tour: open the matching tab and narrate each feature in English."""
    staff = role in ("admin", "analyst")
    steps = [
        {
            "title": "Welcome to your AI Workspace",
            "description": "Welcome to Government AI Platform. This guided tour will open each available section and explain what you can do there. Use Next to continue.",
            "tab": None,
            "speech": "Welcome to Government AI Platform. I am Ravana, your guide. I will show you the main features and explain how to use each one. Select Next to begin."
        },
        {
            "title": "💬 AI Workspace & Chat",
            "description": "This is your main AI assistant. Ask questions, request explanations, draft documents, analyse information and continue previous conversations from your chat history. Use the message box to ask a question.",
            "tab": "AI Workspace & Chat",
            "speech": "This is the AI Workspace and Chat. Type a question in the message box and send it to the AI assistant. You can ask for explanations, help with documents, research and analysis. Your previous conversations can be opened from the chat history."
        },
        {
            "title": "📷 Scan",
            "description": "Open Scan to take a photo with your camera or upload an image. The AI can identify the main object and provide information. Available scanning tools depend on your account permissions.",
            "tab": "Scan",
            "speech": "This is the Scan section. You can take a photo using your camera or upload an image. Press Scan to let the AI examine the image and identify the object or provide useful information. Please allow camera access when your browser asks."
        },
        {
            "title": "🖐️ HoloMotion 3D",
            "description": "Use HoloMotion 3D to explore interactive 3D models and gesture-based controls. Launch the viewer, allow camera access if you want hand tracking, and load a supported 3D model.",
            "tab": "HoloMotion 3D",
            "speech": "This is HoloMotion 3D. Press Launch to open the interactive three dimensional viewer. You can enable your camera and use hand gestures to interact with the model. You can also add a supported three dimensional model file."
        },
    ]
    if staff:
        steps += [
            {
                "title": "🌐 Website Sources",
                "description": "Manage trusted websites and sources that the AI can use when answering questions. Add a website URL, refresh its content or remove a source when it is no longer needed.",
                "tab": "Website Sources",
                "speech": "This is Website Sources. Staff can add trusted website addresses for the platform to collect and search information from. You can refresh a source to update its content or remove it when it is no longer required."
            },
            {
                "title": "🖼️ Image & PDF Library",
                "description": "Upload and manage reference images and PDF documents. The AI can search indexed document content to help answer related questions.",
                "tab": "Image & PDF Library",
                "speech": "This is the Image and PDF Library. You can upload reference pictures and PDF documents here. After the documents are indexed, the AI can search their content and use relevant information when answering questions."
            },
            {
                "title": "📊 Database Workbench",
                "description": "Administrators can run read-only SELECT queries to inspect platform data. Only safe read-only queries are allowed.",
                "tab": "Database Workbench",
                "speech": "This is the Database Workbench. Administrators can inspect platform data by running read only select queries. Data-changing commands are not allowed here. Review your query carefully before running it."
            },
            {
                "title": "👥 User Management",
                "description": "Create accounts, choose their roles and manage account access. The actions available depend on whether you are an Admin or Analyst.",
                "tab": "User Management",
                "speech": "This is User Management. Admins and Analysts can create accounts and manage permitted user access. Choose a username, set a temporary password and select an allowed role. Give the temporary password to the account owner securely."
            },
            {
                "title": "🖥️ Screen Monitor",
                "description": "View computers that have explicitly chosen to share their screen. Screen sharing starts only after the device owner approves the browser permission prompt.",
                "tab": "Screen Monitor",
                "speech": "This is Screen Monitor. It lets authorized staff view computers that have chosen to share their screens. Screen capture is not started secretly. The person using the computer must open the sharing page, choose a screen and approve the browser permission."
            },
            {
                "title": "📱 Mobile Phone Monitor",
                "description": "View paired Android devices that have explicitly started screen sharing. The phone owner must approve Android's screen-capture permission and can stop sharing at any time.",
                "tab": "Mobile Phone Monitor",
                "speech": "This is Mobile Phone Monitor. Authorized staff can view paired Android devices only while the phone owner has started screen sharing and approved Android's system permission. The owner can stop sharing whenever they want."
            },
            {
                "title": "📈 PC System Monitoring",
                "description": "Review telemetry shared by an installed PC agent, such as CPU, memory, disk, network and supported system information. The agent must be configured and running on the computer.",
                "tab": "PC System Monitoring",
                "speech": "This is PC System Monitoring. It displays system information shared by a configured computer agent, such as processor usage, memory, disk and network activity. The agent must be installed and running on a computer whose owner has agreed to share this information."
            },
        ]
        if role == "admin":
            steps.append({
                "title": "📊 Live Analytics",
                "description": "Administrators can review platform usage, active sessions, account statistics and security events.",
                "tab": "Live Analytics",
                "speech": "This is Live Analytics. Administrators can review platform activity, active sessions, account statistics and security events. Use the report period and charts to explore the available information."
            })
    steps.append({
        "title": "🔐 Account Security & Help",
        "description": "Protect your account with a strong password and the available two-factor authentication and recovery options. You can reopen this tour from the sidebar using Take a Tour.",
        "tab": None,
        "speech": "Finally, remember to protect your account. Use a strong password and keep your two factor authentication and recovery information safe. You can open this guided tour again at any time using Take a Tour in the sidebar. Thank you for using Government AI Platform."
    })

    idx = max(0, min(int(st.session_state.get("product_tour_step", 0)), len(steps)-1))
    step = steps[idx]
    st.caption(f"Step {idx+1} of {len(steps)}")
    st.progress((idx+1)/len(steps))
    st.subheader(step["title"])
    st.write(step["description"])
    st.caption("Ravana will explain this section in English. You can replay the explanation below.")
    speech_text = json.dumps(step["speech"])
    tab_text = json.dumps(step["tab"])
    avatar_script = AVATAR_JS
    tour_html = (
        "<!doctype html><html><head><meta charset='utf-8'></head><body>"
        "<button id='tourReplay' style='border:0;border-radius:22px;padding:9px 16px;"
        "background:#5146d8;color:white;font-weight:600;cursor:pointer'>🔊 Replay Ravana's explanation</button>"
        "<span id='tourVoiceStatus' style='margin-left:9px;font:12px Arial,sans-serif'></span>"
        "<div id='tourRavana' style='display:flex;align-items:center;gap:12px;margin-top:14px;padding:12px 18px;width:fit-content;max-width:100%;border-radius:55px;background:radial-gradient(circle at 25% 50%,#fff 0 28%,transparent 29%),radial-gradient(circle at 42% 35%,#fff 0 34%,transparent 35%),radial-gradient(circle at 68% 38%,#fff 0 34%,transparent 35%),radial-gradient(circle at 82% 55%,#fff 0 25%,transparent 26%),linear-gradient(180deg,#fff,#eaf3ff);box-shadow:0 8px 22px rgba(50,70,130,.18);animation:tourCloudFloat 3.4s ease-in-out infinite;position:relative;z-index:5'>"
        "<canvas id='tourRavanaFace' width='720' height='720' role='img' aria-label='Animated Ravana avatar' style='width:94px;height:94px;object-fit:cover;border-radius:50%;border:4px solid white;box-shadow:0 4px 14px #7894b966;background:#fff'></canvas>"
        "<div style='font:700 13px Arial;color:#24324b;min-width:105px'><div>☁️ Ravana</div><div id='tourRavanaState' style='font-size:11px;color:#5265dc;margin-top:5px'>Ready to guide you</div></div></div>"
        "<style>@keyframes tourCloudFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}@keyframes tourTalk{0%,100%{transform:scale(1)}50%{transform:scale(1.055)}}#tourRavana.talking #tourRavanaFace{animation:tourTalk .28s ease-in-out infinite alternate}#tourRavana.talking{box-shadow:0 8px 26px rgba(79,70,229,.28)}</style>"
        "<script>" + r"""
        (function(){
          const P=(function(){try{return window.parent&&window.parent.document?window.parent:window;}catch(e){return window;}})();
          const text=__TOUR_TEXT__, target=__TOUR_TAB__;
          function openTarget(){
            if(!target)return;
            const wanted=target.toLowerCase();
            const tabs=P.document.querySelectorAll('button[role="tab"]');
            for(const tab of tabs){
              const label=(tab.innerText||tab.textContent||'').toLowerCase();
              if(label.includes(wanted)){tab.click();break;}
            }
          }
          function speak(){
            const status=document.getElementById('tourVoiceStatus');
            try{
              if(!P.speechSynthesis||!P.SpeechSynthesisUtterance){status.textContent='English speech is not supported in this browser.';return;}
              P.speechSynthesis.cancel();
              P.__govSession=(P.__govSession||0)+1;
              const avatarPanel=document.getElementById('tourRavana');
              const avatarState=document.getElementById('tourRavanaState');
              const avatarCanvas=document.getElementById('tourRavanaFace');
              const avatarCtx=avatarCanvas ? avatarCanvas.getContext('2d') : null;
              const avatarImage=new Image();
              let avatarReady=false, avatarSpeaking=false, avatarMouth=0, avatarRaf=0, avatarLastPulse=0;
              const avatarSources=[
                'https://raw.githubusercontent.com/Dev-Sachintha/government_ai_platform/main/static/avatar.jpg',
                P.location.origin + '/app/static/avatar.jpg'
              ];
              let avatarSourceIndex=0;
              avatarImage.onload=function(){avatarReady=true;if(!avatarRaf)avatarRaf=P.requestAnimationFrame(drawRavanaFrame);};
              avatarImage.onerror=function(){
                avatarSourceIndex++;
                if(avatarSourceIndex<avatarSources.length) avatarImage.src=avatarSources[avatarSourceIndex];
                else if(avatarState) avatarState.textContent='Ravana image could not load';
              };
              avatarImage.src=avatarSources[0];
              function drawRavanaFrame(now){
                if(!avatarCtx)return;
                if(!avatarReady){avatarRaf=P.requestAnimationFrame(drawRavanaFrame);return;}
                avatarCtx.clearRect(0,0,720,720);
                avatarCtx.drawImage(avatarImage,0,0,720,720);
                const blinkCycle=(now%5000)/5000;
                const blink=Math.max(0,1-Math.abs(blinkCycle-0.78)/0.035);
                if(blink>0.02){
                  [{x:273,y:409,w:58,h:34},{x:397,y:409,w:58,h:34}].forEach(function(eye){
                    avatarCtx.fillStyle='rgba(183,139,111,'+Math.min(1,blink)+')';
                    avatarCtx.beginPath();
                    avatarCtx.ellipse(eye.x+eye.w/2,eye.y+eye.h/2,eye.w/2+2,Math.max(1,eye.h*blink*1.25),0,0,Math.PI*2);
                    avatarCtx.fill();
                    avatarCtx.strokeStyle='rgba(75,42,35,'+Math.min(.75,blink)+')';
                    avatarCtx.lineWidth=3;
                    avatarCtx.beginPath();avatarCtx.moveTo(eye.x,eye.y+eye.h/2);
                    avatarCtx.quadraticCurveTo(eye.x+eye.w/2,eye.y+eye.h/2+eye.h*blink*.5,eye.x+eye.w,eye.y+eye.h/2);avatarCtx.stroke();
                  });
                }
                const target=avatarSpeaking ? (.25+.55*Math.abs(Math.sin(now/95))) : 0;
                avatarMouth+=(target-avatarMouth)*.34;
                if(avatarMouth>.035){
                  const dy=30*avatarMouth;
                  avatarCtx.fillStyle='rgb(34,8,10)';
                  avatarCtx.beginPath();avatarCtx.ellipse(354,541+(dy-2)/2,27,(dy+12)/2,0,0,Math.PI*2);avatarCtx.fill();
                  if(avatarMouth>.35){avatarCtx.fillStyle='rgb(222,210,190)';avatarCtx.fillRect(337,539,34,6);}
                  avatarCtx.fillStyle='rgb(120,40,48)';
                  avatarCtx.beginPath();avatarCtx.ellipse(354,541+dy-5,13,6,0,0,Math.PI*2);avatarCtx.fill();
                }
                // Keep the frame loop alive so natural blinking continues even between speech steps.
                avatarRaf=P.requestAnimationFrame(drawRavanaFrame);
              }
              function startRavanaAnimation(){
                avatarSpeaking=true;
                if(avatarPanel)avatarPanel.classList.add('talking');
                if(avatarState)avatarState.textContent='Speaking English…';
                if(!avatarRaf)avatarRaf=P.requestAnimationFrame(drawRavanaFrame);
              }
              function stopRavanaAnimation(){
                avatarSpeaking=false;
                if(avatarPanel)avatarPanel.classList.remove('talking');
                if(avatarState)avatarState.textContent='Ready to guide you';
                if(!avatarRaf)avatarRaf=P.requestAnimationFrame(drawRavanaFrame);
              }
              const avatar={
                start:startRavanaAnimation,
                pulse:function(){avatarLastPulse=P.performance.now();startRavanaAnimation();},
                stop:stopRavanaAnimation
              };
              avatar.start();
              const utterance=new P.SpeechSynthesisUtterance(text);
              utterance.lang='en-US'; utterance.rate=0.94; utterance.pitch=0.95;
              utterance.onboundary=function(){avatar.pulse();};
              utterance.onend=function(){avatar.stop();status.textContent='';};
              utterance.onerror=function(){avatar.stop();status.textContent='Speech could not start. Press Replay to try again.';};
              P.speechSynthesis.speak(utterance);
              status.textContent='☁️ Ravana is speaking inside the tour…';
            }catch(e){status.textContent='Press Replay to hear the explanation.';}
          }
          document.getElementById('tourReplay').addEventListener('click',speak);
          openTarget();
          // Attempt automatic narration; the replay control remains available if
          // the browser blocks speech started during a Streamlit rerun.
          setTimeout(speak,250);
        })();
        </script></body></html>
        """.replace("__TOUR_TEXT__", speech_text).replace("__TOUR_TAB__", tab_text)
    )
    components.html(tour_html, height=180)

    st.divider()
    a,b,c=st.columns(3)
    with a:
        if st.button("← Back", disabled=idx==0, use_container_width=True, key="tour_back"):
            st.session_state["product_tour_step"]=idx-1
            st.rerun()
    with b:
        if st.button("Skip Tour", use_container_width=True, key="tour_skip"):
            _finish_tour(db_path,username)
            st.session_state["show_product_tour"]=False
            st.session_state.pop("product_tour_step",None)
            save_now()
            st.rerun()
    with c:
        label="Finish ✓" if idx==len(steps)-1 else "Next →"
        if st.button(label, type="primary", use_container_width=True, key="tour_next"):
            if idx==len(steps)-1:
                _finish_tour(db_path,username)
                st.session_state["show_product_tour"]=False
                st.session_state.pop("product_tour_step",None)
                save_now()
            else:
                st.session_state["product_tour_step"]=idx+1
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

    user_role = st.session_state.get("role", "user")
    username = st.session_state.get("current_user", "User")
    # Revalidate authorization on every app rerun so disabled accounts lose access promptly.
    auth_conn = sqlite3.connect(db_path)
    try:
        auth_row = auth_conn.execute(
            "SELECT role, active FROM app_users WHERE username = ? COLLATE NOCASE LIMIT 1",
            (username,),
        ).fetchone()
    finally:
        auth_conn.close()
    if not auth_row or not auth_row[1] or auth_row[0] != user_role:
        log_event(db_path, username, "ACCOUNT_SESSION_REVOKED", "inactive or role changed")
        remove_user_presence(db_path, st.session_state.get("analytics_session_id", ""))
        st.session_state["authenticated"] = False
        st.session_state.pop("role", None)
        st.session_state.pop("current_user", None)
        st.error("Your account is disabled or its permissions changed. Please log in again.")
        st.rerun()
    if "analytics_session_id" not in st.session_state:
        st.session_state["analytics_session_id"] = secrets.token_urlsafe(18)
    _presence_id = st.session_state["analytics_session_id"]
    update_user_presence(db_path, _presence_id, username, user_role)

    # Background ticker. It used to re-run the WHOLE page every second (st_autorefresh); a re-run that arrives
    # while the page is still processing a click makes Streamlit drop that click (buttons / forms "do nothing").
    # A fragment only re-runs itself and asks for a full re-run only when something really needs it.
    if not st.session_state.get("is_streaming", False):
        _busy = any(
            k.startswith(("job:", "scanjob:")) and v is not None for k, v in list(st.session_state.items())
        )

        @st.fragment(run_every=1 if _busy else 10)
        def _ticker():
            auth_check = sqlite3.connect(db_path)
            try:
                active_row = auth_check.execute(
                    "SELECT active, role FROM app_users WHERE username = ? COLLATE NOCASE LIMIT 1",
                    (username,),
                ).fetchone()
            finally:
                auth_check.close()
            if not active_row or not active_row[0] or active_row[1] != user_role:
                st.rerun()
            update_user_presence(db_path, _presence_id, username, user_role)
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

    render_sidebar_controls(db_path, user_role)
    if not _tour_status(db_path, username) and "show_product_tour" not in st.session_state:
        st.session_state["show_product_tour"] = True
        st.session_state["product_tour_step"] = 0
    if st.session_state.get("show_product_tour"):
        _show_product_tour(db_path, username, user_role)

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
        ("admin_dash", "📊 Live Analytics (Admin)", user_role == "admin"),
    ]
    visible = [(k, label) for k, label, allowed in tab_defs if allowed]
    tabs = dict(zip([k for k, _ in visible], st.tabs([label for _, label in visible])))

    if user_role == "admin":
        with tabs["admin_dash"]:
            render_admin_dashboard(db_path)

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
