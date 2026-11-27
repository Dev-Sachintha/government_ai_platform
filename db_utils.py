import re
import sqlite3

REQUIRED_TABLES = ["government_services", "audit_logs"]


def init_db(db_path: str) -> None:
    """Create all tables the platform needs (safe to call repeatedly)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS government_services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                content TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                username TEXT,
                action TEXT NOT NULL,
                details TEXT
            );
            """
        )
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
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT,
                added_by TEXT,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_fetched TEXT,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                username TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                event_type TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_time ON usage_events(timestamp);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_user ON usage_events(username, timestamp);")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_seen ON user_sessions(last_seen);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_pages_source ON web_pages(source_id);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                page_count INTEGER NOT NULL DEFAULT 1,
                added_by TEXT,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                page_number INTEGER NOT NULL DEFAULT 1,
                text_content TEXT NOT NULL DEFAULT '',
                ai_text TEXT NOT NULL DEFAULT '',
                mime TEXT,
                data BLOB
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_items_file ON library_items(file_id);"
        )
        conn.commit()
    finally:
        conn.close()


def log_event(db_path: str, username: str, action: str, details: str = "") -> None:
    """Write an audit log entry. Never raises (auditing must not break the app)."""
    try:
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO audit_logs (username, action, details) VALUES (?, ?, ?);",
                (username, action, details),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


def is_safe_select(sql: str) -> bool:
    """Allow only a single read-only SELECT / WITH statement."""
    if not sql or not sql.strip():
        return False
    stmt = sql.strip().rstrip(";").strip()
    if ";" in stmt:
        return False
    if not re.match(r"^(select|with)\b", stmt, re.IGNORECASE):
        return False
    return not _FORBIDDEN.search(stmt)

    
def log_usage_event(db_path: str, username: str, role: str, event_type: str, details: str = "") -> None:
    """Record privacy-conscious usage analytics without interrupting the user's task."""
    try:
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO usage_events (username, role, event_type, details) VALUES (?, ?, ?, ?)",
                (str(username)[:100], str(role)[:20], str(event_type)[:40], str(details)[:240]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def update_user_presence(db_path: str, session_id: str, username: str, role: str) -> None:
    """Refresh a session heartbeat; stale sessions are excluded by dashboard queries."""
    try:
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO user_sessions (session_id, username, role, last_seen)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(session_id) DO UPDATE SET
                     username=excluded.username, role=excluded.role, last_seen=CURRENT_TIMESTAMP""",
                (str(session_id)[:100], str(username)[:100], str(role)[:20]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def remove_user_presence(db_path: str, session_id: str) -> None:
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM user_sessions WHERE session_id = ?", (str(session_id)[:100],))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
