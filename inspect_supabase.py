import streamlit as st
from sqlalchemy import text
from supabase_db import get_engine

st.set_page_config(page_title="Supabase Database Inspector")
st.title("Supabase Database Inspector")

try:
    engine = get_engine()

    with engine.connect() as conn:
        tables = conn.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)).fetchall()

        st.success("Supabase connected successfully!")

        if not tables:
            st.warning("No tables found in public schema.")

        for (table_name,) in tables:
            safe_name = table_name.replace('"', '""')

            count = conn.execute(
                text(f'SELECT COUNT(*) FROM public."{safe_name}"')
            ).scalar()

            st.write(f"**{table_name}:** {count} rows")

except Exception as e:
    st.error("Database inspection failed.")
    st.code(str(e))
