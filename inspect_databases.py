import sqlite3
from pathlib import Path

databases = [
    Path(r"D:\calcu\database.db"),
    Path(r"D:\government-ai-platform-desktop\database.db")
]

for db in databases:
    print("\n" + "=" * 60)
    print("DATABASE:", db)
    print("EXISTS:", db.exists())

    if not db.exists():
        continue

    conn = sqlite3.connect(str(db))

    tables = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table'
        AND name NOT LIKE 'sqlite_%'
    """).fetchall()

    for (name,) in tables:
        count = conn.execute(
            f'SELECT COUNT(*) FROM "{name}"'
        ).fetchone()[0]

        print(f"{name}: {count} rows")

    conn.close()
