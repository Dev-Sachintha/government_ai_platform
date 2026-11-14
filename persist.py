"""Durable storage for hosts with a temporary disk (Streamlit Community Cloud).

Problem: on Streamlit Cloud the app folder is wiped whenever the app restarts,
sleeps, or is redeployed, so websites, library files and chats added at runtime
disappear. This module keeps a compressed copy of the SQLite database in a
PRIVATE GitHub repository (a separate "data" repo, NOT the app repo) and
restores it automatically on startup.

Secrets needed (Streamlit: Manage app > Settings > Secrets):
    PERSIST_GITHUB_TOKEN = "github_pat_..."   # fine-grained token, Contents: read & write on the data repo
    PERSIST_GITHUB_REPO  = "your-name/gov-ai-data"
    PERSIST_GITHUB_BRANCH = "main"            # optional
    PERSIST_GITHUB_PATH   = "database.db.gz"  # optional

No Streamlit calls here: safe to run in a background thread.
"""
import base64
import gzip
import os
import sqlite3
import tempfile
import threading
import time

import requests

API = "https://api.github.com"
MIN_SECONDS_BETWEEN_UPLOADS = 10
TIMEOUT = 60

SECRET_KEYS = [
    "PERSIST_GITHUB_TOKEN",
    "PERSIST_GITHUB_REPO",
    "PERSIST_GITHUB_BRANCH",
    "PERSIST_GITHUB_PATH",
]

# Process-wide state (shared by all browser sessions and the upload thread).
_lock = threading.Lock()
_state = {
    "restore_done": False,
    "restore_ok": False,      # True only if remote was read (or confirmed absent)
    "last_sig": None,
    "last_upload": 0.0,
    "last_saved": "",
    "last_error": "",
    "restored_msg": "",
    "uploading": False,
}


def get_config(secrets: dict) -> dict | None:
    def pick(key, default=""):
        return str(secrets.get(key) or os.environ.get(key) or default).strip()

    token, repo = pick("PERSIST_GITHUB_TOKEN"), pick("PERSIST_GITHUB_REPO")
    if not token or not repo or "/" not in repo:
        return None
    return {
        "token": token,
        "repo": repo.strip("/"),
        "branch": pick("PERSIST_GITHUB_BRANCH", "main"),
        "path": pick("PERSIST_GITHUB_PATH", "database.db.gz").lstrip("/"),
    }


def status() -> dict:
    with _lock:
        return dict(_state)


def _headers(cfg, raw=False):
    accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "GovAIPlatform-persist",
    }


def _url(cfg):
    return f"{API}/repos/{cfg['repo']}/contents/{cfg['path']}"


def _db_sig(db_path):
    sig = []
    for p in (db_path, db_path + "-wal"):
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _snapshot_bytes(db_path) -> bytes:
    """Consistent copy of the live DB, gzip-compressed."""
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
            return gzip.compress(fh.read(), compresslevel=6)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _valid_sqlite(path) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            ok = conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';")}
        finally:
            conn.close()
        return ok and "audit_logs" in tables
    except Exception:
        return False


# ------------------------------------------------------------------ restore
def restore_if_needed(cfg: dict | None, db_path: str) -> None:
    """Run once per process (cold start): pull the saved DB over the fresh local one."""
    with _lock:
        if _state["restore_done"]:
            return
        _state["restore_done"] = True
    if not cfg:
        return
    try:
        meta = requests.get(_url(cfg), headers=_headers(cfg), params={"ref": cfg["branch"]}, timeout=TIMEOUT)
        if meta.status_code == 404:
            with _lock:
                _state["restore_ok"] = True   # nothing saved yet: first upload is allowed
                _state["restored_msg"] = "No saved copy found yet; the first save will create it."
            return
        meta.raise_for_status()
        raw = requests.get(_url(cfg), headers=_headers(cfg, raw=True), params={"ref": cfg["branch"]}, timeout=TIMEOUT)
        raw.raise_for_status()
        data = gzip.decompress(raw.content)
        tmp = tempfile.mktemp(suffix=".db")
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            if not _valid_sqlite(tmp):
                raise ValueError("saved copy failed the integrity check")
            src = sqlite3.connect(tmp)
            dst = sqlite3.connect(db_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        with _lock:
            _state["restore_ok"] = True
            _state["last_sig"] = _db_sig(db_path)
            _state["restored_msg"] = "Saved data restored from GitHub."
    except Exception as e:  # never block the app; but never overwrite remote after a failed read
        with _lock:
            _state["restore_ok"] = False
            _state["last_error"] = f"Restore failed: {e}"


# --------------------------------------------------------------------- save
def _upload(cfg, db_path, sig_before):
    try:
        payload = _snapshot_bytes(db_path)
        sha = None
        meta = requests.get(_url(cfg), headers=_headers(cfg), params={"ref": cfg["branch"]}, timeout=TIMEOUT)
        if meta.status_code == 200:
            sha = meta.json().get("sha")
        elif meta.status_code != 404:
            meta.raise_for_status()
        body = {
            "message": "Auto-save database " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "content": base64.b64encode(payload).decode("ascii"),
            "branch": cfg["branch"],
        }
        if sha:
            body["sha"] = sha
        res = requests.put(_url(cfg), headers=_headers(cfg), json=body, timeout=TIMEOUT * 2)
        res.raise_for_status()
        with _lock:
            _state["last_sig"] = sig_before
            _state["last_saved"] = time.strftime("%H:%M:%S UTC", time.gmtime())
            _state["last_error"] = ""
    except Exception as e:
        with _lock:
            _state["last_error"] = f"Save failed: {e}"
    finally:
        with _lock:
            _state["uploading"] = False
            _state["last_upload"] = time.time()


def maybe_save(cfg: dict | None, db_path: str, force: bool = False) -> None:
    """Start a background upload if the DB changed since the last save."""
    if not cfg:
        return
    sig = _db_sig(db_path)
    with _lock:
        if not _state["restore_ok"] or _state["uploading"]:
            return
        if not force:
            if sig == _state["last_sig"]:
                return
            if time.time() - _state["last_upload"] < MIN_SECONDS_BETWEEN_UPLOADS:
                return
        _state["uploading"] = True
    threading.Thread(target=_upload, args=(cfg, db_path, sig), daemon=True).start()
