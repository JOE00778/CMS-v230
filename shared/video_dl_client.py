import os

import requests

BASE = os.environ.get("VIDEO_DL_BASE", "http://video-downloader:8000")


def probe(url: str) -> dict:
    r = requests.post(f"{BASE}/probe", json={"url": url}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(_detail(r))
    return r.json()


def download(url: str, progress=None):
    """流式下载，progress(recv_bytes) 回调用于进度条。返回 (bytes, filename)。"""
    with requests.post(f"{BASE}/download", json={"url": url},
                       stream=True, timeout=900) as r:
        if r.status_code != 200:
            raise RuntimeError(_detail(r))
        cd = r.headers.get("content-disposition", "")
        fname = "video.mp4"
        if "filename=" in cd:
            fname = cd.split("filename=")[-1].strip('"; ')
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=256 * 1024):
            buf.extend(chunk)
            if progress:
                progress(len(buf))
        return bytes(buf), fname


def _detail(r) -> str:
    try:
        return r.json().get("detail", r.text)
    except Exception:
        return r.text
