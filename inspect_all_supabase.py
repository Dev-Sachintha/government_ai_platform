import streamlit as st
from sqlalchemy import text
from supabase_db import get_engine

st.set_page_config(page_title="Supabase Full Inspector")
st.title("Supabase Full Database Inspector")

try:
    engine = get_engine()

    with engine.connect() as conn:

        info = conn.execute(text("""
            SELECT current_database(), current_user, current_schema()
        """)).fetchone()

        st.success("Supabase connection successful!")

        st.write("Database:", info[0])
        st.write("User:", info[1])
        st.write("Current schema:", info[2])

        tables = conn.execute(text("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN
            ('information_schema', 'pg_catalog')
            AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
        """)).fetchall()

        st.subheader("All Database Tables")

        if not tables:
            st.warning("No user tables found in this database.")

        for schema, table in tables:
            safe_schema = schema.replace('"', '""')
            safe_table = table.replace('"', '""')

            query = (
                f'SELECT COUNT(*) FROM "{safe_schema}"."{safe_table}"'
            )

            count = conn.execute(text(query)).scalar()

            st.write(f"{schema}.{table}: {count} rows")

except Exception as e:
    st.error("Inspection failed")
    st.code(str(e))
