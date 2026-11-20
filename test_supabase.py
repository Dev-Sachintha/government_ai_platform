import streamlit as st
from supabase_db import test_connection

st.set_page_config(page_title="Supabase Connection Test")

st.title("Government AI Platform")
st.subheader("Supabase Database Connection")

try:
    version = test_connection()

    st.success("Supabase connection successful!")
    st.write("PostgreSQL Version:")
    st.code(version)

except Exception as e:
    st.error("Supabase connection failed.")
    st.code(str(e))
