"""Scan: show a real object to the camera -> the AI vision model identifies it -> information comes up.

Admins can add items to a catalogue ONE BY ONE (name, aliases, information, link, reference photo).
If the scanned object matches a catalogue item, that item's information is shown (it is authoritative);
otherwise the AI's own description is shown and clearly marked as an estimate.
No Streamlit calls here, so it can run in a background thread.
"""
import io
import json
import re
import sqlite3

import llm
from db_utils import log_event

MAX_PHOTO_BYTES = 8 * 1024 * 1024


def init_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                keywords TEXT DEFAULT '',
                info TEXT NOT NULL,
                link TEXT DEFAULT '',
                photo BLOB,
                added_by TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );"""
        )
        conn.commit()
    finally:
        conn.close()


def _to_jpeg(data: bytes, max_side: int, quality: int) -> bytes:
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _clean_link(link: str) -> str:
    link = (link or "").strip()
    if not link:
        return ""
    if not re.match(r"^https?://[^\s]+$", link, re.I):
        raise ValueError("The link must start with http:// or https://")
    return link[:500]


def _validate(name: str, info: str):
    name, info = (name or "").strip(), (info or "").strip()
    if not name:
        raise ValueError("Please enter a name.")
    if len(name) > 80:
        raise ValueError("The name is too long (max 80 characters).")
    if not info:
        raise ValueError("Please enter the information to show when this item is scanned.")
    if len(info) > 4000:
        raise ValueError("The information is too long (max 4000 characters).")
    return name, info


def _photo_blob(photo):
    if not photo:
        return None
    if len(photo) > MAX_PHOTO_BYTES:
        raise ValueError("The photo is larger than 8 MB.")
    try:
        return _to_jpeg(photo, 640, 80)
    except Exception:
        raise ValueError("That file is not a valid image.")


def add_item(db_path, name, keywords, info, link, photo, username):
    """Add ONE item. Returns (ok, message)."""
    try:
        name, info = _validate(name, info)
        link = _clean_link(link)
        blob = _photo_blob(photo)
        init_tables(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO scan_items (name, keywords, info, link, photo, added_by) VALUES (?,?,?,?,?,?);",
                (name, (keywords or "").strip()[:300], info, link, blob, username),
            )
            conn.commit()
        finally:
            conn.close()
        log_event(db_path, username, "SCAN_ITEM_ADD", name)
        return True, f"Added “{name}” to the scan catalogue."
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Could not add the item: {e}"


def update_item(db_path, item_id, name, keywords, info, link, username):
    try:
        name, info = _validate(name, info)
        link = _clean_link(link)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE scan_items SET name=?, keywords=?, info=?, link=? WHERE id=?;",
                (name, (keywords or "").strip()[:300], info, link, int(item_id)),
            )
            conn.commit()
        finally:
            conn.close()
        log_event(db_path, username, "SCAN_ITEM_EDIT", str(item_id))
        return True, "Saved."
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Could not save: {e}"


def delete_item(db_path, item_id, username):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM scan_items WHERE id=?;", (int(item_id),))
        conn.commit()
    finally:
        conn.close()
    log_event(db_path, username, "SCAN_ITEM_DELETE", str(item_id))


def list_items(db_path, with_photo=False):
    init_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = "id, name, keywords, info, link, added_by, added_at" + (", photo" if with_photo else ", (photo IS NOT NULL) AS has_photo")
        return [dict(r) for r in conn.execute(f"SELECT {cols} FROM scan_items ORDER BY name COLLATE NOCASE;")]
    finally:
        conn.close()


def get_photo(db_path, item_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT photo FROM scan_items WHERE id=?;", (int(item_id),)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


PROMPT = """You are the object-recognition assistant of a guide app. Look at the photo and identify the MAIN object the camera is pointed at.

Known items in our catalogue (id: name - aliases):
{catalogue}

Reply with ONLY one JSON object and nothing else:
{{"match_id": <id of the catalogue item that is the SAME kind of object, or null>,
  "name": "<short common name of the object>",
  "category": "<one or two words>",
  "confidence": "high" | "medium" | "low",
  "description": "<2-3 sentences: what it is and what it is used for>",
  "facts": ["<useful fact>", "<useful fact>", "<useful fact>"]}}
Set match_id only if you are fairly sure it is the same kind of object as that catalogue item.
If there is no clear object in the photo, use "Nothing recognised" as the name and null as match_id."""


def _parse_ai(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {"name": "Scanned object", "category": "", "confidence": "low", "description": (text or "").strip()[:800], "facts": []}


def identify(db_path, cfg, vision_model, jpeg_bytes):
    """Run one scan. Returns (ok, json_string) - safe to run in a background thread. Never raises."""
    try:
        if len(jpeg_bytes) > MAX_PHOTO_BYTES:
            return False, "The photo is too large."
        try:
            small = _to_jpeg(jpeg_bytes, 1024, 85)
        except Exception:
            return False, "That is not a valid image."
        vision_model = llm.resolve_vision_model(cfg, vision_model)
        ready, problem = llm.vision_ready(cfg, vision_model)
        if not ready:
            return False, problem + " (Scanning needs an AI model that can read pictures.)"
        items = list_items(db_path)
        catalogue = "\n".join(
            f"{it['id']}: {it['name']}" + (f" - {it['keywords']}" if it["keywords"] else "") for it in items
        ) or "(the catalogue is empty)"
        prompt = PROMPT.format(catalogue=catalogue)
        try:
            raw = llm.describe_image(cfg, small, prompt, vision_model, timeout=120)
        except llm.LLMError as first:
            if first.kind not in ("no_model", "no_vision"):
                raise
            raw, last = None, first
            # that model is retired / text-only: try the picture-capable models this account really has
            for alt in llm.vision_candidates(cfg, exclude=(vision_model,))[:3]:
                try:
                    raw = llm.describe_image(cfg, small, prompt, alt, timeout=120)
                    vision_model = alt
                    break
                except llm.LLMError as e2:
                    last = e2
                    if e2.kind not in ("no_model", "no_vision"):
                        raise
            if raw is None:
                raise last
        llm.remember_vision_model(cfg, vision_model)
        ai = _parse_ai(raw)
        facts = ai.get("facts") if isinstance(ai.get("facts"), list) else []
        out = {
            "ai": {
                "name": str(ai.get("name") or "Scanned object")[:120],
                "category": str(ai.get("category") or "")[:60],
                "confidence": str(ai.get("confidence") or "")[:10].lower(),
                "description": str(ai.get("description") or "")[:1500],
                "facts": [str(f)[:300] for f in facts[:5]],
            },
            "item": None,
        }
        mid = ai.get("match_id")
        try:
            mid = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            mid = None
        for it in items:
            if mid is not None and it["id"] == mid:
                out["item"] = {k: it[k] for k in ("id", "name", "info", "link", "has_photo")}
                break
        out["model"] = vision_model
        log_event(db_path, "scan", "SCAN", out["ai"]["name"])
        return True, json.dumps(out)
    except llm.LLMError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Scan failed: {e}"
