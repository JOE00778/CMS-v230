"""image_processor — Smikie 商品图处理 sidecar（N8N HTTP 调用）

3 个核心端点：
  POST /upscale       低分原图 → 1500×1500 白底方图（Lanczos / TODO: realesrgan）
  POST /cutout        1500 白底图 → 抠图 + 套 SMIKIE RED 模板
  POST /compose-spu   N 个 SKU 白底图 → SPU 多图（PIL 网格拼接）

文件持久化全部走 /data/whitebg（容器内），由 docker-compose 挂到 Windows D 盘：
  D:\\Smikie-Images\\whitebg → /data/whitebg

目录结构（自动建）：
  /data/whitebg/raw/<JAN>.jpg          上传/下载的原图
  /data/whitebg/upscaled/<JAN>.jpg     1500×1500 白底方图
  /data/whitebg/branded/<JAN>.jpg      套模板成品（Shopee 主图候选 · 母版/默认模板）
  /data/whitebg/branded/<SHOP>/<JAN>.jpg  该店模板成品（2026-09-10 每店一套主图模板）
  /data/whitebg/spu/<SPU_KEY>.jpg      SPU 多图合成（拼接图）
  /data/whitebg/spu/<SHOP>/<SPU_KEY>.jpg  该店拼图
  /data/whitebg/cutout/<JAN>.png       rembg 抠图缓存（RGBA · 抠一次，套 20 店模板不再重抠）
  /data/whitebg/templates/<KEY>.png    店铺主图模板（CMS 上传 · 1500×1500 带透明通道；无则回落 template_red）
  /data/whitebg/manual/<JAN>.jpg       运营手传原图

输入策略：所有端点接受 (jan + image_url) 或 (jan + base64_bytes) 二选一；
        优先走 image_url（N8N 抓 CMS 图），避免大字段穿 N8N JSON。
"""
from __future__ import annotations

import base64
import io
import logging
import os
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field
from rembg import new_session, remove

# ────────────────────────────────────────────────────────────────
# 配置
# ────────────────────────────────────────────────────────────────
WHITEBG_ROOT = Path(os.environ.get("WHITEBG_ROOT", "/data/whitebg"))
TEMPLATE_PATH = Path(os.environ.get("TEMPLATE_PATH", "/app/assets/template_red.png"))
CANVAS = 1500
PRODUCT_RATIO = 0.80
MAX_BOX = int(CANVAS * PRODUCT_RATIO)
LOGO_BOTTOM = 290   # 与 compose_with_template.py 一致：产品顶部不能高于此

DIRS = {
    "raw": WHITEBG_ROOT / "raw",
    "upscaled": WHITEBG_ROOT / "upscaled",
    "branded": WHITEBG_ROOT / "branded",
    "spu": WHITEBG_ROOT / "spu",
    "cutout": WHITEBG_ROOT / "cutout",
    "templates": WHITEBG_ROOT / "templates",
    "manual": WHITEBG_ROOT / "manual",
}
_KEY_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")   # shop_key / template_key / jan 文件名安全


def _safe_key(v: str | None, what: str) -> str | None:
    if v is None or v == "":
        return None
    if not _KEY_RE.match(v):
        raise HTTPException(400, f"{what} 只能是字母数字 _ . -（收到 {v!r}）")
    return v


def _out_path(kind: str, key: str, shop_key: str | None, ext: str = "jpg") -> Path:
    d = DIRS[kind] / shop_key if shop_key else DIRS[kind]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.{ext}"


def _resolve_template(template_key: str | None) -> tuple[Path, str]:
    """(模板路径, 实际用的 key)。有店铺模板用店铺的，没有回落默认红模板并标 'default'。"""
    if template_key:
        p = DIRS["templates"] / f"{template_key}.png"
        if p.exists():
            return p, template_key
    return TEMPLATE_PATH, "default"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("image_processor")

app = FastAPI(title="Smikie Image Processor", version="0.2.0")

_REMBG_SESSION = None


def _rembg():
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        log.info("init rembg u2net session")
        _REMBG_SESSION = new_session("u2net")
    return _REMBG_SESSION


def _ensure_dirs():
    for p in DIRS.values():
        p.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
def _startup():
    _ensure_dirs()
    if not TEMPLATE_PATH.exists():
        log.warning(f"template not found: {TEMPLATE_PATH}")
    else:
        log.info(f"template ready: {TEMPLATE_PATH}")
    log.info(f"WHITEBG_ROOT = {WHITEBG_ROOT}")


# ────────────────────────────────────────────────────────────────
# 帮助函数
# ────────────────────────────────────────────────────────────────
async def _fetch_bytes(image_url: str | None, image_b64: str | None) -> bytes:
    if image_url:
        async with httpx.AsyncClient(timeout=30) as cx:
            r = await cx.get(image_url)
            r.raise_for_status()
            return r.content
    if image_b64:
        return base64.b64decode(image_b64)
    raise HTTPException(400, "must provide image_url or image_b64")


def _upscale_lanczos(img: Image.Image) -> Image.Image:
    """最长边 → 1500，白底居中粘贴成 1500×1500 方图。无 AI 超分。"""
    img = img.convert("RGB") if img.mode != "RGB" else img
    img.thumbnail((CANVAS, CANVAS), Image.LANCZOS)
    canvas = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
    canvas.paste(img, ((CANVAS - img.width) // 2, (CANVAS - img.height) // 2))
    return canvas


def _cutout_rgba(jan: str, white_bg_jpg_bytes: bytes, overwrite: bool = False) -> Image.Image:
    """rembg 抠图，结果缓存 cutout/<JAN>.png。套 20 店模板时只抠这一次。"""
    cache = DIRS["cutout"] / f"{jan}.png"
    if cache.exists() and not overwrite:
        return Image.open(cache).convert("RGBA")
    cut = remove(white_bg_jpg_bytes, session=_rembg())
    prod = Image.open(io.BytesIO(cut)).convert("RGBA")
    bbox = prod.getbbox()
    if not bbox:
        raise HTTPException(422, "rembg produced empty alpha — bad input?")
    prod = prod.crop(bbox)
    DIRS["cutout"].mkdir(parents=True, exist_ok=True)
    prod.save(cache, "PNG", optimize=True)
    return prod


def _apply_template(prod: Image.Image, template_path: Path) -> Image.Image:
    """抠好的 RGBA → 套模板。逻辑同 shopify/scripts/compose_with_template.py"""
    prod = prod.copy()
    prod.thumbnail((MAX_BOX, MAX_BOX), Image.LANCZOS)

    template = Image.open(template_path).convert("RGBA")
    if template.size != (CANVAS, CANVAS):
        template = template.resize((CANVAS, CANVAS), Image.LANCZOS)
    out = template.copy()

    x = (CANVAS - prod.width) // 2
    y = (CANVAS - prod.height) // 2
    if x + prod.width > 1050 and y < LOGO_BOTTOM:
        new_top = LOGO_BOTTOM
        if new_top + prod.height > CANVAS - 30:
            max_h = CANVAS - 30 - new_top
            scale = max_h / prod.height
            prod = prod.resize((int(prod.width * scale), max_h), Image.LANCZOS)
            x = (CANVAS - prod.width) // 2
        y = new_top
    out.paste(prod, (x, y), prod)
    return out.convert("RGB")


def _grid_layout(n: int) -> tuple[int, int]:
    """N 张图 → (cols, rows)。1=>1x1, 2=>2x1, 3=>3x1, 4=>2x2, 5-6=>3x2, 7-9=>3x3."""
    if n <= 1: return (1, 1)
    if n == 2: return (2, 1)
    if n == 3: return (3, 1)
    if n == 4: return (2, 2)
    if n <= 6: return (3, 2)
    return (3, 3)


# ────────────────────────────────────────────────────────────────
# 请求/响应模型
# ────────────────────────────────────────────────────────────────
class SingleImageReq(BaseModel):
    jan: str = Field(..., min_length=1, description="JAN/SKU 编号，用于落盘文件名")
    image_url: str | None = Field(None, description="远程图 URL（CMS / NAS）")
    image_b64: str | None = Field(None, description="base64 字节（fallback）")
    method: Literal["lanczos", "realesrgan"] = Field(
        "lanczos",
        description="lanczos=CPU 瞬时；realesrgan=未实装（v1 用 lanczos 起步）",
    )
    overwrite: bool = False
    template_key: str | None = Field(None, description="店铺模板 key（templates/<key>.png）；无/不存在 → 默认红模板")
    shop_key: str | None = Field(None, description="给了就落到 branded/<shop_key>/；母版不给")


class SingleImageResp(BaseModel):
    jan: str
    saved_path: str
    width: int
    height: int
    skipped: bool = False
    template_key: str | None = None      # 实际用的模板：店铺 key 或 'default'
    source: str | None = None            # auto / manual / manual_templated


class ComposeSpuReq(BaseModel):
    spu_key: str = Field(..., min_length=1)
    sku_jans: list[str] = Field(..., min_length=1, max_length=9)
    source: Literal["branded", "upscaled"] = Field(
        "branded",
        description="拼图素材：branded=套模板成品，upscaled=纯白底（建议 branded）",
    )
    overwrite: bool = False
    shop_key: str | None = Field(None, description="给了就用 branded/<shop_key>/ 素材、落 spu/<shop_key>/")


class ManualImageReq(BaseModel):
    """运营手传主图。templated=False → 成品原样（补成 1500 方图）；True → 走抠图 + 套模板。"""
    jan: str = Field(..., min_length=1)
    image_b64: str = Field(..., min_length=16)
    templated: bool = False
    template_key: str | None = None
    shop_key: str | None = None


class TemplateReq(BaseModel):
    image_b64: str = Field(..., min_length=16)


class TemplateResp(BaseModel):
    key: str
    saved_path: str
    width: int
    height: int
    has_alpha: bool
    updated_at: float


class ComposeSpuResp(BaseModel):
    spu_key: str
    saved_path: str
    layout: str           # "2x2", "3x3" 等
    skus_used: list[str]
    skus_missing: list[str]


# ────────────────────────────────────────────────────────────────
# 端点
# ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "template_ready": TEMPLATE_PATH.exists(),
        "whitebg_root": str(WHITEBG_ROOT),
        "subdirs": {k: str(v) for k, v in DIRS.items()},
    }


@app.post("/upscale", response_model=SingleImageResp)
async def upscale(req: SingleImageReq):
    """原图 → 1500×1500 白底方图。落 /data/whitebg/upscaled/<JAN>.jpg"""
    _ensure_dirs()
    dst = DIRS["upscaled"] / f"{req.jan}.jpg"
    if dst.exists() and not req.overwrite:
        with Image.open(dst) as im:
            return SingleImageResp(jan=req.jan, saved_path=str(dst), width=im.width, height=im.height, skipped=True)

    raw = await _fetch_bytes(req.image_url, req.image_b64)
    src = Image.open(io.BytesIO(raw))

    if req.method == "realesrgan":
        raise HTTPException(501, "realesrgan backend not wired yet — use method=lanczos for v1")

    out = _upscale_lanczos(src)
    out.save(dst, "JPEG", quality=88, optimize=True)
    return SingleImageResp(jan=req.jan, saved_path=str(dst), width=out.width, height=out.height)


@app.post("/cutout", response_model=SingleImageResp)
async def cutout(req: SingleImageReq):
    """白底图 → 抠图（缓存）+ 套模板。落 branded/[<shop_key>/]<JAN>.jpg

    若同名 upscaled 已存在，优先用它（避免重复下载）。否则按入参取图。
    模板：template_key 对应 templates/<key>.png；没有则默认红模板（响应 template_key='default'）。
    """
    _ensure_dirs()
    shop = _safe_key(req.shop_key, "shop_key")
    _safe_key(req.template_key, "template_key")
    dst = _out_path("branded", req.jan, shop)
    tpl_path, tpl_key = _resolve_template(req.template_key)
    if dst.exists() and not req.overwrite:
        with Image.open(dst) as im:
            return SingleImageResp(jan=req.jan, saved_path=str(dst), width=im.width, height=im.height,
                                   skipped=True, template_key=tpl_key, source="auto")

    cache = DIRS["cutout"] / f"{req.jan}.png"
    src_pre = DIRS["upscaled"] / f"{req.jan}.jpg"
    if cache.exists():
        prod = _cutout_rgba(req.jan, b"")
    elif src_pre.exists():
        prod = _cutout_rgba(req.jan, src_pre.read_bytes())
    else:
        if not req.image_url and not req.image_b64:
            raise HTTPException(404, f"no upscaled/cutout cache for {req.jan} and no image given — run /upscale first")
        prod = _cutout_rgba(req.jan, await _fetch_bytes(req.image_url, req.image_b64))

    out = _apply_template(prod, tpl_path)
    out.save(dst, "JPEG", quality=90, optimize=True)
    return SingleImageResp(jan=req.jan, saved_path=str(dst), width=out.width, height=out.height,
                           template_key=tpl_key, source="auto")


@app.post("/manual-image", response_model=SingleImageResp)
def manual_image(req: ManualImageReq):
    """运营手传主图（Boss 2026-09-10「上传主图的形式」+「上传原图 → 也走抠图套模板」勾选）。

    templated=False：成品图原样，只补成 1500×1500 白底方图 → branded/[<shop>/]<JAN>.jpg（source=manual）
    templated=True ：原图 → 白底方图 → 抠图（重抠并刷新缓存）→ 套模板 → 同路径（source=manual_templated）
    原图都留一份在 manual/<JAN>.jpg。
    """
    _ensure_dirs()
    shop = _safe_key(req.shop_key, "shop_key")
    _safe_key(req.template_key, "template_key")
    try:
        raw = base64.b64decode(req.image_b64)
        src = Image.open(io.BytesIO(raw))
        src.load()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"image_b64 不是可读图片: {e}")
    square = _upscale_lanczos(src)
    (DIRS["manual"] / f"{req.jan}.jpg").write_bytes(_jpeg_bytes(square))
    dst = _out_path("branded", req.jan, shop)
    if req.templated:
        tpl_path, tpl_key = _resolve_template(req.template_key)
        prod = _cutout_rgba(req.jan, _jpeg_bytes(square), overwrite=True)
        out = _apply_template(prod, tpl_path)
        source = "manual_templated"
    else:
        out, tpl_key, source = square, None, "manual"
    out.save(dst, "JPEG", quality=92, optimize=True)
    return SingleImageResp(jan=req.jan, saved_path=str(dst), width=out.width, height=out.height,
                           template_key=tpl_key, source=source)


def _jpeg_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=95)
    return buf.getvalue()


# ─── 店铺主图模板（CMS 上传管理 · 不进镜像 · 换模板不用重建/重启）───
def _template_resp(p: Path) -> TemplateResp:
    with Image.open(p) as im:
        return TemplateResp(key=p.stem, saved_path=str(p), width=im.width, height=im.height,
                            has_alpha=im.mode in ("RGBA", "LA") or "transparency" in im.info,
                            updated_at=p.stat().st_mtime)


@app.get("/templates", response_model=list[TemplateResp])
def list_templates():
    _ensure_dirs()
    return [_template_resp(p) for p in sorted(DIRS["templates"].glob("*.png"))]


@app.put("/template/{key}", response_model=TemplateResp)
def put_template(key: str, req: TemplateReq):
    """上传/替换一张店铺模板。硬校验：PNG · 1500×1500 · 带透明通道（产品要透出来）。"""
    _ensure_dirs()
    key = _safe_key(key, "template key")
    try:
        im = Image.open(io.BytesIO(base64.b64decode(req.image_b64)))
        im.load()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"不是可读图片: {e}")
    if im.format != "PNG":
        raise HTTPException(422, f"模板必须是 PNG（收到 {im.format}）")
    if im.size != (CANVAS, CANVAS):
        raise HTTPException(422, f"模板必须是 {CANVAS}×{CANVAS}（收到 {im.width}×{im.height}）")
    if not (im.mode in ("RGBA", "LA") or "transparency" in im.info):
        raise HTTPException(422, "模板必须带透明通道（中间要透出产品）")
    dst = DIRS["templates"] / f"{key}.png"
    im.convert("RGBA").save(dst, "PNG", optimize=True)
    return _template_resp(dst)


@app.delete("/template/{key}")
def delete_template(key: str):
    key = _safe_key(key, "template key")
    p = DIRS["templates"] / f"{key}.png"
    if not p.exists():
        raise HTTPException(404, "no such template")
    p.unlink()
    return {"deleted": key}


@app.post("/compose-spu", response_model=ComposeSpuResp)
def compose_spu(req: ComposeSpuReq):
    """N 个 SKU 已处理图 → 1 张 SPU 拼接总览图。

    每张子图落到 CANVAS // cols 大小的格子里，居中粘贴，1500×1500 白底。
    """
    _ensure_dirs()
    shop = _safe_key(req.shop_key, "shop_key")
    dst = _out_path("spu", req.spu_key, shop)
    if dst.exists() and not req.overwrite:
        with Image.open(dst) as im:
            return ComposeSpuResp(
                spu_key=req.spu_key, saved_path=str(dst),
                layout=f"{im.width}x{im.height}", skus_used=req.sku_jans, skus_missing=[],
            )

    src_dir = (DIRS[req.source] / shop) if (shop and req.source == "branded") else DIRS[req.source]
    used: list[str] = []
    missing: list[str] = []
    tiles: list[Image.Image] = []
    for jan in req.sku_jans:
        p = src_dir / f"{jan}.jpg"
        if not p.exists():
            missing.append(jan)
            continue
        tiles.append(Image.open(p).convert("RGB"))
        used.append(jan)

    if not tiles:
        raise HTTPException(404, f"no source images found in {src_dir} for any of {req.sku_jans}")

    cols, rows = _grid_layout(len(tiles))
    cell_w = CANVAS // cols
    cell_h = CANVAS // rows
    canvas = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        tile_copy = tile.copy()
        tile_copy.thumbnail((cell_w, cell_h), Image.LANCZOS)
        ox = c * cell_w + (cell_w - tile_copy.width) // 2
        oy = r * cell_h + (cell_h - tile_copy.height) // 2
        canvas.paste(tile_copy, (ox, oy))
    canvas.save(dst, "JPEG", quality=90, optimize=True)

    return ComposeSpuResp(
        spu_key=req.spu_key,
        saved_path=str(dst),
        layout=f"{cols}x{rows}",
        skus_used=used,
        skus_missing=missing,
    )


@app.get("/list/{kind}/{key}")
def list_image(kind: Literal["raw", "upscaled", "branded", "spu", "cutout", "manual"], key: str,
               shop_key: str | None = None):
    """查某 JAN/SPU 是否已有图。返回 {exists, path}。"""
    ext = "png" if kind == "cutout" else "jpg"
    d = DIRS[kind] / shop_key if shop_key else DIRS[kind]
    p = d / f"{key}.{ext}"
    return {"exists": p.exists(), "path": str(p) if p.exists() else None}
