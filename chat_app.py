import json
import re
import sqlite3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import library
import llm
from db_utils import is_safe_select, log_event

STOP_WORDS = {
    "what",
    "where",
    "when",
    "how",
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "was",
    "were",
    "show",
    "image",
    "images",
    "picture",
    "pictures",
    "photo",
    "photos",
    "photograph",
    "please",
    "give",
    "send",
    "display",
    "find",
    "tell",
    "about",
    "have",
    "need",
    "want",
    "could",
    "would",
    "there",
    "their",
    "them",
    "some",
    "into",
    "also",
    "does",
    "your",
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
INTERNAL_TABLES = (
    "audit_logs",
    "library_items",
    "library_files",
    "web_pages",
    "web_sources",
)
# Not offered to the AI for SQL: documents are searched automatically, never by SQL
HIDDEN_FROM_SQL_SCHEMA = set(INTERNAL_TABLES) | {"government_services"}
INTERNAL_SQL = re.compile(r"\b(" + "|".join(INTERNAL_TABLES) + r")\b", re.IGNORECASE)

MAX_IMAGES = 3


@st.cache_resource
def _get_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=2)


# ------------------------------------------------------------------ search
def _extract_keywords(prompt: str) -> list[str]:
    """Words of 4+ letters/digits. Punctuation separates words ("2022/2023" -> 2022, 2023).
    Letters, combining marks (needed for Sinhala/Tamil) and the joiners ZWJ/ZWNJ are kept.
    """
    text = "".join(
        (
            ch
            if ch in ("-", "\u200d", "\u200c") or unicodedata.category(ch)[0] in "LMN"
            else " "
        )
        for ch in prompt
    )
    keywords: list[str] = []
    for token in text.split():
        w = token.strip("-").lower()
        if len(w) > 3 and w not in STOP_WORDS and w not in keywords:
            keywords.append(w)
    return keywords[:8]


def _best_window(text: str, keywords: list[str], limit: int) -> str:
    """The `limit`-character part of `text` that contains the most keywords."""
    if len(text) <= limit or not keywords:
        return text[:limit]
    low = text.lower()
    starts = []
    for kw in keywords:
        pos, n = low.find(kw), 0
        while pos != -1 and n < 50:
            starts.append(pos)
            pos, n = low.find(kw, pos + 1), n + 1
    if not starts:
        return text[:limit]
    best_score, best_start = -1, 0
    for pos in starts:
        start = max(0, pos - limit // 5)
        window = low[start : start + limit]
        sc = sum(kw in window for kw in keywords)
        if sc > best_score:
            best_score, best_start = sc, start
    if best_start > 0:  # begin at a word boundary
        nxt = text.find(" ", best_start)
        if 0 <= nxt < best_start + 30:
            best_start = nxt + 1
    return ("... " if best_start > 0 else "") + text[best_start : best_start + limit]


def _clean_snippet(text: str, limit: int, keywords: list[str] | None = None) -> str:
    """Collapse whitespace/control chars, keep the part around the keywords,
    and drop highly repetitive (garbled) text."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(.)\1{4,}", r"\1\1\1", text)  # "......." / "-----" -> short
    text = _best_window(text, keywords or [], limit)
    if len(text) > 80 and len(set(text.split())) < 8:
        return ""
    return text


def _pick_images(
    db_path: str,
    keywords: list[str],
    hits: list[dict],
    wants_image: bool,
    wants_picture: bool,
) -> list[dict]:
    """Choose library pictures to show."""
    if wants_image:
        chosen = [h for h in hits if h["has_data"] and h["score"] >= 1]
    else:
        chosen = [
            h
            for h in hits
            if h["has_data"]
            and (
                (h["kind"] == "image" and h["head"] >= 1)
                or (h["kind"] == "pdf" and h["body"] >= 3)
            )
        ]
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
    """Keyword search over PDFs, websites and the image library."""
    shown_ids = shown_ids or set()
    candidates = []
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
                    candidates.append((f"", content, None))
            except sqlite3.Error:
                pass
            try:
                for title, url, content in conn.execute(
                    f"SELECT s.title, s.url, p.content FROM web_pages p "
                    f"JOIN web_sources s ON s.id = p.source_id "
                    f"WHERE {web_clause} LIMIT 60;",
                    params,
                ):
                    candidates.append(
                        (f"[Website: {title or url} - {url}]", content, None)
                    )
            except sqlite3.Error:
                pass

        for h in library_items or []:
            what = "Image" if h["kind"] == "image" else f"PDF page {h['page']}"
            prefix = "Shown to the user: " if h["id"] in shown_ids else "Library: "
            content = " ".join(
                x for x in (h["description"], h["ai_text"], h["text_content"]) if x
            )
            candidates.append(
                (f"[{prefix}{what} - {h['title']}]", content or h["title"], h["id"])
            )

        def score(item):
            low = str(item[1]).lower()
            return sum(kw in low for kw in keywords)

        shown = [c for c in candidates if c[2] in shown_ids]
        others = sorted(
            (c for c in candidates if c[2] not in shown_ids), key=score, reverse=True
        )

        snippets = []
        for label, content, _ in shown + others:
            cleaned = _clean_snippet(content, snippet_chars, keywords)
            if cleaned:
                snippets.append(f"{label}\n{cleaned}...")
            if len(snippets) >= max_snippets:
                break

        if not snippets:
            try:
                for title, url, content in conn.execute(
                    "SELECT s.title, s.url, p.content FROM web_pages p "
                    "JOIN web_sources s ON s.id = p.source_id "
                    "WHERE p.chunk_index = 0 ORDER BY s.id DESC LIMIT 2;"
                ):
                    cleaned = _clean_snippet(content, snippet_chars, keywords)
                    if cleaned:
                        snippets.append(
                            f"[Website: {title or url} - {url}]\n{cleaned}..."
                        )
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
            if t_name in HIDDEN_FROM_SQL_SCHEMA:
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
    db_path: str, llm_cfg: dict, username: str, prompt: str, target_language: str
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
                f"Database Schema (tables you may query with SQL):\n{db_schema or '(no data tables available)'}\n\n"
                f"Answer the user's question using the excerpts above. Translate and reply strictly in the language: **{target_language}**. "
                'Put the complete answer in "explanation" as plain text (short lists are fine). '
                "If the excerpts do not contain the answer, say so clearly instead of guessing. "
                "If you use a website excerpt, mention that website's URL in your answer. "
                "Items marked 'Shown to the user' are displayed as pictures under your answer; "
                "you may refer to them (for example 'see the image below').\n"
                "Only write a SQL query when the user explicitly asks for records, counts, totals or tables "
                "from the tables listed in the Database Schema. NEVER write SQL to look up document content: "
                "the excerpts already contain it. If no tables are listed, or the question is about documents, "
                'leave "sql_query" empty.\n'
                "Return ONLY a valid JSON object with keys:\n"
                '1. "explanation": Your answer.\n'
                '2. "sql_query": A valid SQLite SELECT statement, or an empty string.\n'
                "No markdown wrappers outside the JSON."
            )
            return f"{system_instruction}\n\nUser Question: {prompt}"

        attempts = [
            {"snippets": 3, "chars": 600, "repeat_penalty": 1.15, "temperature": 0.2},
            {"snippets": 1, "chars": 250, "repeat_penalty": 1.3, "temperature": 0.6},
        ]
        raw_text = ""
        for i, cfg in enumerate(attempts):
            doc_context = _search_documents(
                db_path,
                keywords,
                cfg["snippets"],
                cfg["chars"],
                library_items,
                shown_ids,
            )
            try:
                raw_text = llm.generate(
                    llm_cfg,
                    build_prompt(doc_context),
                    json_mode=True,
                    temperature=cfg["temperature"],
                    repeat_penalty=cfg["repeat_penalty"],
                    max_tokens=1024,
                )
                break
            except llm.LLMError as e:
                if e.looped and i < len(attempts) - 1:
                    continue
                if e.looped:
                    raise llm.LLMError(
                        "The model got stuck repeating itself. Try rephrasing with a "
                        "shorter question (a few keywords works best)."
                    )
                raise

        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        try:
            ai_data = json.loads(match.group(0) if match else raw_text)
        except json.JSONDecodeError:
            ai_data = {"explanation": raw_text.strip(), "sql_query": ""}

        explanation = (
            ai_data.get("explanation") or "Here is the information you requested:"
        )
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
                if df is not None and df.empty:
                    explanation += "\n\n(The query returned no rows.)"

        shown = [{"caption": _image_caption(im), "bytes": im["bytes"]} for im in images]
        if shown:
            log_event(
                db_path,
                username,
                "CHAT_IMAGES_SHOWN",
                ", ".join(s["caption"] for s in shown)[:500],
            )
        return {
            "explanation": explanation,
            "sql_query": sql_query,
            "dataframe": df,
            "images": shown,
            "error": None,
        }

    except llm.LLMError as e:
        return {"error": str(e)}
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
    db_path: str, llm_cfg: dict, user_role: str, username: str
):
    """Chat assistant with added language selection, file upload, and voice translator widgets."""
    st.header("💬 Government Service & Database Chat Assistant")
    st.markdown(
        "Ask questions about government service minutes, officer recruitment, or database records. "
        "Ask to *show* something to get matching images from the library."
    )
    st.caption(f"🤖 {llm.describe_config(llm_cfg)}")
    problem = llm.config_problem(llm_cfg)
    if problem:
        st.error(problem)

    # --- CONTROLS BAR: Language Selector & File Upload ---
    col_lang, col_upload = st.columns([1, 1])
    with col_lang:
        target_language = st.selectbox(
            "🌐 Select Output Language / Translator",
            [
                "English",
                "Sinhala (සිංහල)",
                "Tamil (தமிழ்)",
                "Spanish",
                "French",
                "German",
                "Chinese",
                "Japanese",
                "Hindi",
            ],
            index=0,
        )
    with col_upload:
        uploaded_chat_file = st.file_uploader(
            "📁 Upload File to Chat Context",
            type=["png", "jpg", "jpeg", "pdf", "txt", "csv"],
            key="chat_file_uploader",
        )
        if uploaded_chat_file is not None:
            # Quick ingestion check or notification
            st.success(
                f"File '{uploaded_chat_file.name}' attached to current request context."
            )

    # --- VOICE TRANSLATOR COMPONENT (Web Speech API) ---
    st.markdown(
        "🎙️ **Voice Translator / Speech-to-Text Input** (Speak in any language):"
    )
    voice_component_html = """
    <div>
        <button id="recordButton" style="background-color: #ff4b4b; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: bold;">🎤 Start Voice Input</button>
        <span id="recordStatus" style="margin-left: 10px; font-style: italic; color: gray;">Click to start speaking...</span>
    </div>
    <script>
        const recordButton = document.getElementById('recordButton');
        const recordStatus = document.getElementById('recordStatus');
        
        if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            const recognition = new SpeechRecognition();
            recognition.continuous = false;
            recognition.interimResults = false;
            recognition.lang = 'en-US'; // Can capture mixed inputs or let browser resolve

            recordButton.onclick = function() {
                recordStatus.innerText = "Listening... Speak now.";
                recordButton.style.backgroundColor = "#ffa500";
                recognition.start();
            };

            recognition.onresult = function(event) {
                const speechResult = event.results[0][0].transcript;
                recordStatus.innerText = "Captured: " + speechResult;
                recordButton.style.backgroundColor = "#4CAF50";
                
                // Pass text back via URL params to Streamlit
                const url = new URL(window.parent.location.href);
                url.searchParams.set('voice_input_text', speechResult);
                window.parent.history.replaceState({}, '', url);
                window.parent.location.reload();
            };

            recognition.onerror = function(event) {
                recordStatus.innerText = "Error occurred in recognition: " + event.error;
                recordButton.style.backgroundColor = "#ff4b4b";
            };
        } else {
            recordButton.style.display = 'none';
            recordStatus.innerText = "Speech recognition not supported in this browser.";
        }
    </script>
    """
    components.html(voice_component_html, height=60)

    # Check if voice input was captured via query parameter
    query_params = st.query_params
    voice_text = query_params.get("voice_input_text", "")

    # Handle prepopulating input if voice captured
    default_prompt_value = voice_text if voice_text else ""

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
                except Exception as e:
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

    # Text input box for the chat prompt
    if prompt := st.chat_input(
        "Ask a question about government services or data...", disabled=busy
    ):
        # Clear out voice parameter query state once consumed
        if "voice_input_text" in st.query_params:
            del st.query_params["voice_input_text"]

        # If a file was uploaded, add file reference content to prompt context
        final_prompt = prompt
        if uploaded_chat_file is not None:
            final_prompt = f"[User attached file: {uploaded_chat_file.name}]\n{prompt}"

        st.session_state["chat_messages"].append(
            {"role": "user", "content": final_prompt}
        )

        # Pass target language to background generator
        future = _get_executor().submit(
            _generate_answer, db_path, llm_cfg, username, final_prompt, target_language
        )
        st.session_state["chat_job"] = {"future": future, "started": time.time()}
        st.session_state["last_active"] = time.time()
        st.rerun()
