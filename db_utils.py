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
