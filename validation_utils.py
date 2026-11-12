from io import BytesIO
import os
import shutil
import sqlite3
import tempfile
import zipfile

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

        if uploaded_file.name.lower().endswith(".zip"):
            with zipfile.ZipFile(BytesIO(file_bytes), "r") as zip_ref:
                target_file = next(
                    (
                        f
                        for f in zip_ref.namelist()
                        if f.endswith("database.db") or f == "database.db"
                    ),
                    None,
                )
                if not target_file:
                    return False, "Invalid archive: 'database.db' not found."
                db_bytes = zip_ref.read(target_file)
        else:
            db_bytes = file_bytes

        if not db_bytes.startswith(b"SQLite format 3\x00"):
            return False, _not_a_database_message(db_bytes)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as temp_db:
            temp_db.write(db_bytes)
            temp_db_path = temp_db.name

        success, msg = _inspect_and_verify_sqlite(temp_db_path, expected_tables)

        if success:
            # shutil.move works across filesystems (os.replace does not)
            shutil.move(temp_db_path, db_path)
            return (
                True,
                "Database passed integrity and schema validation! Restored successfully.",
            )

        if os.path.exists(temp_db_path):
            os.unlink(temp_db_path)
        return False, msg

    except Exception as e:
        return False, f"Failed to process file: {e}"
