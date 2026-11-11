import json
import re
import sqlite3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st

import library
from db_utils import is_safe_select, log_event

STOP_WORDS = {
    "what", "where", "when", "how", "the", "and", "for", "with", "that", "this",
    "from", "are", "was", "were", "show", "image", "images", "picture", "pictures",
    "photo", "photos", "photograph", "please", "give", "send", "display", "find",
    "tell", "about", "have", "need", "want", "could", "would", "there", "their",
    "them", "some", "into", "also", "does", "your",
}
# Words that mean "I want to SEE something"
IMAGE_INTENT = re.compile(
    r"\b(image|images|picture|pictures|photo|photos|photograph|photographs|pic|pics|"
    r"diagram|chart|map|screenshot|illustration|show|display|scan|scanned|poster|logo)\b",
    re.IGNORECASE,
)
# Words that clearly ask for a picture (used for the "latest images" fallback and the "none found" note)
PICTURE_WORDS = re.compile(
    r"\b(image|images|picture|pictures|photo|photos|photograph|photographs|pic|pics|"
    r"diagram|map|screenshot|illustration|poster|logo)\b",
    re.IGNORECASE,
)
# Internal tables the AI must never read or show
INTERNAL_TABLES = ("audit_logs", "library_items", "library_files", "web_pages", "web_sources")
INTERNAL_SQL = re.compile(r"\b(" + "|".join(INTERNAL_TABLES) + r")\b", re.IGNORECASE)

MAX_IMAGES = 3
OLLAMA_TIMEOUT = 600  # seconds; local models can be slow, especially the first request


@st.cache_resource
def _get_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=2)


# ------------------------------------------------------------------ search
def _clean_token(token: str) -> str:
    """Keep letters, combining marks (needed for Sinhala/Tamil), digits and '-'."""
    return "".join(
        ch
        for ch in token
        if ch in ("-", "\u200d", "\u200c") or unicodedata.category(ch)[0] in "LMN"
    )


def _extract_keywords(prompt: str) -> list[str]:
    keywords: list[str] = []
    for token in prompt.split():
        w = _clean_token(token).lower()
        if len(w) > 3 and w not in STOP_WORDS and w not in keywords:
            keywords.append(w)
    return keywords[:8]


def _clean_snippet(text: str, limit: int) -> str:
    """Collapse whitespace/control chars and drop highly repetitive (garbled) text."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(.)\1{4,}", r"\1\1\1", text)  # "......." / "-----" -> short
    text = text[:limit]
    if len(text) > 80 and len(set(text.split())) < 8:
        return ""
    return text


def _pick_images(
    db_path: str, keywords: list[str], hits: list[dict], wants_image: bool, wants_picture: bool
) -> list[dict]:
    """Choose library pictures to show.
    - The user asked to see something ("show", "image", ...): any match counts.
    - Otherwise only strong matches: a keyword in the title/description, or 3+ words in the text."""
    if wants_image:
        chosen = [h for h in hits if h["has_data"] and h["score"] >= 1]
    else:
        chosen = [h for h in hits if h["has_data"] and (h["head"] >= 1 or h["body"] >= 3)]
    if not keywords and wants_picture:
        chosen = library.latest_images(db_path, MAX_IMAGES)
    chosen = chosen[:MAX_IMAGES]
    images = []
    for h in chosen:
        data = library.get_item_image(db_path, h["id"])
        if data:
            images.append(dict(h, bytes=data))
    return images


def _search_documents(
    db_path: str,
    keywords: list[str],
    max_snippets: int = 3,
    snippet_chars: int = 600,
    library_items: list[dict] | None = None,
    shown_ids: set | None = None,
) -> str:
    """Keyword search over PDFs, websites and the image library; best matches first.
    Pictures that will be shown to the user always come first."""
    shown_ids = shown_ids or set()
    candidates = []  # (label, content, library_item_id or None)
    conn = sqlite3.connect(db_path)
    try:
        if keywords:
            params = [f"%{kw}%" for kw in keywords]
            pdf_clause = " OR ".join(["content LIKE ?"] * len(keywords))
            web_clause = " OR ".join(["p.content LIKE ?"] * len(keywords))
            try:
                for fn, pg, content in conn.execute(
                    f"SELECT filename, page_number, content FROM government_services "
                    f"WHERE {pdf_clause} LIMIT 40;",
                    params,
                ):
                    candidates.append((f"[Source: {fn} (Page {pg})]", content, None))
            except sqlite3.Error:
                pass
            try:
                for title, url, content in conn.execute(
                    f"SELECT s.title, s.url, p.content FROM web_pages p "
                    f"JOIN web_sources s ON s.id = p.source_id "
                    f"WHERE {web_clause} LIMIT 60;",
                    params,
                ):
                    candidates.append((f"[Website: {title or url} - {url}]", content, None))
            except sqlite3.Error:
                pass

        for h in library_items or []:
            what = "Image" if h["kind"] == "image" else f"PDF page {h['page']}"
            prefix = "Shown to the user: " if h["id"] in shown_ids else "Library: "
            content = " ".join(x for x in (h["description"], h["ai_text"], h["text_content"]) if x)
            candidates.append((f"[{prefix}{what} - {h['title']}]", content or h["title"], h["id"]))

        def score(item):
            low = str(item[1]).lower()
            return sum(kw in low for kw in keywords)

        shown = [c for c in candidates if c[2] in shown_ids]
        others = sorted((c for c in candidates if c[2] not in shown_ids), key=score, reverse=True)

        snippets = []
        for label, content, _ in shown + others:
            cleaned = _clean_snippet(content, snippet_chars)
            if cleaned:
                snippets.append(f"{label}\n{cleaned}...")
            if len(snippets) >= max_snippets:
                break

        if not snippets:
            # No keyword match (e.g. "what is this website about?"): use the
            # opening section of the most recently added websites.
            try:
                for title, url, content in conn.execute(
                    "SELECT s.title, s.url, p.content FROM web_pages p "
                    "JOIN web_sources s ON s.id = p.source_id "
                    "WHERE p.chunk_index = 0 ORDER BY s.id DESC LIMIT 2;"
                ):
                    cleaned = _clean_snippet(content, snippet_chars)
                    if cleaned:
                        snippets.append(f"[Website: {title or url} - {url}]\n{cleaned}...")
            except sqlite3.Error:
                pass
    finally:
        conn.close()
    return "\n\n".join(snippets)


def _get_schema(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
        lines = []
        for (t_name,) in tables:
            if t_name in INTERNAL_TABLES:
                continue
            lines.append(f"Table: {t_name}")
            for c in conn.execute(f'PRAGMA table_info("{t_name}");').fetchall():
                lines.append(f"  - {c[1]} ({c[2]})")
        return "\n".join(lines)
    finally:
        conn.close()


def _image_caption(h: dict) -> str:
    return h["title"] if h["kind"] == "image" else f"{h['title']} - page {h['page']}"


# --------------------------------------------------------------- the model
def _generate_answer(
    db_path: str, model_name: str, ollama_url: str, username: str, prompt: str
) -> dict:
    """Runs in a BACKGROUND THREAD. Must not call any st.* function."""
    try:
        db_schema = _get_schema(db_path)
        keywords = _extract_keywords(prompt)
        wants_image = bool(IMAGE_INTENT.search(prompt))
        wants_picture = bool(PICTURE_WORDS.search(prompt))

        hits = library.search_items(db_path, keywords)
        images = _pick_images(db_path, keywords, hits, wants_image, wants_picture)
        shown_ids = {im["id"] for im in images}
        hit_ids = {h["id"] for h in hits}
        library_items = hits + [im for im in images if im["id"] not in hit_ids]

        note = ""
        if wants_picture and not images:
            note = "\n[Note: no matching image was found in the library, so no picture is shown.]"

        def build_prompt(doc_context: str) -> str:
            system_instruction = (
                "You are an expert AI assistant specializing in Sri Lankan Government Services and database reporting.\n"
                f"Relevant Excerpts (government PDFs, websites and images added by the admin):\n"
                f"{doc_context or 'No specific excerpt found.'}{note}\n\n"
                f"Database Schema:\n{db_schema}\n\n"
                "Answer the user's question accurately using the excerpts above. "
                "If you use a website excerpt, mention that website's URL in your answer. "
                "Items marked 'Shown to the user' are displayed as pictures under your answer; "
                "you may refer to them (for example 'see the image below'). "
                "If the question requires structured database data, provide a valid SQLite query and explanation as JSON, "
                "otherwise provide a direct textual answer.\n"
                "Return ONLY a valid JSON object with keys:\n"
                '1. "explanation": Your detailed answer or explanation.\n'
                '2. "sql_query": A valid SQLite SELECT statement (or empty string if not querying the database).\n'
                "No markdown wrappers outside the JSON."
            )
            return f"{system_instruction}\n\nUser Question: {prompt}"

        # Attempt 1: normal context. Attempt 2 (if the model loops): less context,
        # stronger repetition penalty, higher temperature.
        attempts = [
            {"snippets": 3, "chars": 600, "repeat_penalty": 1.15, "temperature": 0.2},
            {"snippets": 1, "chars": 250, "repeat_penalty": 1.3, "temperature": 0.6},
        ]
        response = None
        for cfg in attempts:
            doc_context = _search_documents(
                db_path, keywords, cfg["snippets"], cfg["chars"], library_items, shown_ids
            )
            response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": model_name,
                    "prompt": build_prompt(doc_context),
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": cfg["temperature"],
                        "repeat_penalty": cfg["repeat_penalty"],
                        "num_ctx": 4096,
                        "num_predict": 1024,
                    },
                },
                timeout=OLLAMA_TIMEOUT,
            )
            looped = response.status_code == 500 and "repeat" in response.text.lower()
            if not looped:
                break

        if response.status_code != 200:
            if "repeat" in response.text.lower():
                raise RuntimeError(
                    "The model got stuck repeating itself. Try rephrasing with a "
                    "shorter question (a few keywords works best)."
                )
            raise RuntimeError(f"Ollama error {response.status_code}: {response.text}")

        raw_text = response.json().get("response", "")
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        try:
            ai_data = json.loads(match.group(0) if match else raw_text)
        except json.JSONDecodeError:
            ai_data = {"explanation": raw_text.strip(), "sql_query": ""}

        explanation = ai_data.get("explanation") or "Here is the information you requested:"
        if not isinstance(explanation, str):
            explanation = json.dumps(explanation, ensure_ascii=False, indent=2)
        sql_query = (ai_data.get("sql_query") or "").strip()

        df = None
        if sql_query:
            if not is_safe_select(sql_query) or INTERNAL_SQL.search(sql_query):
                explanation += (
                    "\n\n⚠️ The generated SQL was blocked because only read-only "
                    "SELECT queries on the data tables are allowed."
                )
                log_event(db_path, username, "BLOCKED_SQL", sql_query[:500])
                sql_query = ""
            else:
                conn = sqlite3.connect(db_path)
                try:
                    df = pd.read_sql_query(sql_query, conn)
                finally:
                    conn.close()
                log_event(db_path, username, "AI_SQL_QUERY", sql_query[:500])

        shown = [{"caption": _image_caption(im), "bytes": im["bytes"]} for im in images]
        if shown:
            log_event(db_path, username, "CHAT_IMAGES_SHOWN", ", ".join(s["caption"] for s in shown)[:500])
        return {
            "explanation": explanation,
            "sql_query": sql_query,
            "dataframe": df,
            "images": shown,
            "error": None,
        }

    except requests.exceptions.ConnectionError:
        return {"error": f"Cannot connect to Ollama at {ollama_url}. Make sure Ollama is running."}
    except requests.exceptions.Timeout:
        return {"error": "Ollama took too long to respond. Try a shorter question or a smaller model."}
    except Exception as e:
        return {"error": f"Error processing query: {e}"}


# ------------------------------------------------------------------- the UI
def _render_message(message: dict):
    st.markdown(message["content"])
    if message.get("sql_query"):
        st.code(message["sql_query"], language="sql")
    if message.get("dataframe") is not None and not message["dataframe"].empty:
        st.dataframe(message["dataframe"], width="stretch")
    for img in message.get("images") or []:
        st.image(img["bytes"], caption=img["caption"], width=520)


def render_ai_chat_interface(
    db_path: str, model_name: str, ollama_url: str, user_role: str, username: str
):
    """Chat assistant. The model call runs in a background thread so the 1-second
    auto-refresh (session timer) cannot interrupt it."""
    st.header("💬 Government Service & Database Chat Assistant")
    st.markdown(
        "Ask questions about government service minutes, officer recruitment, or database records. "
        "Ask to *show* something to get matching images from the library."
    )

    if "chat_messages" not in st.session_state:
        st.session_state["chat_messages"] = [
            {
                "role": "assistant",
                "content": "Hello! I am your AI assistant for government services and database records. How can I help you today?",
            }
        ]

    for message in st.session_state["chat_messages"]:
        with st.chat_message(message["role"]):
            _render_message(message)

    job = st.session_state.get("chat_job")
    if job is not None:
        with st.chat_message("assistant"):
            if job["future"].done():
                try:
                    result = job["future"].result()
                except Exception as e:  # defensive; worker already catches errors
                    result = {"error": f"Error processing query: {e}"}

                if result.get("error"):
                    st.session_state["chat_messages"].append(
                        {"role": "assistant", "content": f"❌ {result['error']}"}
                    )
                else:
                    st.session_state["chat_messages"].append(
                        {
                            "role": "assistant",
                            "content": result["explanation"],
                            "sql_query": result["sql_query"] or None,
                            "dataframe": result["dataframe"],
                            "images": result.get("images") or [],
                        }
                    )
                st.session_state["chat_job"] = None
                st.session_state["last_active"] = time.time()
                st.rerun()
            else:
                waited = int(time.time() - job["started"])
                st.info(
                    f"⏳ Thinking... {waited}s (the first answer can take a minute "
                    "while the model loads)"
                )

    busy = st.session_state.get("chat_job") is not None
    if prompt := st.chat_input(
        "Ask a question about government services or data...", disabled=busy
    ):
        st.session_state["chat_messages"].append({"role": "user", "content": prompt})
        future = _get_executor().submit(
            _generate_answer, db_path, model_name, ollama_url, username, prompt
        )
        st.session_state["chat_job"] = {"future": future, "started": time.time()}
        st.session_state["last_active"] = time.time()
        st.rerun()
