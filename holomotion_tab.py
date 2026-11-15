"""HoloMotion 3D tab: gesture-controlled 3D particle models (by G. Charan Teja, MIT licence).

The front end in ./holomotion is served by Streamlit's own component server, so no npm build,
no static-file setting and no extra hosting are needed. Hand tracking runs in the browser
(MediaPipe, loaded from a CDN) and the camera video never leaves the computer.
"""
import os

import streamlit as st
import streamlit.components.v1 as components

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holomotion")
_holomotion = components.declare_component("holomotion_3d", path=_DIR) if os.path.isdir(_DIR) else None


def render_holomotion_tab():
    st.header("🖐️ HoloMotion 3D")
    st.caption(
        "Control glowing 3D models (brain, heart, DNA, engines, cars, wonders…) with your hand in front of "
        "the webcam. Hand tracking runs inside your browser; the camera video is not uploaded anywhere."
    )
    if _holomotion is None:
        st.error("The HoloMotion files are missing (folder 'holomotion').")
        return

    running = st.session_state.get("holo_on", False)
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
        _holomotion(key="holomotion_view", default=None)
        st.caption(
            "Tips: snap to summon · fist to form · open hand to release/explode · twist to rotate · "
            "two hands to zoom · ☝ point / 🤏 pinch to pick a part · ✌ next model. "
            "HoloMotion 3D © G. Charan Teja, MIT licence."
        )
