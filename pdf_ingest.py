import io
import sqlite3
import zipfile
import pypdf

def init_document_table(db_path: str):
    """Creates the government_services table for storing PDF text."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS government_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            content TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

def ingest_zip_pdfs_to_sqlite(zip_path: str, db_path: str):
    """Extracts text from all PDFs inside the ZIP archive and inserts them into SQLite."""
    init_document_table(db_path)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM government_services;")
    conn.commit()

    with zipfile.ZipFile(zip_path, "r") as z:
        for filename in z.namelist():
            if filename.lower().endswith(".pdf") and not filename.startswith("__MACOSX"):
                with z.open(filename) as f:
                    try:
                        reader = pypdf.PdfReader(io.BytesIO(f.read()))
                        for page_num, page in enumerate(reader.pages, start=1):
                            text = page.extract_text()
                            if text and text.strip():
                                cursor.execute(
                                    """
                                    INSERT INTO government_services (filename, page_number, content)
                                    VALUES (?, ?, ?);
                                    """,
                                    (filename, page_num, text.strip()),
                                )
                    except Exception as e:
                        print(f"Error processing {filename}: {e}")

    conn.commit()
    conn.close()
    print("Government service PDFs successfully ingested into SQLite database!")

if __name__ == "__main__":
    import os
    if os.path.exists("Downloads.zip"):
        ingest_zip_pdfs_to_sqlite("Downloads.zip", "database.db")
    else:
        print("Downloads.zip not found in current directory.")
