import io
import json
import re
import sqlite3
import time
import unicodedata
import uuid

import pandas as pd
import pypdf
import streamlit as st

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
IMAGE_INTENT = re.compile(
    r"\b(image|images|picture|pictures|photo|photos|photograph|photographs|pic|pics|"
    r"diagram|chart|map|screenshot|illustration|show|display|scan|scanned|poster|logo)\b",
    re.IGNORECASE,
)
PICTURE_WORDS = re.compile(
    r"\b(image|images|picture|pictures|photo|photos|photograph|photographs|pic|pics|"
    r"diagram|map|screenshot|illustration|poster|logo)\b",
    re.IGNORECASE,
)
INTERNAL_TABLES = (
    "audit_logs",
    "library_items",
    "library_files",
    "web_pages",
    "web_sources",
    "chat_threads",
    "chat_messages",
    "shared_links",
)
HIDDEN_FROM_SQL_SCHEMA = set(INTERNAL_TABLES) | {"government_services"}
INTERNAL_SQL = re.compile(r"\b(" + "|".join(INTERNAL_TABLES) + r")\b", re.IGNORECASE)
MAX_IMAGES = 3

PERSONAS = {
    "🏛️ Sri Lankan Gov & Data Analyst (Default)": (
        "You are an expert AI assistant specializing in Sri Lankan Government Services, "
        "policy circulars, and database reporting. Answer accurately using provided excerpts, "
        "web citations, and SQLite data."
    ),
    "🗄️ SQL & Database Architect": (
        "You are a senior SQLite and data engineering expert. Write clean, optimized read-only "
        "SELECT queries, explain schemas clearly, and format tabular insights."
    ),
    "✍️ Executive Copywriter & Policy Drafter": (
        "You are an executive government and business copywriter. Produce structured, formal "
        "memos, circulars, letters, and polished reports ready for publication."
    ),
    "🎓 Study Tutor & Technical Explainer": (
        "You are a patient, insightful tutor. Break complex concepts down step-by-step using "
        "clear analogies, formatted tables, code snippets, and LaTeX math ($...$)."
    ),
    "⚙️ Custom System Persona": "",
}

PROMPT_TEMPLATES = {
    "Select a quick template...": "",
    "📄 Summarize Attached Document": "Summarize the key points, dates, and action items from the attached document in bullet points.",
    "📊 Query Database Records": "Show me a summary table of records from the available database tables.",
    "🌐 Draft Official Notice (HTML)": "Draft a clean, responsive HTML official public notice with a header, summary table, and contact footer.",
    "🔍 Latest Policy / News Check": "Search the web for the latest updates on this topic and provide cited source links.",
}

TONE_TEMPS = {
    "🎯 Precise & Analytical (0.1)": 0.1,
    "⚖️ Balanced & Helpful (0.4)": 0.4,
    "🎨 Creative & Expressive (0.8)": 0.8,
}


# ------------------------------------------------------------------ SQLite Thread Storage
def init_chat_tables(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_threads (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                title TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sql_query TEXT,
                citations_json TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES chat_threads(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shared_links (
                token TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                title TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """)
        conn.commit()
    finally:
        conn.close()


def _create_thread(db_path: str, username: str, title: str = "New Conversation") -> str:
    tid = str(uuid.uuid4())
    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO chat_threads (id, username, title, pinned, updated_at) VALUES (?, ?, ?, 0, ?)",
            (tid, username, title, now),
        )
        conn.execute(
            "INSERT INTO chat_messages (thread_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (
                tid,
                "Hello! I am your AI Workspace Assistant. Ask questions, drag & drop files, dictate via voice, or open the split-screen Canvas.",
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def _list_threads(db_path: str, username: str, search: str = "") -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        if search.strip():
            q = f"%{search.strip()}%"
            rows = conn.execute(
                """
                SELECT DISTINCT t.id, t.title, t.pinned, t.updated_at
                FROM chat_threads t
                LEFT JOIN chat_messages m ON m.thread_id = t.id
                WHERE t.username = ? AND (t.title LIKE ? OR m.content LIKE ?)
                ORDER BY t.pinned DESC, t.updated_at DESC
                """,
                (username, q, q),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, pinned, updated_at FROM chat_threads WHERE username = ? ORDER BY pinned DESC, updated_at DESC",
                (username,),
            ).fetchall()
        return [
            {"id": r[0], "title": r[1], "pinned": bool(r[2]), "updated_at": r[3]}
            for r in rows
        ]
    finally:
        conn.close()


def _load_messages(db_path: str, thread_id: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, role, content, sql_query, citations_json FROM chat_messages WHERE thread_id = ? ORDER BY id ASC",
            (thread_id,),
        ).fetchall()
        msgs = []
        for r in rows:
            df = None
            sql_q = r[3] or ""
            if sql_q and is_safe_select(sql_q) and not INTERNAL_SQL.search(sql_q):
                try:
                    df = pd.read_sql_query(sql_q, conn)
                except Exception:
                    df = None
            msgs.append(
                {
                    "id": r[0],
                    "role": r[1],
                    "content": r[2],
                    "sql_query": sql_q or None,
                    "dataframe": df,
                    "citations": json.loads(r[4]) if r[4] else [],
                    "images": [],
                }
            )
        return msgs
    finally:
        conn.close()


def _save_message(
    db_path: str,
    thread_id: str,
    role: str,
    content: str,
    sql_query: str = "",
    citations: list[dict] | None = None,
    auto_title: str | None = None,
):
    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO chat_messages (thread_id, role, content, sql_query, citations_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                role,
                content,
                sql_query or None,
                json.dumps(citations or []),
                now,
            ),
        )
        if auto_title:
            conn.execute(
                "UPDATE chat_threads SET title = ?, updated_at = ? WHERE id = ? AND title = 'New Conversation'",
                (auto_title[:48], now, thread_id),
            )
        else:
            conn.execute(
                "UPDATE chat_threads SET updated_at = ? WHERE id = ?", (now, thread_id)
            )
        conn.commit()
    finally:
        conn.close()


def _truncate_thread_from(
    db_path: str, thread_id: str, message_id: int, inclusive: bool = True
):
    op = ">=" if inclusive else ">"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"DELETE FROM chat_messages WHERE thread_id = ? AND id {op} ?",
            (thread_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def render_sidebar_threads(db_path: str, username: str):
    """Sidebar thread management: New, Search, Pin, Rename, Delete."""
    st.subheader("💬 Chat Threads")
    if st.button("➕ New Chat (Ctrl+K)", use_container_width=True, type="primary"):
        st.session_state["active_thread_id"] = _create_thread(db_path, username)
        st.rerun()

    search_q = st.text_input(
        "🔍 Search threads...",
        placeholder="Keyword or title",
        label_visibility="collapsed",
    )
    threads = _list_threads(db_path, username, search_q)

    if not threads and not search_q:
        tid = _create_thread(db_path, username)
        st.session_state["active_thread_id"] = tid
        threads = _list_threads(db_path, username)

    active_id = st.session_state.get("active_thread_id")
    if threads and (not active_id or not any(t["id"] == active_id for t in threads)):
        active_id = threads[0]["id"]
        st.session_state["active_thread_id"] = active_id

    for t in threads:
        is_active = t["id"] == active_id
        pin_icon = "📌 " if t["pinned"] else ""
        label = f"{pin_icon}{t['title']}"
        c1, c2 = st.columns([5, 1])
        with c1:
            if st.button(
                label,
                key=f"sel_th_{t['id']}",
                use_container_width=True,
                type="primary" if is_active else "secondary",
            ):
                st.session_state["active_thread_id"] = t["id"]
                st.rerun()
        with c2:
            with st.popover("⋮"):
                if st.button(
                    "📌 Unpin" if t["pinned"] else "📌 Pin",
                    key=f"pin_{t['id']}",
                    use_container_width=True,
                ):
                    conn = sqlite3.connect(db_path)
                    conn.execute(
                        "UPDATE chat_threads SET pinned = ? WHERE id = ?",
                        (0 if t["pinned"] else 1, t["id"]),
                    )
                    conn.commit()
                    conn.close()
                    st.rerun()
                new_name = st.text_input(
                    "Rename", value=t["title"], key=f"ren_in_{t['id']}"
                )
                if st.button(
                    "💾 Save Name", key=f"ren_btn_{t['id']}", use_container_width=True
                ):
                    if new_name.strip():
                        conn = sqlite3.connect(db_path)
                        conn.execute(
                            "UPDATE chat_threads SET title = ? WHERE id = ?",
                            (new_name.strip()[:60], t["id"]),
                        )
                        conn.commit()
                        conn.close()
                        st.rerun()
                if st.button(
                    "🗑️ Delete", key=f"del_{t['id']}", use_container_width=True
                ):
                    conn = sqlite3.connect(db_path)
                    conn.execute(
                        "DELETE FROM chat_messages WHERE thread_id = ?", (t["id"],)
                    )
                    conn.execute("DELETE FROM chat_threads WHERE id = ?", (t["id"],))
                    conn.commit()
                    conn.close()
                    if st.session_state.get("active_thread_id") == t["id"]:
                        st.session_state.pop("active_thread_id", None)
                    st.rerun()


# ------------------------------------------------------------------ Search, Files & Web Citations
def _extract_keywords(prompt: str) -> list[str]:
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
    if best_start > 0:
        nxt = text.find(" ", best_start)
        if 0 <= nxt < best_start + 30:
            best_start = nxt + 1
    return ("... " if best_start > 0 else "") + text[best_start : best_start + limit]


def _clean_snippet(text: str, limit: int, keywords: list[str] | None = None) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(.)\1{4,}", r"\1\1\1", text)
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
    images = []
    for h in chosen[:MAX_IMAGES]:
        data = library.get_item_image(db_path, h["id"])
        if data:
            images.append(dict(h, bytes=data))
    return images


def _search_local_documents(
    db_path: str, keywords: list[str], library_items: list[dict], shown_ids: set
) -> str:
    candidates = []
    conn = sqlite3.connect(db_path)
    try:
        if keywords:
            params = [f"%{kw}%" for kw in keywords]
            pdf_clause = " OR ".join(["content LIKE ?"] * len(keywords))
            web_clause = " OR ".join(["p.content LIKE ?"] * len(keywords))
            try:
                for fn, pg, content in conn.execute(
                    f"SELECT filename, page_number, content FROM government_services WHERE {pdf_clause} LIMIT 30;",
                    params,
                ):
                    candidates.append((f"", content, None))
            except sqlite3.Error:
                pass
            try:
                for title, url, content in conn.execute(
                    f"SELECT s.title, s.url, p.content FROM web_pages p JOIN web_sources s ON s.id = p.source_id WHERE {web_clause} LIMIT 40;",
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
        for label, content, _ in (shown + others)[:4]:
            cleaned = _clean_snippet(content, 650, keywords)
            if cleaned:
                snippets.append(f"{label}\n{cleaned}")
        return "\n\n".join(snippets)
    finally:
        conn.close()


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


def _live_web_search(query: str, max_results: int = 4) -> list[dict]:
    """Fetch live web search citations using DuckDuckGo."""
    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [
            {
                "title": r.get("title", "Web Source"),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in results
            if r.get("href")
        ]
    except Exception:
        return []


def _parse_uploaded_attachments(files, llm_cfg: dict, vision_model: str) -> str:
    """Extract text from PDFs, CSVs, TXT/MD, or describe images dropped into the chat bar."""
    if not files:
        return ""
    blocks = []
    for f in files:
        name = f.name
        lower = name.lower()
        data = f.getvalue()
        try:
            if lower.endswith(".pdf"):
                reader = pypdf.PdfReader(io.BytesIO(data))
                pages_txt = [p.extract_text() or "" for p in reader.pages[:15]]
                blocks.append(f"[Attached PDF: {name}]\n" + "\n".join(pages_txt)[:6000])
            elif lower.endswith(".csv"):
                df = pd.read_csv(io.BytesIO(data))
                blocks.append(
                    f"[Attached CSV: {name} | Shape: {df.shape}]\nColumns: {list(df.columns)}\nSample:\n{df.head(15).to_markdown(index=False)}"
                )
            elif lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                ok, _ = llm.vision_ready(llm_cfg, vision_model)
                if ok:
                    desc = llm.describe_image(
                        llm_cfg,
                        data,
                        "Transcribe all visible text, tables, and key visual details in this image.",
                        vision_model,
                        timeout=120,
                    )
                    blocks.append(f"[Attached Image Analysis: {name}]\n{desc}")
                else:
                    blocks.append(
                        f"[Attached Image: {name} (Configure a vision model in Library tab to inspect image pixels)]"
                    )
            else:
                blocks.append(
                    f"[Attached Document: {name}]\n"
                    + data.decode("utf-8", errors="replace")[:6000]
                )
        except Exception as e:
            blocks.append(f"[Attached File: {name} - Error reading: {e}]")
    return "\n\n".join(blocks)


def _export_pdf_bytes(title: str, markdown_text: str) -> bytes:
    """Generate clean downloadable PDF bytes using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    safe_title = title.encode("latin-1", "replace").decode("latin-1")
    pdf.multi_cell(0, 8, safe_title)
    pdf.ln(4)
    pdf.set_font("Helvetica", size=10)
    clean_body = (
        re.sub(r"[#*`]", "", markdown_text)
        .encode("latin-1", "replace")
        .decode("latin-1")
    )
    pdf.multi_cell(0, 6, clean_body)
    return bytes(pdf.output())


# ------------------------------------------------------------------ Multi-Language TTS & STT
def _render_message_tools(text: str, msg_index: int):
    """Render reliable Copy / Listen / Stop controls for an AI message."""
    safe_text = json.dumps(text, ensure_ascii=False)
    root_id = f"message_tools_{msg_index}"
    status_id = f"message_status_{msg_index}"
    tools_html = f"""
    <div id="{root_id}" style="display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin-top:2px; font-family:sans-serif;">
        <button type="button" data-action="copy" style="background:transparent; border:1px solid rgba(128,128,128,0.35); border-radius:999px; padding:4px 12px; cursor:pointer; font-size:12px; color:inherit;">📋 Copy</button>
        <button type="button" data-action="listen" style="background:transparent; border:1px solid rgba(128,128,128,0.35); border-radius:999px; padding:4px 12px; cursor:pointer; font-size:12px; color:inherit;">🔊 Listen</button>
        <button type="button" data-action="stop" style="background:transparent; border:1px solid rgba(128,128,128,0.35); border-radius:999px; padding:4px 12px; cursor:pointer; font-size:12px; color:inherit;">⏹️ Stop</button>
        <span id="{status_id}" role="status" aria-live="polite" style="font-size:11px; opacity:0.75;"></span>
    </div>
    <script>
    (() => {{
        const root = document.getElementById({json.dumps(root_id)});
        if (!root || root.dataset.bound === '1') return;
        root.dataset.bound = '1';
        const messageText = {safe_text};
        const status = root.querySelector('[role="status"]');
        const setStatus = (value) => {{ if (status) status.textContent = value; }};

        async function copyText() {{
            try {{
                if (navigator.clipboard && window.isSecureContext) {{
                    await navigator.clipboard.writeText(messageText);
                }} else {{
                    const field = document.createElement('textarea');
                    field.value = messageText;
                    field.setAttribute('readonly', '');
                    field.style.position = 'fixed';
                    field.style.left = '-9999px';
                    document.body.appendChild(field);
                    field.select();
                    const copied = document.execCommand('copy');
                    field.remove();
                    if (!copied) throw new Error('Clipboard permission unavailable');
                }}
                setStatus('Copied!');
            }} catch (error) {{
                setStatus('Copy failed — check browser permissions.');
            }}
        }}

        function detectLanguage(value) {{
            if (/[\u0D80-\u0DFF]/.test(value)) return 'si-LK';
            if (/[\u0B80-\u0BFF]/.test(value)) return 'ta-IN';
            if (/[\u0900-\u097F]/.test(value)) return 'hi-IN';
            if (/[\u0600-\u06FF]/.test(value)) return 'ar-SA';
            if (/[\u4E00-\u9FFF]/.test(value)) return 'zh-CN';
            if (/[\u3040-\u30FF]/.test(value)) return 'ja-JP';
            if (/[\uAC00-\uD7AF]/.test(value)) return 'ko-KR';
            if (/[\u0400-\u04FF]/.test(value)) return 'ru-RU';
            return 'en-US';
        }}

        function speakText() {{
            if (!('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) {{
                setStatus('Speech is not supported by this browser.');
                return;
            }}
            window.speechSynthesis.cancel();
            const language = detectLanguage(messageText);
            const utterance = new SpeechSynthesisUtterance(messageText);
            utterance.lang = language;
            utterance.rate = 1;
            utterance.onstart = () => setStatus('Speaking (' + language + ')…');
            utterance.onend = () => setStatus('');
            utterance.onerror = (event) => setStatus('Speech error: ' + event.error);
            const voices = window.speechSynthesis.getVoices();
            const voice = voices.find(item => item.lang && item.lang.toLowerCase().startsWith(language.split('-')[0].toLowerCase()));
            if (voice) utterance.voice = voice;
            window.speechSynthesis.speak(utterance);
            if (window.speechSynthesis.paused) window.speechSynthesis.resume();
        }}

        root.addEventListener('click', (event) => {{
            const button = event.target.closest('button[data-action]');
            if (!button || !root.contains(button)) return;
            const action = button.dataset.action;
            if (action === 'copy') copyText();
            if (action === 'listen') speakText();
            if (action === 'stop') {{
                if ('speechSynthesis' in window) window.speechSynthesis.cancel();
                setStatus('Speech stopped.');
            }}
        }});
    }})();
    </script>
    """
    st.html(tools_html, unsafe_allow_javascript=True)


def _render_stt_bar(disabled: bool):
    """Render browser-based multilingual voice dictation controls."""
    disabled_attr = "disabled" if disabled else ""
    stt_html = f"""
    <div id="sttControls" style="display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:6px 10px; background:rgba(128,128,128,0.08); border:1px solid rgba(128,128,128,0.2); border-radius:999px; font-family:Inter,sans-serif;">
        <span style="font-size:12px; font-weight:600;">🎙️ Dictate:</span>
        <select id="sttLang" {disabled_attr} style="padding:4px 6px; border-radius:5px; border:1px solid #888; font-size:12px;">
            <option value="en-US">English (US)</option>
            <option value="si-LK">Sinhala (සිංහල)</option>
            <option value="ta-IN">Tamil (தமிழ்)</option>
            <option value="hi-IN">Hindi (हिन्दी)</option>
            <option value="ar-SA">Arabic (العربية)</option>
            <option value="zh-CN">Chinese (中文)</option>
            <option value="ja-JP">Japanese (日本語)</option>
            <option value="ko-KR">Korean (한국어)</option>
            <option value="fr-FR">French</option>
            <option value="de-DE">German</option>
            <option value="es-ES">Spanish</option>
            <option value="ru-RU">Russian</option>
        </select>
        <button id="sttBtn" type="button" {disabled_attr} style="background:linear-gradient(120deg,#6366f1,#8b5cf6); color:white; border:none; padding:6px 14px; border-radius:999px; cursor:pointer; font-size:12px; font-weight:600;">🎤 Speak</button>
        <button id="sttStopBtn" type="button" {disabled_attr} style="background:transparent; border:1px solid #888; padding:5px 10px; border-radius:5px; cursor:pointer; font-size:12px;">⏹ Stop</button>
        <span id="sttStatus" role="status" aria-live="polite" style="font-size:12px; opacity:0.8;">Click Speak to dictate</span>
    </div>
    <script>
    (() => {{
        const root = document.getElementById('sttControls');
        if (!root || root.dataset.bound === '1') return;
        root.dataset.bound = '1';
        const startButton = root.querySelector('#sttBtn');
        const stopButton = root.querySelector('#sttStopBtn');
        const status = root.querySelector('#sttStatus');
        const languageSelect = root.querySelector('#sttLang');
        const setStatus = (value) => status.textContent = value;
        try {{
            const savedLanguage = sessionStorage.getItem('chat_stt_lang');
            if (savedLanguage) languageSelect.value = savedLanguage;
            languageSelect.addEventListener('change', () => sessionStorage.setItem('chat_stt_lang', languageSelect.value));
        }} catch (error) {{ /* Storage may be blocked; dictation still works. */ }}

        const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!Recognition) {{
            startButton.disabled = true;
            setStatus('Voice dictation is not supported. Use Chrome or Edge.');
            return;
        }}
        const recognition = new Recognition();
        recognition.continuous = false;
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        let listening = false;
        startButton.addEventListener('click', () => {{
            if (listening) return;
            recognition.lang = languageSelect.value;
            try {{
                recognition.start();
                listening = true;
                startButton.style.background = '#e69500';
                setStatus('Listening (' + recognition.lang + ')…');
            }} catch (error) {{
                setStatus('Could not start microphone. Check site microphone permission.');
            }}
        }});
        stopButton.addEventListener('click', () => {{
            if (listening) recognition.stop();
            setStatus('Dictation stopped.');
        }});
        recognition.onresult = (event) => {{
            const transcript = event.results && event.results[0] && event.results[0][0]
                ? event.results[0][0].transcript.trim() : '';
            if (!transcript) {{ setStatus('No speech detected. Please try again.'); return; }}
            const doc = (window.parent && window.parent.document) || document;
            const box = doc.querySelector('textarea[data-testid="stChatInputTextArea"]')
                || doc.querySelector('[data-testid="stChatInput"] textarea');
            if (!box) {{ setStatus('Chat box not found. Please type your message.'); return; }}
            const setter = Object.getOwnPropertyDescriptor((box.ownerDocument.defaultView || window).HTMLTextAreaElement.prototype, 'value').set;
            setter.call(box, transcript);
            box.dispatchEvent(new Event('input', {{ bubbles: true }}));
            setStatus('Sending: ' + transcript);
            setTimeout(() => {{
                const send = doc.querySelector('[data-testid="stChatInputSubmitButton"]')
                    || doc.querySelector('[data-testid="stChatInput"] button');
                if (send && !send.disabled) {{
                    send.click();
                }} else {{
                    box.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }}));
                }}
            }}, 150);
        }};
        recognition.onerror = (event) => {{
            setStatus('Microphone error: ' + event.error + '. Check browser permissions.');
        }};
        recognition.onend = () => {{
            listening = false;
            startButton.style.background = 'linear-gradient(120deg,#6366f1,#8b5cf6)';
        }};
    }})();
    </script>
    """
    st.html(stt_html, unsafe_allow_javascript=True)


# ------------------------------------------------------------------ Public Share View
def render_shared_thread_view(db_path: str, token: str):
    st.title("🔗 Shared AI Conversation")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT title, snapshot_json, created_by, created_at FROM shared_links WHERE token = ?",
            (token,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        st.error("This share link is invalid or has expired.")
        return

    title, snap_json, author, ts = row
    st.caption(
        f"Shared by **{author}** · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts))}"
    )
    st.subheader(title)
    messages = json.loads(snap_json)
    for idx, m in enumerate(messages):
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("sql_query"):
                st.code(m["sql_query"], language="sql")
            if m.get("citations"):
                cols = st.columns(min(len(m["citations"]), 4))
                for c, cit in zip(cols, m["citations"]):
                    c.markdown(f"**[{cit['title'][:38]}]({cit['url']})**")
            if m["role"] == "assistant":
                _render_message_tools(m["content"], idx)


# ------------------------------------------------------------------ Split-Screen Canvas / Artifacts
def _extract_latest_artifact(text: str) -> str:
    """Pull the largest code block (HTML/React/SQL/Python) or long markdown into the Canvas."""
    blocks = re.findall(r"```(?:\w+)?\n(.*?)```", text, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()


def _render_canvas_pane(thread_title: str):
    st.subheader("🎨 Split-Screen Canvas / Artifact")
    if "canvas_content" not in st.session_state:
        st.session_state["canvas_content"] = (
            "# Draft Document\n\nGenerate code or a document in chat, or edit directly here."
        )

    view_mode = st.radio(
        "Workspace Mode",
        ["✏️ Editor", "📖 Markdown Preview", "🌐 Live HTML / JS Preview"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if view_mode == "✏️ Editor":
        st.session_state["canvas_content"] = st.text_area(
            "Canvas Content",
            value=st.session_state["canvas_content"],
            height=460,
            label_visibility="collapsed",
        )
    elif view_mode == "📖 Markdown Preview":
        with st.container(border=True, height=460):
            st.markdown(st.session_state["canvas_content"])
    else:
        html_code = st.session_state["canvas_content"]
        if "<html" not in html_code.lower():
            html_code = f"<!DOCTYPE html><html><body style='font-family:sans-serif;padding:16px;'>{html_code}</body></html>"
        st.html(f"<div style=\"height:460px; overflow:auto;\">{html_code}</div>", unsafe_allow_javascript=True)

    # Export Canvas (.md, .pdf, .txt)
    c1, c2, c3 = st.columns(3)
    content = st.session_state["canvas_content"]
    c1.download_button(
        "⬇️ .MD",
        data=content,
        file_name="canvas_draft.md",
        mime="text/markdown",
        use_container_width=True,
    )
    c2.download_button(
        "⬇️ .TXT",
        data=content,
        file_name="canvas_draft.txt",
        mime="text/plain",
        use_container_width=True,
    )
    c3.download_button(
        "⬇️ .PDF",
        data=_export_pdf_bytes(thread_title, content),
        file_name="canvas_draft.pdf",
        mime="application/pdf",
        use_container_width=True,
    )


# ------------------------------------------------------------------ Main AI Chat Interface
def render_ai_chat_interface(
    db_path: str, llm_cfg: dict, user_role: str, username: str
):
    active_tid = st.session_state.get("active_thread_id")
    if not active_tid:
        active_tid = _create_thread(db_path, username)
        st.session_state["active_thread_id"] = active_tid

    # --- TOP PRODUCTIVITY HEADER BAR ---
    h1, h2, h3, h4, h5 = st.columns([2.2, 1.8, 1.3, 1.2, 1.2])
    with h1:
        custom_model = st.text_input(
            "🤖 Model",
            value=st.session_state.get("override_model", llm_cfg["model"]),
            help="Switch between fast models (e.g., llama3, gpt-4o-mini) and reasoning models (e.g., o3-mini, deepseek-r1).",
        )
        st.session_state["override_model"] = custom_model.strip()
        active_llm_cfg = dict(llm_cfg, model=custom_model.strip() or llm_cfg["model"])

    with h2:
        persona_name = st.selectbox("🎭 System Persona", list(PERSONAS.keys()))
    with h3:
        tone_label = st.selectbox("🎚️ Tone", list(TONE_TEMPS.keys()), index=1)
        temperature = TONE_TEMPS[tone_label]
    with h4:
        st.write("")
        use_web = st.toggle(
            "🌐 Web Search", value=False, help="Fetch live web citations"
        )
    with h5:
        st.write("")
        show_canvas = st.toggle(
            "🎨 Canvas", value=st.session_state.get("show_canvas", False)
        )
        st.session_state["show_canvas"] = show_canvas

    if persona_name == "⚙️ Custom System Persona":
        custom_sys = st.text_area(
            "Custom System Instructions",
            value=st.session_state.get(
                "custom_system_prompt", "You are a helpful AI assistant."
            ),
            height=68,
        )
        st.session_state["custom_system_prompt"] = custom_sys
        base_persona = custom_sys
    else:
        base_persona = PERSONAS[persona_name]

    # Export & Public Share Bar
    messages = _load_messages(db_path, active_tid)
    conn = sqlite3.connect(db_path)
    t_row = conn.execute(
        "SELECT title FROM chat_threads WHERE id = ?", (active_tid,)
    ).fetchone()
    conn.close()
    thread_title = t_row[0] if t_row else "Conversation"

    exp1, exp2, exp3, exp4, _ = st.columns([1.1, 1.1, 1.1, 1.4, 3.3])
    full_md = f"# {thread_title}\n\n" + "\n\n---\n\n".join(
        f"**{m['role'].upper()}**:\n{m['content']}" for m in messages
    )
    exp1.download_button(
        "📥 Export MD",
        data=full_md,
        file_name=f"{thread_title}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    exp2.download_button(
        "📥 Export TXT",
        data=full_md,
        file_name=f"{thread_title}.txt",
        mime="text/plain",
        use_container_width=True,
    )
    exp3.download_button(
        "📥 Export PDF",
        data=_export_pdf_bytes(thread_title, full_md),
        file_name=f"{thread_title}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )
    with exp4:
        with st.popover("🔗 Share Link", use_container_width=True):
            if st.button("Generate Public Read-Only Link", use_container_width=True):
                tok = uuid.uuid4().hex[:12]
                snap = [
                    {
                        "role": m["role"],
                        "content": m["content"],
                        "sql_query": m["sql_query"],
                        "citations": m["citations"],
                    }
                    for m in messages
                ]
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "INSERT INTO shared_links (token, thread_id, title, snapshot_json, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        tok,
                        active_tid,
                        thread_title,
                        json.dumps(snap),
                        username,
                        time.time(),
                    ),
                )
                conn.commit()
                conn.close()
                st.session_state["last_share_token"] = tok
            if st.session_state.get("last_share_token"):
                st.code(
                    f"?share={st.session_state['last_share_token']}", language="text"
                )
                st.caption(
                    "Append this query parameter to your app URL to share read-only."
                )

    problem = llm.config_problem(active_llm_cfg)
    if problem:
        st.error(problem)

    # --- SPLIT SCREEN LAYOUT (Chat Left | Canvas Right) ---
    if show_canvas:
        chat_col, canvas_col = st.columns([1.15, 1])
    else:
        chat_col, canvas_col = st.container(), None

    if canvas_col is not None:
        with canvas_col:
            _render_canvas_pane(thread_title)

    with chat_col:
        chat_container = st.container(height=470) if show_canvas else st.container()
        with chat_container:
            for idx, msg in enumerate(messages):
                with st.chat_message(msg["role"]):
                    # Inline prompt editor if user clicked Edit on this message
                    if st.session_state.get("editing_msg_id") == msg["id"]:
                        edited_txt = st.text_area(
                            "Edit prompt",
                            value=msg["content"],
                            key=f"ed_box_{msg['id']}",
                        )
                        ec1, ec2 = st.columns([1, 5])
                        if ec1.button(
                            "Send Edit", key=f"ed_save_{msg['id']}", type="primary"
                        ):
                            st.session_state.pop("editing_msg_id", None)
                            _truncate_thread_from(
                                db_path, active_tid, msg["id"], inclusive=True
                            )
                            st.session_state["pending_prompt"] = edited_txt
                            st.rerun()
                        if ec2.button("Cancel", key=f"ed_cancel_{msg['id']}"):
                            st.session_state.pop("editing_msg_id", None)
                            st.rerun()
                    else:
                        st.markdown(msg["content"])

                        if msg.get("sql_query"):
                            st.code(msg["sql_query"], language="sql")
                        if (
                            msg.get("dataframe") is not None
                            and not msg["dataframe"].empty
                        ):
                            st.dataframe(msg["dataframe"], width="stretch")

                        # Clickable Web Search Citation Cards
                        if msg.get("citations"):
                            st.caption("🌐 Web Sources:")
                            cit_cols = st.columns(min(len(msg["citations"]), 4))
                            for c_col, cit in zip(cit_cols, msg["citations"]):
                                with c_col:
                                    with st.container(border=True):
                                        st.markdown(
                                            f"**[{cit['title'][:40]}]({cit['url']})**"
                                        )
                                        st.caption(cit["snippet"][:85] + "...")

                        # Message Action Controls
                        if msg["role"] == "user" and idx > 0:
                            if st.button("✏️ Edit", key=f"edit_btn_{msg['id']}"):
                                st.session_state["editing_msg_id"] = msg["id"]
                                st.rerun()
                        elif msg["role"] == "assistant" and idx > 0:
                            ac1, ac2, ac3 = st.columns([1.2, 1.4, 4])
                            if ac1.button("🔄 Regenerate", key=f"regen_{msg['id']}"):
                                prev_user = next(
                                    (
                                        m
                                        for m in reversed(messages[:idx])
                                        if m["role"] == "user"
                                    ),
                                    None,
                                )
                                if prev_user:
                                    _truncate_thread_from(
                                        db_path, active_tid, msg["id"], inclusive=True
                                    )
                                    st.session_state["pending_regen_prompt"] = (
                                        prev_user["content"]
                                    )
                                    st.rerun()
                            if ac2.button("🎨 Send to Canvas", key=f"canv_{msg['id']}"):
                                st.session_state["canvas_content"] = (
                                    _extract_latest_artifact(msg["content"])
                                )
                                st.session_state["show_canvas"] = True
                                st.rerun()
                            _render_message_tools(msg["content"], idx)

        # --- PROMPT TEMPLATES, ATTACHMENTS, STT & CHAT INPUT ---
        with st.expander("📎 Attach Files / Images & Prompt Templates", expanded=False):
            t_col, f_col = st.columns([1, 1.3])
            with t_col:
                chosen_tpl = st.selectbox(
                    "Quick Prompt Templates", list(PROMPT_TEMPLATES.keys())
                )
                if chosen_tpl != "Select a quick template..." and st.button(
                    "Insert & Send Template"
                ):
                    st.session_state["pending_prompt"] = PROMPT_TEMPLATES[chosen_tpl]
                    st.rerun()
            with f_col:
                dropped_files = st.file_uploader(
                    "Drag & Drop PDFs, CSVs, TXT/MD, or Screenshots",
                    type=["pdf", "csv", "txt", "md", "png", "jpg", "jpeg", "webp"],
                    accept_multiple_files=True,
                    key="chat_drag_drop_files",
                )

        _render_stt_bar(disabled=False)

        # Capture STT voice input if submitted
        stt_prompt = st.query_params.get("stt_prompt", "").strip()
        stt_ts = st.query_params.get("stt_ts", "")
        if stt_prompt and stt_ts != st.session_state.get("last_stt_ts"):
            st.session_state["last_stt_ts"] = stt_ts
            st.query_params.pop("stt_prompt", None)
            st.query_params.pop("stt_ts", None)
            st.session_state["pending_prompt"] = stt_prompt
            st.rerun()

        typed_prompt = st.chat_input(
            "Message AI Workspace... (Enter to send, Shift+Enter for new line)"
        )

        # Determine active prompt (typed, edited, regenerated, or voice/template)
        user_prompt = None
        already_saved_user = False
        if st.session_state.get("pending_regen_prompt"):
            user_prompt = st.session_state.pop("pending_regen_prompt")
            already_saved_user = True
        elif st.session_state.get("pending_prompt"):
            user_prompt = st.session_state.pop("pending_prompt")
        elif typed_prompt:
            user_prompt = typed_prompt

        if user_prompt:
            vision_model = (
                st.session_state.get("vision_model") or llm_cfg["vision_model"]
            )
            attachment_context = _parse_uploaded_attachments(
                dropped_files, active_llm_cfg, vision_model
            )
            display_prompt = user_prompt
            if dropped_files:
                names = ", ".join(f.name for f in dropped_files)
                display_prompt = f"📎 *Attached: {names}*\n\n{user_prompt}"

            if not already_saved_user:
                _save_message(
                    db_path, active_tid, "user", display_prompt, auto_title=user_prompt
                )
                with chat_container:
                    with st.chat_message("user"):
                        st.markdown(display_prompt)

            # Gather context: Local Gov PDFs/Websites + Library Images + Optional Live Web Search
            keywords = _extract_keywords(user_prompt)
            wants_image = bool(IMAGE_INTENT.search(user_prompt))
            wants_picture = bool(PICTURE_WORDS.search(user_prompt))
            hits = library.search_items(db_path, keywords)
            images = _pick_images(db_path, keywords, hits, wants_image, wants_picture)
            shown_ids = {im["id"] for im in images}
            hit_ids = {h["id"] for h in hits}
            library_items = hits + [im for im in images if im["id"] not in hit_ids]
            local_context = _search_local_documents(
                db_path, keywords, library_items, shown_ids
            )
            db_schema = _get_schema(db_path)

            citations = _live_web_search(user_prompt) if use_web else []
            web_context = "\n".join(
                f"- [{c['title']}]({c['url']}): {c['snippet']}" for c in citations
            )

            system_prompt = (
                f"{base_persona}\n\n"
                "Support rich Markdown, formatted tables, syntax-highlighted code blocks, and LaTeX math ($...$ / $$...$$). "
                "Reply in the exact same language as the user's message.\n"
                f"Local Government & Library Excerpts:\n{local_context or 'None'}\n\n"
                f"Attached File Contents:\n{attachment_context or 'None'}\n\n"
                f"Live Web Search Results:\n{web_context or 'None'}\n\n"
                f"SQLite Database Schema:\n{db_schema or 'None'}\n"
                "If the user asks for database records/counts from the SQLite schema, include a valid read-only "
                "SELECT query inside a ```sql code block."
            )

            llm_messages = [{"role": "system", "content": system_prompt}]
            for m in messages[-8:]:
                llm_messages.append({"role": m["role"], "content": m["content"]})
            if not already_saved_user:
                llm_messages.append({"role": "user", "content": user_prompt})

            # --- REAL-TIME STREAMING RESPONSE WITH STOP CONTROL ---
            st.session_state["is_streaming"] = True
            st.session_state["stop_stream"] = False
            full_response = ""

            with chat_container:
                with st.chat_message("assistant"):
                    stop_placeholder = st.empty()
                    if stop_placeholder.button(
                        "⏹️ Stop Generating", key="stop_gen_btn"
                    ):
                        st.session_state["stop_stream"] = True

                    def token_generator():
                        nonlocal full_response
                        for chunk in llm.stream_generate(
                            active_llm_cfg, llm_messages, temperature=temperature
                        ):
                            if st.session_state.get("stop_stream"):
                                break
                            full_response += chunk
                            yield chunk

                    try:
                        st.write_stream(token_generator())
                    except Exception as e:
                        full_response += f"\n\n❌ Stream interrupted: {e}"
                        st.error(full_response)
                    finally:
                        stop_placeholder.empty()
                        st.session_state["is_streaming"] = False

                    for im in images:
                        st.image(im["bytes"], caption=im["title"], width=500)

            # Extract and safely execute any generated SQL query
            sql_match = re.search(
                r"```sql\s+(SELECT\b.*?)```", full_response, re.IGNORECASE | re.DOTALL
            )
            sql_query = sql_match.group(1).strip() if sql_match else ""
            if sql_query and (
                not is_safe_select(sql_query) or INTERNAL_SQL.search(sql_query)
            ):
                log_event(db_path, username, "BLOCKED_SQL", sql_query[:500])
                sql_query = ""
            elif sql_query:
                log_event(db_path, username, "AI_SQL_QUERY", sql_query[:500])

            _save_message(
                db_path,
                active_tid,
                "assistant",
                full_response or "(Generation stopped)",
                sql_query=sql_query,
                citations=citations,
            )

            # Auto-populate Canvas if response contains substantial code or document structure
            if "```" in full_response and len(full_response) > 350:
                st.session_state["canvas_content"] = _extract_latest_artifact(
                    full_response
                )

            st.session_state["last_active"] = time.time()
            st.rerun()
