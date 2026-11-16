import streamlit as st
from scan_handler import scan_object_with_ai


def render_scan_tab():
    """Drop-in Streamlit Scan tab using the fixed scan_object_with_ai handler."""
    st.header("Scan")
    st.caption(
        "Point the camera at a real object and press Scan. The AI works out what it is "
        "and the information comes up. Objects the admin has added to the catalogue show the official information."
    )

    mode = st.radio(
        "Input source",
        options=["Camera", "Upload a photo"],
        index=1,
        horizontal=True,
        label_visibility="collapsed",
    )

    photo = None
    if mode == "Camera":
        photo = st.camera_input("Take a photo")
    else:
        photo = st.file_uploader("Choose a photo", type=["jpg", "jpeg", "png", "webp"])

    if st.button("Scan", type="primary"):
        if photo is None:
            st.warning("Please capture or upload a photo first.")
        else:
            with st.spinner("Scanning object..."):
                result = scan_object_with_ai(
                    image_bytes=photo.getvalue(),
                    filename=photo.name,
                    catalogue_items=st.session_state.get("scan_catalogue", []),
                    api_base_url=st.secrets.get("AI_BASE_URL", None),
                    api_key=st.secrets.get("AI_API_KEY", None),
                    vision_model=st.secrets.get("AI_VISION_MODEL", None),
                )
            if result.startswith("AI service error") or result.startswith("Configuration error"):
                st.error(result)
            else:
                st.success("Scan complete")
                st.markdown(result)


if __name__ == "__main__":
    st.set_page_config(page_title="Scan Fix Demo", layout="wide")
    render_scan_tab()
