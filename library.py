"""Image & PDF library.

Admins add images and PDF files. Every image (and every PDF page) is stored in
SQLite as a JPEG together with searchable text:
  * the admin's title / description / keywords,
  * text extracted from PDF pages,
  * an optional AI description made by a local vision model (Ollama).
The chat assistant searches this text and shows the matching pictures.

No Streamlit calls here, so everything can run in a background thread.
"""
import hashlib
import io
import os
import re
import sqlite3

import llm
from PIL import Image, ImageOps

from db_utils import init_db, log_event

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 150
MAX_SIDE = 1600            # stored images are scaled down to this longest side
VISION_SIDE = 1024         # images are scaled down further before the vision model
PDF_RENDER_SCALE = 1.4
MIN_TEXT_CHARS = 20        # a PDF page with less text than this counts as "scanned"
MAX_AI_ITEMS_PER_RUN = 30
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


# ------------------------------------------------------------------ images
def _to_jpeg(img: Image.Image, max_side: int = MAX_SIDE, quality: int = 85) -> bytes:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    else:
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _render_pdf(raw: bytes) -> tuple[list[dict], bool]:
    """Return ([{page, text, jpeg}], truncated). jpeg is None without pypdfium2."""
    pages: list[dict] = []
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None

    if pdfium is not None:
        pdf = pdfium.PdfDocument(raw)
        try:
            total = len(pdf)
            for i in range(min(total, MAX_PDF_PAGES)):
                page = pdf[i]
                try:
                    text = ""
                    try:
                        textpage = page.get_textpage()
                        text = textpage.get_text_range() or ""
                        textpage.close()
                    except Exception:
                        pass
                    jpeg = _to_jpeg(page.render(scale=PDF_RENDER_SCALE).to_pil())
                finally:
                    page.close()
                pages.append({"page": i + 1, "text": _clean(text), "jpeg": jpeg})
            return pages, total > MAX_PDF_PAGES
        finally:
            pdf.close()

    import pypdf  # text only fallback

    reader = pypdf.PdfReader(io.BytesIO(raw))
    total = len(reader.pages)
    for i in range(min(total, MAX_PDF_PAGES)):
        pages.append({"page": i + 1, "text": _clean(reader.pages[i].extract_text() or ""), "jpeg": None})
    return pages, total > MAX_PDF_PAGES


def _clean(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:20000]


# ------------------------------------------------------------------ adding
def add_files(db_path: str, uploads: list[tuple[str, bytes]], description: str, username: str):
    """Store uploaded files. Returns (new_item_ids, messages[(ok, text)])."""
    init_db(db_path)
    new_ids: list[int] = []
    messages: list[tuple[bool, str]] = []
    description = (description or "").strip()[:2000]

    for raw_name, raw in uploads:
        name = os.path.basename(raw_name or "file")[:120] or "file"
        ext = os.path.splitext(name)[1].lower()
        title = os.path.splitext(name)[0][:150] or name
        try:
            is_pdf = ext == ".pdf"
            if not is_pdf and ext not in IMAGE_EXTS:
                messages.append((False, f"{name}: unsupported file type (use PNG, JPG, WEBP, GIF or PDF)."))
                continue
            limit = MAX_PDF_BYTES if is_pdf else MAX_IMAGE_BYTES
            if len(raw) > limit:
                messages.append((False, f"{name}: file is too large (limit {limit // (1024 * 1024)} MB)."))
                continue

            digest = hashlib.sha256(raw).hexdigest()
            conn = sqlite3.connect(db_path)
            try:
                if conn.execute("SELECT 1 FROM library_files WHERE sha256 = ?;", (digest,)).fetchone():
                    messages.append((False, f"{name}: already in the library."))
                    continue
            finally:
                conn.close()

            truncated = False
            if is_pdf:
                if raw[:5] != b"%PDF-":
                    messages.append((False, f"{name}: this is not a valid PDF file."))
                    continue
                pages, truncated = _render_pdf(raw)
                if not pages:
                    messages.append((False, f"{name}: the PDF has no pages."))
                    continue
                kind = "pdf"
                items = [(p["page"], p["text"], "image/jpeg" if p["jpeg"] else None, p["jpeg"]) for p in pages]
            else:
                with Image.open(io.BytesIO(raw)) as probe:
                    probe.verify()
                with Image.open(io.BytesIO(raw)) as img:
                    jpeg = _to_jpeg(img)
                kind = "image"
                items = [(1, "", "image/jpeg", jpeg)]

            conn = sqlite3.connect(db_path)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO library_files (filename, title, description, kind, sha256, page_count, added_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (name, title, description, kind, digest, len(items), username),
                )
                file_id = cur.lastrowid
                for page_no, text, mime, data in items:
                    cur.execute(
                        "INSERT INTO library_items (file_id, page_number, text_content, mime, data) "
                        "VALUES (?, ?, ?, ?, ?);",
                        (file_id, page_no, text, mime, data),
                    )
                    new_ids.append(cur.lastrowid)
                conn.commit()
            finally:
                conn.close()

            log_event(db_path, username, "LIBRARY_ADD", name)
            if kind == "pdf":
                scanned = sum(1 for _, t, _, _ in items if len(t) < MIN_TEXT_CHARS)
                note = f" (first {MAX_PDF_PAGES} pages only)" if truncated else ""
                extra = f", {scanned} scanned page(s) without text" if scanned else ""
                no_img = "" if items[0][3] else " Page pictures need the 'pypdfium2' package."
                messages.append((True, f"{name}: {len(items)} page(s) added{note}{extra}.{no_img}"))
            else:
                messages.append((True, f"{name}: image added."))
        except Image.DecompressionBombError:
            messages.append((False, f"{name}: the image is too large to process."))
        except Exception as e:
            messages.append((False, f"{name}: could not be added ({type(e).__name__}: {e})."))
    return new_ids, messages


# ------------------------------------------------------------ AI description
VISION_PROMPT = (
    "You are indexing an image for a search system. In 2-3 sentences describe what the image shows. "
    "Then write 'Text:' followed by any clearly readable text in the image, copied exactly "
    "(at most 600 characters, or 'none'). Then write 'Keywords:' followed by 8 comma-separated keywords."
)


def describe_items(db_path, item_ids, llm_cfg, vision_model, username) -> tuple[bool, str]:
    """Describe images / scanned pages with a vision model. Never raises."""
    try:
        ids = list(item_ids)[:MAX_AI_ITEMS_PER_RUN]
        if not ids:
            return True, "Nothing needs an AI description."
        ready, problem = llm.vision_ready(llm_cfg, vision_model)
        if not ready:
            return False, problem

        done = failed = 0
        last_error = ""
        for item_id in ids:
            data = get_item_image(db_path, item_id)
            if not data:
                failed += 1
                continue
            try:
                with Image.open(io.BytesIO(data)) as img:
                    small = _to_jpeg(img, max_side=VISION_SIDE, quality=80)
                text = _clean(llm.describe_image(llm_cfg, small, VISION_PROMPT, vision_model))[:2500]
                if not text:
                    failed += 1
                    continue
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("UPDATE library_items SET ai_text = ? WHERE id = ?;", (text, item_id))
                    conn.commit()
                finally:
                    conn.close()
                done += 1
            except llm.LLMError as e:
                failed += 1
                last_error = str(e)
                if "rejected the API key" in last_error or "Cannot " in last_error or "rate limit" in last_error:
                    break  # no point trying the remaining items
        log_event(db_path, username, "LIBRARY_AI_DESCRIBE", f"{done} ok, {failed} failed")
        if done == 0:
            return False, last_error or "The AI could not describe the selected item(s). Try a different vision model."
        msg = f"AI described {done} item(s)."
        if failed:
            msg += f" {failed} could not be described" + (f" ({last_error})" if last_error else "") + "."
        return True, msg
    except Exception as e:
        return False, f"AI description failed: {e}"


def process_uploads(db_path, uploads, description, username, auto_describe, llm_cfg, vision_model):
    """Background job: store files, optionally describe them. Returns (ok, message)."""
    try:
        new_ids, messages = add_files(db_path, uploads, description, username)
        lines = [("✅ " if ok else "⚠️ ") + text for ok, text in messages]
        ok_any = any(ok for ok, _ in messages)
        if auto_describe and new_ids:
            pending = pending_description_ids(db_path, None, ids=new_ids)
            if pending:
                _, ai_msg = describe_items(db_path, pending, llm_cfg, vision_model, username)
                lines.append("🤖 " + ai_msg)
        return ok_any, "\n\n".join(lines)
    except Exception as e:
        return False, f"Upload failed: {e}"


# ----------------------------------------------------------------- queries
def list_files(db_path: str) -> list[dict]:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT f.id, f.filename, f.title, f.description, f.kind, f.page_count,
                   f.added_by, f.added_at, COUNT(i.id) AS items,
                   COALESCE(SUM(CASE WHEN length(trim(i.text_content)) >= {MIN_TEXT_CHARS} THEN 1 ELSE 0 END), 0) AS text_pages,
                   COALESCE(SUM(CASE WHEN length(trim(i.ai_text)) > 0 THEN 1 ELSE 0 END), 0) AS ai_pages
            FROM library_files f LEFT JOIN library_items i ON i.file_id = f.id
            GROUP BY f.id ORDER BY f.id DESC;
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def pending_description_ids(db_path, file_id, ids=None, limit=MAX_AI_ITEMS_PER_RUN) -> list[int]:
    """Items that have a picture but no searchable text yet (images, scanned pages)."""
    conn = sqlite3.connect(db_path)
    try:
        sql = (
            "SELECT i.id FROM library_items i WHERE i.data IS NOT NULL "
            f"AND length(trim(i.ai_text)) = 0 AND length(trim(i.text_content)) < {MIN_TEXT_CHARS} "
        )
        params: list = []
        if file_id is not None:
            sql += "AND i.file_id = ? "
            params.append(file_id)
        if ids is not None:
            sql += "AND i.id IN (%s) " % ",".join("?" * len(ids))
            params += list(ids)
        sql += "ORDER BY i.page_number LIMIT ?;"
        params.append(limit)
        return [r[0] for r in conn.execute(sql, params)]
    finally:
        conn.close()


def get_item_image(db_path: str, item_id: int) -> bytes | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT data FROM library_items WHERE id = ?;", (item_id,)).fetchone()
        return bytes(row[0]) if row and row[0] else None
    finally:
        conn.close()


def get_item_id(db_path: str, file_id: int, page_number: int) -> int | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM library_items WHERE file_id = ? AND page_number = ?;",
            (file_id, page_number),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def update_file(db_path, file_id, title, description, username) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE library_files SET title = ?, description = ? WHERE id = ?;",
            ((title or "").strip()[:150] or "Untitled", (description or "").strip()[:2000], file_id),
        )
        conn.commit()
    finally:
        conn.close()
    log_event(db_path, username, "LIBRARY_EDIT", str(file_id))


def delete_file(db_path, file_id, username) -> None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT filename FROM library_files WHERE id = ?;", (file_id,)).fetchone()
        conn.execute("DELETE FROM library_items WHERE file_id = ?;", (file_id,))
        conn.execute("DELETE FROM library_files WHERE id = ?;", (file_id,))
        conn.commit()
    finally:
        conn.close()
    if row:
        log_event(db_path, username, "LIBRARY_DELETE", row[0])


def search_items(db_path: str, keywords: list[str], limit: int = 20) -> list[dict]:
    """Find library items matching any keyword; best first. No picture data returned."""
    if not keywords:
        return []
    cond = " OR ".join(
        ["f.title LIKE ? OR f.description LIKE ? OR i.text_content LIKE ? OR i.ai_text LIKE ?"] * len(keywords)
    )
    params = [f"%{kw}%" for kw in keywords for _ in range(4)]
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"""
                SELECT i.id, f.title, f.filename, f.kind, i.page_number AS page,
                       f.description, i.text_content, i.ai_text, (i.data IS NOT NULL) AS has_data
                FROM library_items i JOIN library_files f ON f.id = i.file_id
                WHERE {cond} LIMIT 300;
                """,
                params,
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()

    hits = []
    for r in rows:
        d = dict(r)
        head = f"{d['title']} {d['description']}".lower()
        body = f"{d['text_content']} {d['ai_text']}".lower()
        d["head"] = sum(kw in head for kw in keywords)                       # title / description
        d["body"] = sum((kw in body) and (kw not in head) for kw in keywords)  # PDF text / AI text
        d["score"] = 2 * d["head"] + d["body"]
        if d["score"] > 0:
            hits.append(d)
    hits.sort(key=lambda d: (-d["score"], d["id"]))
    return hits[:limit]


def latest_images(db_path: str, n: int = 3) -> list[dict]:
    """Most recently added pictures (used for 'show me an image' with no keywords)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT i.id, f.title, f.filename, f.kind, i.page_number AS page,
                   f.description, i.text_content, i.ai_text, 1 AS has_data
            FROM library_items i JOIN library_files f ON f.id = i.file_id
            WHERE f.kind = 'image' AND i.data IS NOT NULL ORDER BY i.id DESC LIMIT ?;
            """,
            (n,),
        ).fetchall()
        return [dict(r, score=0, head=0, body=0) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
