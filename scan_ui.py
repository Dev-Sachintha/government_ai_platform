"""Streamlit UI for the Scan feature (tab + admin catalogue manager) and its background-job helpers."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import streamlit as st

import llm
import scan

_executor = ThreadPoolExecutor(max_workers=2)


# ------------------------------------------------------------------ background jobs (per browser session)
def start(name, fn, *args):
    st.session_state[f"scanjob:{name}"] = {"future": _executor.submit(fn, *args), "started": time.time()}


def running(name) -> bool:
    return st.session_state.get(f"scanjob:{name}") is not None


def poll(name):
    """Returns ('idle'|'running'|'done', (ok, message) or None). A finished job is consumed once."""
    job = st.session_state.get(f"scanjob:{name}")
    if job is None:
        return "idle", None
    if not job["future"].done():
        return "running", int(time.time() - job["started"])
    st.session_state[f"scanjob:{name}"] = None
    try:
        return "done", job["future"].result()
    except Exception as e:  # identify() never raises, but be safe
        return "done", (False, f"Scan failed: {e}")


# ------------------------------------------------------------------ result card
def _prefill(ai):
    facts = "\n".join(f"- {f}" for f in ai.get("facts", []))
    st.session_state["scan_add_name"] = ai.get("name", "")
    st.session_state["scan_add_info"] = (ai.get("description", "") + ("\n\n" + facts if facts else "")).strip()
    st.session_state["scan_add_photo_last"] = True
    st.session_state["scan_open_admin"] = True


def show_scan_result(msg: str, db_path: str, is_admin: bool):
    try:
        d = json.loads(msg)
    except Exception:
        st.error("The scan result could not be read.")
        return
    ai, item = d.get("ai", {}), d.get("item")
    conf = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}.get(ai.get("confidence", ""), ai.get("confidence", ""))
    with st.container(border=True):
        st.markdown(f"### 🔎 {ai.get('name', 'Scanned object')}")
        st.caption(" · ".join(x for x in [ai.get("category", ""), f"AI confidence: {conf}" if conf else ""] if x))
        if item:
            st.success("✅ Found in the catalogue. Official information:")
            c1, c2 = st.columns([2, 1]) if item.get("has_photo") else (st.container(), None)
            with c1:
                st.markdown(item["info"])
                if item.get("link"):
                    st.link_button("🔗 More information", item["link"])
            if c2 is not None:
                photo = scan.get_photo(db_path, item["id"])
                if photo:
                    with c2:
                        st.image(photo, caption=item["name"])
            with st.expander("What the AI saw"):
                st.write(ai.get("description", ""))
                for f in ai.get("facts", []):
                    st.markdown(f"- {f}")
        else:
            st.info("This item is not in the catalogue yet. The text below is the AI's estimate and may be wrong.")
            st.write(ai.get("description", ""))
            for f in ai.get("facts", []):
                st.markdown(f"- {f}")
            if is_admin and ai.get("name") and ai["name"] != "Nothing recognised":
                st.button("➕ Add this to the catalogue", key="scan_prefill_btn", on_click=_prefill, args=(ai,))


# ------------------------------------------------------------------ admin catalogue manager
def render_item_manager(db_path, username, on_change=None):
    items = scan.list_items(db_path)
    flash = st.session_state.pop("scan_flash", None)
    expanded = bool(st.session_state.pop("scan_open_admin", False)) or flash is not None
    with st.expander(f"🛠️ Admin: scan catalogue ({len(items)} items)", expanded=expanded):
        if flash:
            (st.success if flash[0] else st.error)(flash[1])
        st.caption(
            "Add things one by one. When a scanned object matches an item, the information you write here is shown. "
            "Aliases help the AI match (e.g. “travel document, booklet”)."
        )
        last = st.session_state.get("scan_last_photo")
        with st.form("scan_add_form", clear_on_submit=True):
            name = st.text_input("Name", key="scan_add_name", max_chars=80)
            keywords = st.text_input("Aliases / keywords (optional)", key="scan_add_keywords", max_chars=300)
            info = st.text_area("Information to show", key="scan_add_info", height=140, max_chars=4000)
            link = st.text_input("More-information link (optional)", key="scan_add_link", placeholder="https://…")
            photo = st.file_uploader("Reference photo (optional)", type=["png", "jpg", "jpeg", "webp"], key="scan_add_photo")
            use_last = st.checkbox("Use the last scanned photo as the reference photo", key="scan_add_photo_last", disabled=last is None)
            submitted = st.form_submit_button("➕ Add this item", type="primary")
        if submitted:
            data = photo.getvalue() if photo else (last if use_last else None)
            ok, msg = scan.add_item(db_path, name, keywords, info, link, data, username)
            if ok:
                if on_change:
                    on_change()
                st.session_state["scan_flash"] = (True, msg)
                st.rerun()          # reload the list; the message is shown after the reload
            else:
                st.error(msg)
                st.toast(msg, icon="⚠️")     # visible even when the error text is below the screen edge
        if not items:
            st.info("No items yet.")
        for it in items:
            with st.expander(f"📦 {it['name']}"):
                k = f"scan_item_{it['id']}"
                n = st.text_input("Name", value=it["name"], key=f"{k}_n", max_chars=80)
                kw = st.text_input("Aliases", value=it["keywords"] or "", key=f"{k}_k", max_chars=300)
                inf = st.text_area("Information", value=it["info"], key=f"{k}_i", height=120, max_chars=4000)
                lk = st.text_input("Link", value=it["link"] or "", key=f"{k}_l")
                if it.get("has_photo"):
                    ph = scan.get_photo(db_path, it["id"])
                    if ph:
                        st.image(ph, width=160)
                c1, c2 = st.columns(2)
                if c1.button("💾 Save", key=f"{k}_s"):
                    ok, msg = scan.update_item(db_path, it["id"], n, kw, inf, lk, username)
                    if ok:
                        if on_change:
                            on_change()
                        st.session_state["scan_flash"] = (True, f"Saved “{n}”.")
                        st.rerun()
                    else:
                        st.error(msg)
                if c2.button("🗑️ Delete", key=f"{k}_d"):
                    scan.delete_item(db_path, it["id"], username)
                    if on_change:
                        on_change()
                    st.session_state["scan_flash"] = (True, f"Deleted “{it['name']}”.")
                    st.rerun()


# ------------------------------------------------------------------ the Scan tab
def render_scan_tab(db_path, user_role, username, llm_cfg, vision_model, on_change=None):
    scan.init_tables(db_path)
    is_admin = user_role == "admin"
    st.header("📷 Scan")
    st.caption(
        "Point the camera at a real object and press **Scan**. The AI works out what it is and the information comes up. "
        "Objects the admin has added to the catalogue show the official information."
    )
    if is_admin:
        st.caption(f"Vision model in use: `{llm.resolve_vision_model(llm_cfg, vision_model) or 'not set'}`")
    source = st.radio("Photo from", ["📷 Camera", "🖼️ Upload a photo"], horizontal=True, key="scan_src", label_visibility="collapsed")
    if source.startswith("📷"):
        shot = st.camera_input("Point the camera at an object", key="scan_cam")
    else:
        shot = st.file_uploader("Choose a photo", type=["png", "jpg", "jpeg", "webp"], key="scan_up")
    data = shot.getvalue() if shot is not None else None

    busy = running("scan")
    if st.button("🔍 Scan", type="primary", disabled=busy or data is None, key="scan_go"):
        st.session_state["scan_last_photo"] = data
        st.session_state.pop("scan_result", None)
        start("scan", scan.identify, db_path, llm_cfg, vision_model, data)
        st.rerun()

    state, payload = poll("scan")
    if state == "running":
        st.info(f"⏳ Scanning… {payload}s")
    elif state == "done":
        st.session_state["scan_result"] = payload
        if on_change:
            on_change()
        st.rerun()

    res = st.session_state.get("scan_result")
    if res:
        ok, msg = res
        if ok:
            show_scan_result(msg, db_path, is_admin)
        else:
            st.error(msg)
    elif state == "idle" and data is None:
        st.info("Take a photo (or upload one), then press **Scan**.")

    if is_admin:
        render_item_manager(db_path, username, on_change)
    else:
        st.caption("Only admins can add items to the scan catalogue.")
