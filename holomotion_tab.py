"""HoloMotion 3D tab: gesture-controlled 3D particle models (by G. Charan Teja, MIT licence).

The front end in ./holomotion is served by Streamlit's own component server, so no npm build,
no static-file setting and no extra hosting are needed. Hand tracking runs in the browser
(MediaPipe, loaded from a CDN) and the camera video never leaves the computer.
"""
import base64
import json
import os

import streamlit as st
import streamlit.components.v1 as components

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holomotion")
import scan
import scan_ui

_holomotion = components.declare_component("holomotion_3d", path=_DIR) if os.path.isdir(_DIR) else None


def render_holomotion_tab(db_path=None, llm_cfg=None, vision_model="", on_change=None):
    st.header("🖐️ HoloMotion 3D")
    st.caption(
        "Control glowing 3D models (brain, heart, DNA, engines, cars, wonders…) with your hand in front of "
        "the webcam. Hand tracking runs inside your browser; the camera video is not uploaded anywhere."
    )
    if _holomotion is None:
        st.error("The HoloMotion files are missing (folder 'holomotion').")
        return

    running = st.session_state.get("holo_on", False)
    if not running:
        st.session_state["scanjob:holoscan"] = None   # viewer closed: forget any unfinished scan
    c1, c2 = st.columns([1, 3])
    with c1:
        if not running:
            if st.button("▶ Launch HoloMotion 3D", type="primary", key="holo_launch"):
                st.session_state["holo_on"] = True
                st.rerun()
        else:
            if st.button("⏹ Close", key="holo_close"):
                st.session_state["holo_on"] = False
                st.rerun()
    with c2:
        st.caption(
            "Needs Chrome or Edge, a webcam and internet (hand-tracking engine loads from a CDN). "
            "Click **Start with camera** inside the viewer and allow the camera."
        )

    if running:
        val = _holomotion(key="holomotion_view", default=None, result=st.session_state.get("holo_scan_result"))
        _handle_scan(val, db_path, llm_cfg, vision_model, on_change)
        st.caption(
            "🔍 **Scan**: turn the camera on, hold a real object in front of it and press the 🔍 button in the viewer; "
            "the AI tells you what it is. Admins add known items in the 📷 Scan tab.\n\n"
            "Tips: snap to summon · fist to form · open hand to release/explode · twist to rotate · "
            "two hands to zoom · ☝ point / 🤏 pinch to pick a part · ✌ next model. "
            "HoloMotion 3D © G. Charan Teja, MIT licence."
        )


def _handle_scan(val, db_path, llm_cfg, vision_model, on_change):
    """🔍 Scan inside the viewer: the page sends a camera frame, we identify it, the result goes back as an argument."""
    if db_path is None or llm_cfg is None:
        return
    if isinstance(val, dict) and val.get("type") == "scan" and val.get("id") and val["id"] != st.session_state.get("holo_scan_seen"):
        st.session_state["holo_scan_seen"] = val["id"]
        st.session_state["holo_scan_pending"] = val["id"]
        try:
            jpeg = base64.b64decode(str(val.get("image", "")).split(",", 1)[1])
        except Exception:
            st.session_state["holo_scan_result"] = {"id": val["id"], "ok": False, "error": "The camera picture could not be read."}
            st.rerun()
            return
        scan_ui.start("holoscan", scan.identify, db_path, llm_cfg, vision_model, jpeg)
        st.rerun()
    state, payload = scan_ui.poll("holoscan")
    if state == "running":
        st.caption(f"⏳ Scanning… {payload}s")
    elif state == "done":
        ok, msg = payload
        pid = st.session_state.get("holo_scan_pending")
        try:
            data = json.loads(msg) if ok else None
        except Exception:
            ok, data, msg = False, None, "The scan result could not be read."
        st.session_state["holo_scan_result"] = {"id": pid, "ok": bool(ok), "data": data, "error": None if ok else msg}
        if on_change:
            on_change()
        st.rerun()
