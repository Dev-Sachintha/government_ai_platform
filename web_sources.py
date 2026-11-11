"""Website sources: fetch a web page (or PDF), extract its text, store it in
SQLite in small chunks so the chat assistant can search it.

No Streamlit calls in this module, so it is safe to run in a background thread.
"""
import io
import ipaddress
import re
import socket
import sqlite3
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from db_utils import init_db, log_event

MAX_BYTES = 5 * 1024 * 1024      # refuse pages bigger than 5 MB
MAX_TEXT_CHARS = 300_000         # keep at most this much text per page
MAX_CHUNKS = 500
CHUNK_CHARS = 600
MAX_REDIRECTS = 3
REQUEST_TIMEOUT = 20
HEADERS = {"User-Agent": "GovAIPlatform/1.0 (+internal knowledge-base fetcher)"}

# Safety: only fetch public internet addresses (blocks localhost / LAN / cloud
# metadata). Tests may set this to True.
ALLOW_PRIVATE_HOSTS = False


# ---------------------------------------------------------------- validation
def validate_url(raw: str) -> tuple[bool, str]:
    """Return (True, normalized_url) or (False, error_message)."""
    url = (raw or "").strip()
    if not url:
        return False, "Please enter a website link."
    if len(url) > 2000:
        return False, "That link is too long."
    if re.match(r"^(javascript|data|vbscript|mailto|file|ftp|about|blob):", url, re.IGNORECASE):
        return False, "Only http:// and https:// links are allowed."
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "Only http:// and https:// links are allowed."
    if not parsed.hostname:
        return False, "That does not look like a valid link."
    if parsed.username or parsed.password:
        return False, "Links containing a username or password are not allowed."
    return True, parsed._replace(fragment="").geturl()


def _check_public_host(url: str) -> None:
    """Raise ValueError if the host resolves to a non-public address."""
    if ALLOW_PRIVATE_HOSTS:
        return
    host = urlparse(url).hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Cannot find the website '{host}'. Check the link.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise ValueError(
                "For security, links to local or private network addresses are not allowed."
            )


# ------------------------------------------------------------- text extraction
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "nav", "footer", "head"}
    BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "main", "header",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP and tag != "head":
            self._skip_depth += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and tag != "head" and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            self.parts.append(data)


def _clean_text(text: str) -> str:
    lines, prev = [], None
    for line in text.splitlines():
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line != prev:  # drop blanks and consecutive duplicates (menus)
            lines.append(line)
        prev = line
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    for enc in ([m.group(1)] if m else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def fetch_url(url: str) -> tuple[str, str]:
    """Download `url` and return (title, text). Raises on any problem."""
    current = url
    resp = None
    for _ in range(MAX_REDIRECTS + 1):
        _check_public_host(current)
        resp = requests.get(
            current, headers=HEADERS, timeout=REQUEST_TIMEOUT,
            stream=True, allow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError("The website sent an invalid redirect.")
            current = urljoin(current, location)
            ok, checked = validate_url(current)
            if not ok:
                raise ValueError(f"Redirected to a blocked link: {checked}")
            current = checked
            continue
        break
    else:
        raise ValueError("The website redirected too many times.")

    resp.raise_for_status()
    body, total = [], 0
    for part in resp.iter_content(65536):
        total += len(part)
        if total > MAX_BYTES:
            raise ValueError("The page is too large (limit is 5 MB).")
        body.append(part)
    raw = b"".join(body)
    ctype = resp.headers.get("Content-Type", "").lower()
    path_name = urlparse(current).path.rsplit("/", 1)[-1] or urlparse(current).hostname

    if "pdf" in ctype or raw[:5] == b"%PDF-" or current.lower().split("?")[0].endswith(".pdf"):
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(raw))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        title = path_name
    elif "html" in ctype or raw.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        parser = _TextExtractor()
        parser.feed(_decode(raw, ctype))
        text = "".join(parser.parts)
        title = re.sub(r"\s+", " ", parser.title).strip() or path_name
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        text = _decode(raw, ctype)
        title = path_name
    else:
        raise ValueError(f"Unsupported content type: {ctype or 'unknown'}")

    text = _clean_text(text)
    if len(text) < 50:
        raise ValueError(
            "No readable text found. The page may need JavaScript to show its content."
        )
    return title[:200], text


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split text into chunks of roughly `size` characters, on line/word boundaries."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > size:  # very long line: cut at a space if possible
            cut = line.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut].strip())
            line = line[cut:].strip()
        if current and len(current) + len(line) + 1 > size:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()][:MAX_CHUNKS]


# ------------------------------------------------------------------- database
def add_or_refresh_source(db_path: str, url: str, username: str) -> tuple[bool, str]:
    """Fetch `url`, (re)index it. Returns (ok, message). Never raises."""
    try:
        ok, norm = validate_url(url)
        if not ok:
            return False, norm
        title, text = fetch_url(norm)
        chunks = chunk_text(text)
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            row = cur.execute("SELECT id FROM web_sources WHERE url = ?;", (norm,)).fetchone()
            if row:
                sid = row[0]
                cur.execute(
                    "UPDATE web_sources SET title=?, last_fetched=CURRENT_TIMESTAMP, "
                    "chunk_count=?, status='ok' WHERE id=?;",
                    (title, len(chunks), sid),
                )
                cur.execute("DELETE FROM web_pages WHERE source_id = ?;", (sid,))
            else:
                cur.execute(
                    "INSERT INTO web_sources (url, title, added_by, last_fetched, chunk_count, status) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, 'ok');",
                    (norm, title, username, len(chunks)),
                )
                sid = cur.lastrowid
            cur.executemany(
                "INSERT INTO web_pages (source_id, chunk_index, content) VALUES (?, ?, ?);",
                [(sid, i, c) for i, c in enumerate(chunks)],
            )
            conn.commit()
        finally:
            conn.close()
        log_event(db_path, username, "WEB_SOURCE_ADD", norm)
        return True, f"Added “{title}”: {len(chunks)} text sections indexed. You can now ask the chat about it."
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        return False, f"The website returned an error (HTTP {code})."
    except requests.exceptions.Timeout:
        return False, "The website took too long to respond."
    except requests.exceptions.RequestException as e:
        return False, f"Could not reach the website: {e}"
    except Exception as e:
        return False, f"Could not add the website: {e}"


def list_sources(db_path: str) -> list[dict]:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, url, title, added_by, added_at, last_fetched, chunk_count, status "
            "FROM web_sources ORDER BY id DESC;"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_source(db_path: str, source_id: int, username: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT url FROM web_sources WHERE id = ?;", (source_id,)).fetchone()
        conn.execute("DELETE FROM web_pages WHERE source_id = ?;", (source_id,))
        conn.execute("DELETE FROM web_sources WHERE id = ?;", (source_id,))
        conn.commit()
    finally:
        conn.close()
    if row:
        log_event(db_path, username, "WEB_SOURCE_DELETE", row[0])
