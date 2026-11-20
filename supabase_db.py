import streamlit as st
from sqlalchemy import create_engine, text


@st.cache_resource
def get_engine():
    database_url = st.secrets["DATABASE_URL"]

    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(
        database_url, connect_args={"sslmode": "require"}, pool_pre_ping=True
    )


def test_connection():
    engine = get_engine()

    with engine.connect() as connection:
        result = connection.execute(text("SELECT version()"))

        return result.scalar()
