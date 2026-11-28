from io import BytesIO
import os
import shutil
import sqlite3
import tempfile
import zipfile
import stat

from db_utils import REQUIRED_TABLES


def _inspect_and_verify_sqlite(
    temp_db_path: str, expected_tables: list
) -> tuple[bool, str]:
    """Helper method extracted to handle DB connection, integrity check, and schema verification."""
    conn = None
    try:
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()

        # 1. PRAGMA integrity_check
        cursor.execute("PRAGMA integrity_check;")
        if (integrity_result := cursor.fetchone()[0]) != "ok":
            return False, f"Integrity check failed: {integrity_result}"

        # 2. Schema check
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        )
        existing_tables = [row[0] for row in cursor.fetchall()]

        if missing := [t for t in expected_tables if t not in existing_tables]:
            return False, f"Missing required tables: {missing}"

        return True, "Integrity and schema validation passed."
    except Exception as inspect_err:
        return False, f"Inspection error: {inspect_err}"
    finally:
        if conn is not None:
            conn.close()


def _not_a_database_message(data: bytes) -> str:
    """Explain what is wrong when an uploaded file is not an SQLite database."""
    if data.startswith(b"%PDF"):
        return (
            "This is a PDF, not a database backup. Restore DB only accepts a backup made with "
            "'Download backup'. To add a PDF, use the 'Image & PDF Library' tab."
        )
    if data.startswith(b"PK"):
        return (
            "This looks like a ZIP/Office file, not a database backup. Restore DB accepts a .db backup "
            "(or a .zip that contains database.db). To add documents, use the 'Image & PDF Library' tab."
        )
    if data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1") or data[:3] == b"GIF":
        return "This is a picture, not a database backup. To add pictures, use the 'Image & PDF Library' tab."
    return (
        "This file is not a database backup (it is not an SQLite database). Restore DB only accepts a "
        "backup made with 'Download backup'. To add PDFs or pictures, use the 'Image & PDF Library' tab."
    )


def validate_and_restore_sqlite(
    uploaded_file, db_path: str, expected_tables: list = None
) -> tuple[bool, str]:
    """Validates integrity and schema of uploaded SQLite backup before overwriting production."""
    if expected_tables is None:
        expected_tables = REQUIRED_TABLES

    try:
        file_bytes = uploaded_file.getvalue()

        if len(file_bytes) > 100 * 1024 * 1024:
            return False, "Backup upload is too large (maximum 100 MB)."

        if uploaded_file.name.lower().endswith(".zip"):
            with zipfile.ZipFile(BytesIO(file_bytes), "r") as zip_ref:
                entries = zip_ref.infolist()
                if len(entries) > 100:
                    return False, "Invalid archive: too many files."
                candidates = []
                for entry in entries:
                    normalized = entry.filename.replace("\\", "/")
                    parts = normalized.split("/")
                    mode = (entry.external_attr >> 16) & 0xFFFF
                    if normalized.startswith("/") or any(part in ("..", "") for part in parts if part != ""):
                        return False, "Invalid archive: unsafe file path."
                    if stat.S_ISLNK(mode):
                        return False, "Invalid archive: symbolic links are not allowed."
                    if entry.file_size > 512 * 1024 * 1024:
                        return False, "Invalid archive: database entry exceeds 512 MB."
                    if entry.filename == "database.db" or entry.filename.endswith("/database.db"):
                        candidates.append(entry)
                if len(candidates) != 1:
                    return False, "Invalid archive: expected exactly one database.db file."
                entry = candidates[0]
                if entry.file_size == 0 or (entry.compress_size and entry.file_size / entry.compress_size > 200):
                    return False, "Invalid archive: suspicious compression ratio."
                with zip_ref.open(entry, "r") as source:
                    db_bytes = source.read(512 * 1024 * 1024 + 1)
                if len(db_bytes) > 512 * 1024 * 1024:
                    return False, "Invalid archive: extracted database is too large."
        else:
            db_bytes = file_bytes

        if not db_bytes.startswith(b"SQLite format 3\x00"):
            return False, _not_a_database_message(db_bytes)

        target_dir = os.path.dirname(os.path.abspath(db_path))
        temp_db_path = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".govai-restore-", suffix=".db", dir=target_dir, delete=False
            ) as temp_db:
                temp_db.write(db_bytes)
                temp_db_path = temp_db.name

            success, msg = _inspect_and_verify_sqlite(temp_db_path, expected_tables)
            if not success:
                return False, msg

            # Same-directory atomic replacement prevents a partially-written live DB.
            os.replace(temp_db_path, db_path)
            temp_db_path = None
            for suffix in ("-wal", "-shm"):
                sidecar = db_path + suffix
                if os.path.exists(sidecar):
                    os.remove(sidecar)
            return True, "Database passed integrity and schema validation! Restored successfully."
        finally:
            if temp_db_path and os.path.exists(temp_db_path):
                os.remove(temp_db_path)

    except Exception as e:
        return False, f"Failed to process file: {e}"
