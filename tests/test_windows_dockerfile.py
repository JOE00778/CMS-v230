"""Windows-hosted Linux image runtime dependency checks."""
from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "windows" / "Dockerfile"


def test_streamlit_image_has_rapidocr_native_libraries():
    text = DOCKERFILE.read_text(encoding="utf-8")

    for package in ("libxcb1", "libgl1", "libglib2.0-0"):
        assert package in text, f"RapidOCR/OpenCV runtime package missing: {package}"
