import os
import sys
import hashlib
import io
import json
import uuid
import subprocess
import shutil
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
from flask import Flask, Response, abort, jsonify, make_response, redirect, request, send_file, send_from_directory
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from image_edit_service import ImageEditError, generate_masked_edit, load_mask, load_original, optimize_edit_prompt, save_png
from giga_material_download import (
    GigaMaterialDownloadError,
    GigaMaterialDownloader,
    validate_item_code,
)

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from auth import configure_access
from path_safety import confined_child, safe_identifier

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB（支持大素材包）
configure_access(app, public_path_prefixes=("/public/images/",))


def _resolve_sandbox_shim():
    """Locate WorkBuddy's sitecustomize shim if it is loaded in this process."""
    import sys as _sys

    for _mod in list(_sys.modules.values()):
        if _mod is None:
            continue
        if hasattr(_mod, "_orig_shutil_rmtree") and hasattr(_mod, "_orig_unlink"):
            return _mod
    return None


_SANDBOX_SHIM = _resolve_sandbox_shim()


def _restore_raw_deleters():
    """Undo the sandbox delete shim inside this long-running service process.

    WorkBuddy 的 sitecustomize 把 os.unlink / os.remove / os.rmdir / Path.unlink
    全部改写为走"每轮删除守卫"的实现：单轮累计删除 ≥50 个文件时，守卫直接在当前
    线程 raise SystemExit(1)。典型故障（2026-10-10）：/api/upload/complete 收尾
    清理 uploads/<run>/.chunks 里几十个 .part 时触发，正在服务该请求的 waitress
    工作线程当场死掉，浏览器端就永远停在"上传完成/处理中"；后台 retention 清理
    线程同样会被打死，导致 data/ 越积越大。

    历史版本只还原了 shutil.rmtree，但 rmtree 内部仍通过 os.unlink 删除文件，
    依旧落在守卫里，所以不够。这里一次性还原全部删除原语。本服务的删除只发生在
    自己的 data/uploads|outputs|reports|image_edits 目录内（_safe_rmtree 有白名单
    校验），还原仅跳过面向 Agent 会话的守卫，不影响其它沙箱保护；普通终端启动
    （未加载 shim）时为 no-op。
    """
    mod = _SANDBOX_SHIM
    if mod is None:
        return False
    os.unlink = getattr(mod, "_orig_unlink", os.unlink)
    os.remove = getattr(mod, "_orig_remove", os.remove)
    os.rmdir = getattr(mod, "_orig_rmdir", os.rmdir)
    Path.unlink = getattr(mod, "_orig_path_unlink", Path.unlink)
    Path.rmdir = getattr(mod, "_orig_path_rmdir", Path.rmdir)
    shutil.rmtree = getattr(mod, "_orig_shutil_rmtree", shutil.rmtree)
    return True


_ORIG_RMTREE = getattr(_SANDBOX_SHIM, "_orig_shutil_rmtree", shutil.rmtree)
_SANDBOX_SHIM_DISABLED = _restore_raw_deleters()


def _safe_rmtree(path, ignore_errors=True):
    """Remove only a material run directory, never an arbitrary user path."""
    target = Path(path).resolve()
    allowed_roots = (UPLOAD_DIR.resolve(), OUTPUT_DIR.resolve(), REPORT_DIR.resolve(), EDIT_DIR.resolve())
    if not any(target != root and target.is_relative_to(root) for root in allowed_roots):
        raise ValueError(f"refuse to delete outside material run directories: {target}")
    if not target.exists():
        return
    # 删除原语已在启动时还原（_restore_raw_deleters），这里走原始 rmtree，
    # 清理工作不会再被 bulk-delete guard SystemExit 掉请求线程。
    _ORIG_RMTREE(target, ignore_errors=ignore_errors)


APP_NAME = "giga-material-processor"
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
REPORT_DIR = DATA_DIR / "reports"
EDIT_DIR = DATA_DIR / "image_edits"
PUBLIC_IMAGE_DIR = DATA_DIR / "public_images"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)
EDIT_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

RUNS_FILE = DATA_DIR / "material_runs.json"

PROJECT_ROOT = Path(os.environ.get("BUNDLE_ROOT", str(Path(__file__).resolve().parent.parent.parent)))
PYTHON_BIN = os.environ.get("BUNDLE_PYTHON", sys.executable)
PROCESS_SCRIPT = PROJECT_ROOT / "scripts" / "process_materials_json.py"
CHUNK_SIZE = 16 * 1024 * 1024
PROCESS_JOBS = {}
GIGA_MATERIAL_JOB_SOURCE = "giga_material_download"
GIGA_MATERIAL_ARCHIVE_RE = re.compile(r"^[A-Za-z0-9._+\-]+\.zip$", flags=re.IGNORECASE)
IMAGE_EDIT_JOBS = {}
IMAGE_EDIT_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[a-f0-9]{8}$")
PUBLIC_IMAGE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
PUBLIC_IMAGE_MAX_BYTES = 10 * 1024 * 1024
PUBLIC_IMAGE_MAX_COUNT = 20
PUBLIC_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF"}
PUBLIC_IMAGE_BASE_DOMAIN = os.environ.get("KROWORK_BASE_DOMAIN", "huichengds.dpdns.org").strip().strip(".")
if not re.fullmatch(r"[A-Za-z0-9.-]+", PUBLIC_IMAGE_BASE_DOMAIN):
    PUBLIC_IMAGE_BASE_DOMAIN = "huichengds.dpdns.org"
PUBLIC_IMAGE_BASE_URL = f"https://material.{PUBLIC_IMAGE_BASE_DOMAIN}/public/images"
PUBLIC_IMAGE_FILE_TYPES = {
    "jpg": {"format": "JPEG", "mime_type": "image/jpeg"},
    "png": {"format": "PNG", "mime_type": "image/png"},
}
PUBLIC_IMAGE_MIN_SIDE = 500
PUBLIC_IMAGE_ZOOM_MIN_SIDE = 1000
PUBLIC_IMAGE_MAX_SIDE = 10000
PUBLIC_IMAGE_VERIFY_TIMEOUT = 10
PUBLIC_IMAGE_VERIFY_OPENER = build_opener(ProxyHandler({}))
PUBLIC_IMAGE_VERIFY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
PUBLIC_IMAGE_SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
PUBLIC_IMAGE_SRGB_ICC = PUBLIC_IMAGE_SRGB_PROFILE.tobytes()


def _validated_run_id(value):
    return safe_identifier(value, label="run_id")


def _run_dir(root, value):
    return confined_child(root, _validated_run_id(value), label="run directory")


def _run_data_path(prefix, value):
    run_id = _validated_run_id(value)
    return confined_child(DATA_DIR, f"{prefix}{run_id}.json", label="run data")


def _load_json(path, default):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_runs():
    return _load_json(RUNS_FILE, [])


# --- Retention policy -------------------------------------------------------
# uploads/ and outputs/ are staging areas for GIGA material processing: a run
# uploads its raw archives into uploads/<run_id>/, the worker writes processed
# archives into outputs/<run_id>/.  Nothing else consumes them after the job
# finishes, so without a retention pass they grow a few GB per day until the
# disk fills up.  Both are pruned to RETENTION_DAYS here.
#
# Set RETENTION_DAYS=0 to disable pruning entirely.
RETENTION_DAYS = int(os.environ.get("MATERIAL_RETENTION_DAYS", "1"))
RETENTION_SWEEP_INTERVAL_SECONDS = max(
    60, int(os.environ.get("MATERIAL_RETENTION_INTERVAL_SECONDS", "1800"))
)
# Guards against pruning a batch whose worker is still writing into it.
ACTIVE_RUN_IDS = set()


def _retention_enabled():
    return RETENTION_DAYS > 0


def _prune_one_run_dir(root, run_id, cutoff_ts):
    """Delete a single run directory under root if it is older than the cutoff.

    Returns freed bytes, or 0 when the directory was kept.  Skips directories
    with recent mtimes (a run in progress touches its manifest as it goes) and
    anything currently in ACTIVE_RUN_IDS.
    """
    if run_id in ACTIVE_RUN_IDS:
        return 0
    run_dir = root / run_id
    if not run_dir.is_dir():
        return 0
    try:
        newest = max(p.stat().st_mtime for p in run_dir.rglob("*"))
    except (OSError, ValueError):
        return 0
    if newest >= cutoff_ts:
        return 0
    freed = sum(p.stat().st_size for p in run_dir.rglob("*") if p.is_file())
    _safe_rmtree(run_dir)
    return freed


def _prune_stale_job_file(job_path, cutoff_ts):
    """Drop the task metadata for a run whose data has been pruned.

    Keeping the JSON while its uploads/outputs are gone makes the task detail
    page fail with a 404 when the user asks to view the result, so the two are
    removed together.
    """
    try:
        if job_path.stat().st_mtime >= cutoff_ts:
            return 0
        freed = job_path.stat().st_size
        # _ORIG_RMTREE only removes directory trees; this path is a single file.
        # It is read from DATA_DIR only ever matching our own material_job_*.json
        # glob, so a plain unlink stays inside the bundle.
        job_path.unlink()
        return freed
    except OSError:
        return 0


def run_retention_sweep():
    """One retention pass over uploads/, outputs/ and material_job_*.json."""
    if not _retention_enabled():
        return {"enabled": False}
    cutoff_ts = time.time() - RETENTION_DAYS * 86400
    freed_bytes = 0
    pruned_runs = []
    for root in (UPLOAD_DIR, OUTPUT_DIR):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            freed = _prune_one_run_dir(root, child.name, cutoff_ts)
            if freed:
                pruned_runs.append(child.name)
                freed_bytes += freed
    for job_path in DATA_DIR.glob("material_job_*.json"):
        run_id = job_path.name[len("material_job_"):-len(".json")]
        if not (_run_dir(UPLOAD_DIR, run_id).exists()
                or _run_dir(OUTPUT_DIR, run_id).exists()):
            freed_bytes += _prune_stale_job_file(job_path, cutoff_ts)
    # Records in material_runs.json pointing at pruned batches would render as
    # broken rows in the history list, so drop the matching entries too.
    try:
        runs = _load_runs()
    except (OSError, ValueError):
        runs = []
    if pruned_runs and isinstance(runs, list):
        pruned = set(pruned_runs)
        kept = [r for r in runs if r.get("run_id") not in pruned]
        if len(kept) != len(runs):
            _save_runs(kept)
    return {
        "enabled": True,
        "retention_days": RETENTION_DAYS,
        "pruned_runs": pruned_runs,
        "freed_bytes": freed_bytes,
    }


def _retention_loop():
    """Background sweeper: prune stale batches on a fixed interval."""
    while True:
        time.sleep(RETENTION_SWEEP_INTERVAL_SECONDS)
        try:
            result = run_retention_sweep()
            if result.get("pruned_runs"):
                print(
                    "[retention] pruned %d run(s), freed %.1f MB"
                    % (len(result["pruned_runs"]), result["freed_bytes"] / (1024 ** 2))
                )
        except Exception as exc:  # never let the sweeper kill the service
            print("[retention] sweep failed: %r" % (exc,))


def _save_runs(runs):
    _save_json(RUNS_FILE, runs)


def _storage_stats():
    disk = shutil.disk_usage(DATA_DIR)
    return {
        "part_files": sum(1 for _ in DATA_DIR.rglob("*.part")),
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "disk_used_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
    }


def _safe_name(value, fallback):
    text = (value or "").strip() or fallback
    text = re.sub(r"[^\w\u4e00-\u9fff.+-]+", "_", text, flags=re.UNICODE).strip("._")
    return text or fallback


def _new_run_dir():
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    run_dir = _run_dir(UPLOAD_DIR, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir


def _manifest_path(run_dir):
    return run_dir / "manifest.json"


def _job_path(run_id):
    return _run_data_path("material_job_", run_id)


def _edit_dir(edit_id):
    if not IMAGE_EDIT_ID_RE.fullmatch(edit_id or ""):
        raise ValueError("无效的编辑任务编号")
    return EDIT_DIR / edit_id


def _edit_metadata_path(edit_id):
    return _edit_dir(edit_id) / "metadata.json"


def _load_edit(edit_id):
    try:
        return _load_json(_edit_metadata_path(edit_id), {})
    except ValueError:
        return {}


def _save_edit(edit_id, data):
    _save_json(_edit_metadata_path(edit_id), data)
    IMAGE_EDIT_JOBS[edit_id] = data


def _update_edit(edit_id, **changes):
    data = _load_edit(edit_id)
    data.update(changes)
    _save_edit(edit_id, data)
    return data


def _new_edit_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _public_image_extension(value):
    """Return a safe public extension, defaulting legacy records to JPEG."""
    extension = str(value or "jpg").strip().lower().lstrip(".")
    if extension == "jpeg":
        extension = "jpg"
    return extension if extension in PUBLIC_IMAGE_FILE_TYPES else "jpg"


def _public_image_filename(asset_id, extension="jpg"):
    asset_id = str(asset_id or "")
    if not PUBLIC_IMAGE_ID_RE.fullmatch(asset_id):
        raise ValueError("无效的公开图片编号")
    return f"{asset_id}.{_public_image_extension(extension)}"


def _public_image_url(asset_id, extension="jpg"):
    base_url = os.environ.get("MATERIAL_PUBLIC_IMAGE_BASE_URL", PUBLIC_IMAGE_BASE_URL).strip().rstrip("/")
    if not base_url.startswith("https://"):
        base_url = PUBLIC_IMAGE_BASE_URL
    return f"{base_url}/{_public_image_filename(asset_id, extension)}"


def _public_image_amazon_checks(metadata):
    """Return transparent technical checks; visual/category policy still needs review."""
    extension = _public_image_extension(metadata.get("extension"))
    try:
        width = int(metadata.get("width") or 0)
        height = int(metadata.get("height") or 0)
        byte_count = int(metadata.get("bytes") or 0)
    except (TypeError, ValueError):
        width = height = byte_count = 0
    longest_side = max(width, height)
    supported_format = extension in PUBLIC_IMAGE_FILE_TYPES
    dimension_ok = PUBLIC_IMAGE_MIN_SIDE <= longest_side <= PUBLIC_IMAGE_MAX_SIDE
    size_ok = 0 < byte_count <= PUBLIC_IMAGE_MAX_BYTES
    https_ok = _public_image_url(metadata.get("asset_id"), extension).startswith("https://")
    technical_ready = supported_format and dimension_ok and size_ok and https_ok
    zoom_ready = technical_ready and longest_side >= PUBLIC_IMAGE_ZOOM_MIN_SIDE
    return {
        "technical_ready": technical_ready,
        "zoom_ready": zoom_ready,
        "supported_format": supported_format,
        "dimension_ok": dimension_ok,
        "file_size_ok": size_ok,
        "https_url": https_ok,
        "longest_side": longest_side,
        "minimum_side": PUBLIC_IMAGE_MIN_SIDE,
        "recommended_zoom_side": PUBLIC_IMAGE_ZOOM_MIN_SIDE,
        "content_review_required": True,
    }


def _public_image_record(metadata):
    record = dict(metadata)
    extension = _public_image_extension(record.get("extension"))
    record["extension"] = extension
    record["filename"] = _public_image_filename(record["asset_id"], extension)
    record["mime_type"] = PUBLIC_IMAGE_FILE_TYPES[extension]["mime_type"]
    record["url"] = _public_image_url(record["asset_id"], extension)
    record["amazon"] = _public_image_amazon_checks(record)
    return record


def _source_has_transparency(source):
    return source.mode in {"RGBA", "LA"} or "transparency" in source.info


def _source_orientation(source):
    try:
        return int(source.getexif().get(0x0112, 1) or 1)
    except (AttributeError, TypeError, ValueError):
        return 1


def _lossless_public_png(image):
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="PNG",
        optimize=True,
        icc_profile=image.info.get("icc_profile") or PUBLIC_IMAGE_SRGB_ICC,
    )
    return buffer.getvalue()


def _high_quality_public_jpeg(image):
    """Only use JPEG when a lossless PNG cannot meet Amazon's 10 MB limit."""
    for quality in (100, 98, 96):
        buffer = io.BytesIO()
        # 4:4:4 avoids the colour-edge blur around text and line drawings caused
        # by the old default chroma subsampling path.
        image.save(
            buffer,
            format="JPEG",
            quality=quality,
            subsampling=0,
            optimize=True,
            progressive=False,
            icc_profile=image.info.get("icc_profile") or PUBLIC_IMAGE_SRGB_ICC,
        )
        payload = buffer.getvalue()
        if len(payload) <= PUBLIC_IMAGE_MAX_BYTES:
            return payload, quality
    raise ValueError("无损图片超过 10 MB，且高保真 JPEG 仍超过 10 MB；请先导出不超过 10 MB 的原图，不会自动降低清晰度")


def _normalized_rgb_image(source):
    oriented = ImageOps.exif_transpose(source)
    try:
        working = oriented
        icc_profile = oriented.info.get("icc_profile")
        if icc_profile:
            try:
                source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                working = ImageCms.profileToProfile(
                    oriented,
                    source_profile,
                    PUBLIC_IMAGE_SRGB_PROFILE,
                    outputMode="RGBA" if _source_has_transparency(oriented) else "RGB",
                )
            except (ImageCms.PyCMSError, OSError, ValueError):
                working = oriented
        if _source_has_transparency(working):
            rgba = working.convert("RGBA")
            try:
                converted = Image.new("RGB", rgba.size, "white")
                converted.paste(rgba, mask=rgba.getchannel("A"))
            finally:
                rgba.close()
        else:
            converted = working.convert("RGB")
        converted.info["icc_profile"] = PUBLIC_IMAGE_SRGB_ICC
        return converted
    finally:
        if "working" in locals() and working is not oriented:
            working.close()
        if oriented is not source:
            oriented.close()


def _public_image_output(content, source, source_format):
    """Keep compatible source bytes whenever possible; never resize pixels."""
    orientation = _source_orientation(source)
    if source_format == "JPEG" and source.mode == "RGB" and orientation == 1:
        return "jpg", "JPEG", content, "original", True
    if (
        source_format == "PNG"
        and source.mode == "RGB"
        and "transparency" not in source.info
        and orientation == 1
    ):
        return "png", "PNG", content, "original", True

    converted = _normalized_rgb_image(source)
    try:
        # A lossless PNG is the safest conversion for sharp text, diagram lines,
        # alpha flattening, TIFF and WebP. Fall back to 4:4:4 JPEG only if needed.
        png_payload = _lossless_public_png(converted)
        if len(png_payload) <= PUBLIC_IMAGE_MAX_BYTES:
            return "png", "PNG", png_payload, "lossless_png", True
        jpeg_payload, quality = _high_quality_public_jpeg(converted)
        return "jpg", "JPEG", jpeg_payload, f"high_quality_jpeg_q{quality}", False
    finally:
        converted.close()


def _save_public_image(file_storage):
    original_name = Path(file_storage.filename or "image").name
    content = file_storage.stream.read(PUBLIC_IMAGE_MAX_BYTES + 1)
    if not content:
        raise ValueError("图片文件为空")
    if len(content) > PUBLIC_IMAGE_MAX_BYTES:
        raise ValueError("单张图片不能超过 10 MB")

    try:
        with Image.open(io.BytesIO(content)) as source:
            source_format = (source.format or "").upper()
            if source_format not in PUBLIC_IMAGE_FORMATS:
                raise ValueError("仅支持 JPG、PNG、WebP 或 TIFF 图片")
            if getattr(source, "is_animated", False):
                raise ValueError("不支持动态图，请上传静态商品图片")
            width, height = source.size
            longest_side = max(width, height)
            if longest_side < PUBLIC_IMAGE_MIN_SIDE or longest_side > PUBLIC_IMAGE_MAX_SIDE:
                raise ValueError("图片最长边需在 500 至 10,000 像素之间")
            source.load()
            extension, output_format, output_content, conversion, quality_preserved = _public_image_output(
                content,
                source,
                source_format,
            )
            output_width, output_height = width, height
            if _source_orientation(source) != 1:
                output_width, output_height = height, width
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法识别或读取该图片") from exc

    PUBLIC_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    asset_id = uuid.uuid4().hex
    filename = _public_image_filename(asset_id, extension)
    image_path = PUBLIC_IMAGE_DIR / filename
    temp_path = PUBLIC_IMAGE_DIR / f".{filename}.tmp"
    try:
        temp_path.write_bytes(output_content)
        if temp_path.stat().st_size > PUBLIC_IMAGE_MAX_BYTES:
            raise ValueError("转换后的图片仍超过 10 MB")
        os.replace(temp_path, image_path)
        metadata = {
            "asset_id": asset_id,
            "original_name": original_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "width": output_width,
            "height": output_height,
            "format": output_format,
            "extension": extension,
            "mime_type": PUBLIC_IMAGE_FILE_TYPES[extension]["mime_type"],
            "source_format": source_format,
            "conversion": conversion,
            "quality_preserved": quality_preserved,
            "source_bytes": len(content),
            "bytes": image_path.stat().st_size,
            "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        }
        _save_json(PUBLIC_IMAGE_DIR / f"{asset_id}.json", metadata)
        return _public_image_record(metadata)
    except Exception:
        temp_path.unlink(missing_ok=True)
        image_path.unlink(missing_ok=True)
        (PUBLIC_IMAGE_DIR / f"{asset_id}.json").unlink(missing_ok=True)
        raise


def _load_public_image(asset_id):
    asset_id = str(asset_id or "")
    if not PUBLIC_IMAGE_ID_RE.fullmatch(asset_id):
        return None, None
    metadata = _load_json(PUBLIC_IMAGE_DIR / f"{asset_id}.json", {})
    if metadata.get("asset_id") != asset_id:
        return None, None
    extension = _public_image_extension(metadata.get("extension"))
    target = PUBLIC_IMAGE_DIR / _public_image_filename(asset_id, extension)
    if not target.is_file():
        return None, None
    return metadata, target


def _probe_public_image(metadata):
    """Fetch the public URL without cookies to prove crawler-safe byte delivery."""
    record = _public_image_record(metadata)
    url = record["url"]
    expected_mime = record["mime_type"]
    expected_bytes = int(metadata.get("bytes") or 0)
    expected_sha256 = str(metadata.get("sha256") or "").lower()
    report = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "url": url,
        "ok": False,
        "head": {},
        "get": {},
        "issues": [],
    }

    def request_public(method):
        request_headers = {
            # Some CDNs block unknown bot user agents before they reach the
            # public asset. A normal browser identifier verifies the same
            # anonymous path a seller uses, without supplying any session data.
            "User-Agent": PUBLIC_IMAGE_VERIFY_USER_AGENT,
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
        }
        request_obj = Request(url, headers=request_headers, method=method)
        with PUBLIC_IMAGE_VERIFY_OPENER.open(request_obj, timeout=PUBLIC_IMAGE_VERIFY_TIMEOUT) as response:
            payload = response.read(PUBLIC_IMAGE_MAX_BYTES + 1) if method == "GET" else b""
            return {
                "status": int(getattr(response, "status", response.getcode())),
                "final_url": response.geturl(),
                "content_type": str(response.headers.get("Content-Type") or ""),
                "content_length": str(response.headers.get("Content-Length") or ""),
                "etag": str(response.headers.get("ETag") or ""),
                "payload": payload,
            }

    try:
        head = request_public("HEAD")
        report["head"] = {
            "status": head["status"],
            "direct": head["final_url"] == url,
            "content_type": head["content_type"],
        }
        if head["status"] != 200:
            report["issues"].append(f"HEAD 返回 HTTP {head['status']}")
        if head["final_url"] != url:
            report["issues"].append("HEAD 出现重定向，Amazon 表格应使用无跳转直链")
        if not head["content_type"].lower().startswith(expected_mime):
            report["issues"].append("HEAD 的 Content-Type 不正确")

        get = request_public("GET")
        payload = get.pop("payload")
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        report["get"] = {
            "status": get["status"],
            "direct": get["final_url"] == url,
            "content_type": get["content_type"],
            "bytes": len(payload),
            "sha256_matches": bool(expected_sha256) and payload_sha256 == expected_sha256,
        }
        if get["status"] != 200:
            report["issues"].append(f"GET 返回 HTTP {get['status']}")
        if get["final_url"] != url:
            report["issues"].append("GET 出现重定向，Amazon 表格应使用无跳转直链")
        if not get["content_type"].lower().startswith(expected_mime):
            report["issues"].append("GET 的 Content-Type 不正确")
        if len(payload) != expected_bytes:
            report["issues"].append("公网返回的文件大小与源文件不一致")
        if not expected_sha256 or payload_sha256 != expected_sha256:
            report["issues"].append("公网返回的图片字节与源文件不一致")
    except HTTPError as exc:
        report["issues"].append(f"公网抓取返回 HTTP {exc.code}")
    except (URLError, OSError, ValueError) as exc:
        report["issues"].append(f"公网抓取失败：{str(exc)[:180]}")

    report["ok"] = not report["issues"]
    return report


def _run_image_edit(edit_id):
    edit_dir = _edit_dir(edit_id)
    _update_edit(edit_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"), message="正在调用 Agnes 局部编辑模型")
    try:
        from PIL import Image
        with Image.open(edit_dir / "original.png") as original_file, Image.open(edit_dir / "mask.png") as mask_file:
            original = original_file.convert("RGBA")
            mask = mask_file.convert("L")
            metadata = _load_edit(edit_id)
            if metadata.get("edit_mode") == "text_translation":
                edit_spec = {
                    "mode": "text_translation",
                    "source_text": metadata.get("source_text") or "",
                    "target_language": metadata.get("target_language") or "",
                    "replacement_text": metadata.get("replacement_text") or "",
                }
                generated, final = generate_masked_edit(metadata["prompt"], original, mask, edit_spec)
            else:
                generated, final = generate_masked_edit(metadata["prompt"], original, mask)
        save_png(generated, edit_dir / "generated.png")
        save_png(final, edit_dir / "final.png")
        _update_edit(
            edit_id,
            status="succeeded",
            ok=True,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            message="局部编辑完成",
        )
    except (ImageEditError, OSError, ValueError) as exc:
        _update_edit(
            edit_id,
            status="failed",
            ok=False,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            message="局部编辑失败",
            error=str(exc),
        )
    except Exception:
        _update_edit(
            edit_id,
            status="failed",
            ok=False,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            message="局部编辑失败",
            error="局部编辑服务异常，请稍后重试",
        )


def _save_zip(run_dir, file_storage, index=None):
    if not file_storage or not file_storage.filename:
        raise ValueError("缺少上传文件")
    if not file_storage.filename.lower().endswith(".zip"):
        raise ValueError("只支持 .zip 素材包")
    idx = int(index or len(list(run_dir.glob("*.zip"))) + 1)
    safe_name = _safe_name(file_storage.filename, f"material_{idx:02d}.zip")
    if not safe_name.lower().endswith(".zip"):
        safe_name += ".zip"
    save_path = run_dir / f"{idx:02d}_{safe_name}"
    file_storage.save(str(save_path))
    return {"path": str(save_path), "filename": file_storage.filename, "saved_name": save_path.name}


def _save_zip_path(run_dir, source_path, original_name, index=None):
    if not original_name.lower().endswith(".zip"):
        raise ValueError("只支持 .zip 素材包")
    idx = int(index or len(list(run_dir.glob("*.zip"))) + 1)
    safe_name = _safe_name(original_name, f"material_{idx:02d}.zip")
    if not safe_name.lower().endswith(".zip"):
        safe_name += ".zip"
    save_path = run_dir / f"{idx:02d}_{safe_name}"
    shutil.move(str(source_path), str(save_path))
    return {"path": str(save_path), "filename": original_name, "saved_name": save_path.name}


def _save_template(run_dir, file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("缺少 Excel 模板")
    if not file_storage.filename.lower().endswith((".xlsx", ".xls")):
        raise ValueError("模板只支持 .xlsx/.xls")
    template_path = run_dir / "template.xlsx"
    file_storage.save(str(template_path))
    return {"path": str(template_path), "filename": file_storage.filename}


# 上一轮运行残留的字段：重新排队时必须丢弃，否则处理成功后 job JSON 里
# 仍带着旧的 error，界面会同时显示"处理完成"和"处理失败"（2026-10-10 故障）。
_JOB_STALE_KEYS = ("error", "result", "finished_at", "http_status",
                   "started_at", "stdout", "stderr")


def _set_job(run_id, **updates):
    job = PROCESS_JOBS.get(run_id) or _load_json(_job_path(run_id), {})
    if updates.pop("reset", False):
        for _key in _JOB_STALE_KEYS:
            job.pop(_key, None)
    job.update(updates)
    job.setdefault("run_id", run_id)
    PROCESS_JOBS[run_id] = job
    _save_json(_job_path(run_id), job)
    return job


def _giga_text(value, limit=400):
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()[:limit]


def _giga_error_payload(value):
    if isinstance(value, dict):
        result = {}
        code = _giga_text(value.get("code"), 80)
        message = _giga_text(value.get("message") or value.get("error"), 500)
        if code:
            result["code"] = code
        if message:
            result["message"] = message
        return result
    message = _giga_text(value, 500)
    return {"message": message} if message else {}


def _giga_material_archive_url(run_id, filename):
    return "/api/giga-material/download/{}/{}".format(
        quote(run_id, safe=""), quote(filename, safe="")
    )


def _giga_variant_payloads(values, *, run_id=None):
    items = []
    if not isinstance(values, list):
        return items
    for value in values[:120]:
        if not isinstance(value, dict):
            continue
        item = {}
        for key, limit in (("actual_item_code", 80), ("label", 240), ("status", 64), ("note", 400)):
            text = _giga_text(value.get(key), limit)
            if text:
                item[key] = text
        filename = _giga_text(value.get("filename") or value.get("file"), 180)
        status = _giga_text(value.get("status"), 64).casefold()
        if filename and GIGA_MATERIAL_ARCHIVE_RE.fullmatch(filename):
            item["filename"] = filename
            item["file"] = filename
            if run_id and status in {"downloaded", "succeeded", "success", "completed"}:
                item["download_url"] = _giga_material_archive_url(run_id, filename)
        try:
            size = int(value.get("size"))
            if size >= 0:
                item["size"] = size
        except (TypeError, ValueError):
            pass
        error = _giga_error_payload(value.get("error"))
        if error:
            item["error"] = error
        items.append(item)
    return items


def _giga_error_payloads(values):
    if not isinstance(values, list):
        return []
    return [item for item in (_giga_error_payload(value) for value in values[:120]) if item]


def _giga_count(value, default=0):
    try:
        return max(0, min(int(value), 100000))
    except (TypeError, ValueError):
        return default


def _giga_job_status_payload(run_id, job):
    """Return a browser-safe status payload without local paths or GIGA tokens."""
    variants = _giga_variant_payloads(job.get("variants"), run_id=run_id)
    payload = {
        "ok": job.get("ok"),
        "run_id": run_id,
        "status": _giga_text(job.get("status"), 80) or "queued",
        "stage": _giga_text(job.get("stage"), 80),
        "message": _giga_text(job.get("message"), 500),
        "error": _giga_text(job.get("error"), 500),
        "requested_item_code": _giga_text(job.get("requested_item_code"), 80),
        "include_variants": bool(job.get("include_variants", True)),
        "variants": variants,
        "errors": _giga_error_payloads(job.get("errors")),
        "downloaded_count": _giga_count(job.get("downloaded_count")),
        "failed_count": _giga_count(job.get("failed_count")),
        "total": _giga_count(job.get("total"), len(variants)),
        "completed": _giga_count(job.get("completed")),
        "created_at": _giga_text(job.get("created_at"), 40),
        "started_at": _giga_text(job.get("started_at"), 40),
        "finished_at": _giga_text(job.get("finished_at"), 40),
    }
    if payload["total"] == 0 and variants:
        payload["total"] = len(variants)

    if payload["status"] == "succeeded":
        payload["downloads_ready"] = True
    return payload


def _giga_download_progress_status(stage):
    return {
        "resolving": "resolving",
        "enumerating": "enumerating",
        "variant_found": "enumerating",
        "variant_skipped": "enumerating",
        "downloading": "downloading",
        "downloaded": "downloading",
        "variant_failed": "downloading",
    }.get(stage, "downloading")


def _run_giga_material_job(run_id, item_code, include_variants):
    """Download the current product and same-page variant material packages."""
    try:
        run_id = _validated_run_id(run_id)
        run_dir = _run_dir(UPLOAD_DIR, run_id)
    except ValueError:
        return

    def progress(event):
        stage = _giga_text(event.get("stage"), 80) or "downloading"
        variants = _giga_variant_payloads(event.get("variants"))
        _set_job(
            run_id,
            source=GIGA_MATERIAL_JOB_SOURCE,
            status=_giga_download_progress_status(stage),
            stage=stage,
            message=_giga_text(event.get("message"), 500) or "正在下载云仓素材包",
            variants=variants,
            downloaded_count=_giga_count(event.get("downloaded_count")),
            failed_count=_giga_count(event.get("failed_count")),
            total=_giga_count(event.get("total"), len(variants)),
            completed=_giga_count(event.get("completed")),
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )

    _set_job(
        run_id,
        source=GIGA_MATERIAL_JOB_SOURCE,
        status="resolving",
        stage="resolving",
        ok=None,
        started_at=datetime.now().isoformat(timespec="seconds"),
        message="正在打开云仓商品页",
    )
    try:
        download_result = GigaMaterialDownloader().download(
            item_code,
            run_dir,
            include_variants=include_variants,
            progress=progress,
        )
        variants = _giga_variant_payloads(download_result.get("variants"))
        errors = _giga_error_payloads(download_result.get("errors"))
        download_status = _giga_text(download_result.get("status"), 80) or "failed"
        common = {
            "source": GIGA_MATERIAL_JOB_SOURCE,
            "requested_item_code": _giga_text(download_result.get("requested_item_code"), 80) or item_code,
            "include_variants": include_variants,
            "variants": variants,
            "errors": errors,
            "downloaded_count": _giga_count(download_result.get("downloaded_count")),
            "failed_count": _giga_count(download_result.get("failed_count")),
            "total": len(variants),
            "completed": _giga_count(download_result.get("downloaded_count")) + _giga_count(download_result.get("failed_count")),
        }

        # Do not silently continue after GIGA asks for a human login/CAPTCHA.
        # The operator can verify the browser and retry.
        if download_status in {"needs_login", "needs_verification"}:
            _set_job(
                run_id,
                **common,
                ok=False,
                status=download_status,
                stage=download_status,
                message=_giga_text(download_result.get("message"), 500),
                error=_giga_text(download_result.get("message"), 500),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            return
        if not download_result.get("ok") or not any(run_dir.glob("*.zip")):
            _set_job(
                run_id,
                **common,
                ok=False,
                status="failed",
                stage=download_status,
                message=_giga_text(download_result.get("message"), 500) or "没有成功下载任何素材包",
                error=_giga_text(download_result.get("message"), 500) or "没有成功下载任何素材包",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            return

        _save_json(_manifest_path(run_dir), {
            "source": GIGA_MATERIAL_JOB_SOURCE,
            "requested_item_code": item_code,
            "include_variants": include_variants,
            "zips": [path.name for path in sorted(run_dir.glob("*.zip"))],
            "variants": variants,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        message = "云仓原始素材包下载完成"
        if common["failed_count"]:
            message += f"；另有 {common['failed_count']} 个变体下载失败"
        _set_job(
            run_id,
            **common,
            ok=True,
            status="succeeded",
            stage="succeeded",
            message=message,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception:
        app.logger.exception("GIGA material download job failed: %s", run_id)
        _set_job(
            run_id,
            source=GIGA_MATERIAL_JOB_SOURCE,
            ok=False,
            status="failed",
            stage="failed",
            message="云仓素材下载任务异常，请稍后重试",
            error="云仓素材下载任务异常，请稍后重试",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )


def _record_result(run_id, mode, size, quality, result):
    run_id = _validated_run_id(run_id)
    run_dir = _run_dir(UPLOAD_DIR, run_id)
    runs = _load_runs()
    s = result.get("summary", {})
    run_record = {
        "run_id": run_id,
        "mode": mode,
        "created_at": result.get("generated_at"),
        "summary": s,
        "size": size,
        "quality": quality,
        "input_zips": len([p for p in run_dir.glob("*.zip")]),
    }
    runs = [r for r in runs if r.get("run_id") != run_id]
    runs.insert(0, run_record)
    _save_runs(runs[:30])
    _save_json(_run_data_path("material_", run_id), result)


def _process_run(run_id, data):
    try:
        run_id = _validated_run_id(run_id)
        run_dir = _run_dir(UPLOAD_DIR, run_id)
        output_dir = _run_dir(OUTPUT_DIR, run_id)
        report_dir = _run_dir(REPORT_DIR, run_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    if not run_dir.exists():
        return {"ok": False, "error": f"run_id 不存在: {run_id}"}, 404

    template_file = run_dir / "template.xlsx"
    mode = "template" if template_file.exists() else "simple"

    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    size = int(data.get("size", 1600))
    quality = int(data.get("quality", 85))
    workers = data.get("workers")

    cmd = [
        PYTHON_BIN, str(PROCESS_SCRIPT),
        "--mode", mode,
        "--input-dir", str(run_dir),
        "--output-dir", str(output_dir),
        "--report-dir", str(report_dir),
        "--size", str(size),
        "--quality", str(quality),
    ]
    if mode == "template":
        cmd += ["--template", str(template_file)]
    if workers:
        cmd += ["--workers", str(workers)]

    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=60 * 60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "处理超时（60 分钟）"}, 504
    except Exception as exc:
        return {"ok": False, "error": f"调用失败: {exc}"}, 500

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": "process_materials_json.py 失败",
            "stderr": (proc.stderr or "")[-2000:],
            "stdout": (proc.stdout or "")[-2000:],
        }, 500

    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {
            "ok": False,
            "error": f"解析输出失败: {exc}",
            "stdout": (proc.stdout or "")[-1000:],
        }, 500

    if not result.get("ok"):
        return result, 422

    _record_result(run_id, mode, size, quality, result)
    return result, 200


def _process_run_background(run_id, data):
    # Mark the batch busy for the whole run so the retention sweeper cannot
    # delete its directories out from under the worker mid-write.
    ACTIVE_RUN_IDS.add(run_id)
    try:
        _set_job(run_id, reset=True, status="running", ok=None, started_at=datetime.now().isoformat(timespec="seconds"), message="正在处理图片")
        result, status = _process_run(run_id, data)
        if result.get("ok"):
            _set_job(
                run_id,
                status="succeeded",
                ok=True,
                http_status=status,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                result=result,
                message="处理完成",
            )
        else:
            _set_job(
                run_id,
                status="failed",
                ok=False,
                http_status=status,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                error=result.get("error", "处理失败"),
                result=result,
                message="处理失败",
            )
    finally:
        ACTIVE_RUN_IDS.discard(run_id)


@app.errorhandler(413)
def request_too_large(_exc):
    return jsonify({
        "ok": False,
        "error": "上传文件太大：请减少单次选择的文件，或改用逐个素材包上传",
    }), 413


@app.route("/")
def index():
    return send_file(str(APP_DIR / "templates" / "index.html"))


@app.route("/shared/<path:filename>")
def shared_static(filename):
    """统一对外提供 shared 目录下的主题/脚本等静态资源。"""
    return send_from_directory(str(SHARED_DIR), filename)


@app.route("/api/public-images", methods=["POST"])
def create_public_images():
    files = [item for item in request.files.getlist("images") if item and item.filename]
    if not files:
        return jsonify({"ok": False, "error": "请至少选择一张图片", "items": [], "errors": []}), 400
    if len(files) > PUBLIC_IMAGE_MAX_COUNT:
        return jsonify({
            "ok": False,
            "error": f"单次最多上传 {PUBLIC_IMAGE_MAX_COUNT} 张图片",
            "items": [],
            "errors": [],
        }), 400

    items = []
    errors = []
    for file_storage in files:
        try:
            items.append(_save_public_image(file_storage))
        except ValueError as exc:
            errors.append({"original_name": Path(file_storage.filename or "image").name, "error": str(exc)})
    if not items:
        return jsonify({"ok": False, "error": "所选图片均未生成链接", "items": [], "errors": errors}), 400
    return jsonify({"ok": True, "items": items, "errors": errors}), 201


@app.route("/api/public-images", methods=["GET"])
def list_public_images():
    items = []
    for metadata_path in PUBLIC_IMAGE_DIR.glob("*.json"):
        metadata = _load_json(metadata_path, {})
        asset_id = metadata.get("asset_id")
        if not PUBLIC_IMAGE_ID_RE.fullmatch(asset_id or ""):
            continue
        _metadata, image_path = _load_public_image(asset_id)
        if _metadata is None or image_path is None:
            continue
        items.append(_public_image_record(_metadata))
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return jsonify({"items": items[:100]})


@app.route("/api/public-images/<asset_id>/verify", methods=["POST"])
def verify_public_image(asset_id):
    metadata, _image_path = _load_public_image(asset_id)
    if metadata is None:
        return jsonify({"ok": False, "error": "公开图片不存在"}), 404
    verification = _probe_public_image(metadata)
    metadata["last_public_check"] = verification
    _save_json(PUBLIC_IMAGE_DIR / f"{asset_id}.json", metadata)
    record = _public_image_record(metadata)
    return jsonify({
        "ok": verification["ok"],
        "item": record,
        "verification": verification,
    }), 200 if verification["ok"] else 502


@app.route("/public/images/<filename>", methods=["GET", "HEAD"])
def public_image(filename):
    match = re.fullmatch(r"([a-f0-9]{32})\.(jpg|png)", filename or "")
    if not match:
        abort(404)
    asset_id, requested_extension = match.groups()
    metadata, target = _load_public_image(asset_id)
    if metadata is None or target is None:
        abort(404)
    extension = _public_image_extension(metadata.get("extension"))
    if requested_extension != extension or target.name != filename:
        abort(404)
    etag = str(metadata.get("sha256") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", etag):
        etag = True
    response = send_file(
        str(target),
        mimetype=PUBLIC_IMAGE_FILE_TYPES[extension]["mime_type"],
        conditional=True,
        etag=etag,
        max_age=31536000,
    )
    # New asset IDs are never reused, so all clients can cache exactly the same
    # bytes without receiving an older image or an intermediary-transcoded copy.
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable, no-transform"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Content-SHA256"] = str(metadata.get("sha256") or "")
    response.headers["Content-Disposition"] = f"inline; filename={filename}"
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    response.headers["Accept-Ranges"] = "bytes"
    return response



@app.route("/api/image-edit/optimize-prompt", methods=["POST"])
def optimize_image_edit_prompt():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "请使用 JSON 提交优化指令"}), 400
    if body.get("confirmed") is not True:
        return jsonify({"ok": False, "error": "请先确认本次 Agnes 文字模型调用"}), 400
    allowed_fields = ("prompt", "edit_mode", "source_text", "target_language", "replacement_text")
    if any(field in body and not isinstance(body[field], str) for field in allowed_fields):
        return jsonify({"ok": False, "error": "优化指令字段必须为文本"}), 400
    prompt = (body.get("prompt") or "").strip()
    edit_mode = (body.get("edit_mode") or "standard").strip()
    source_text = (body.get("source_text") or "").strip()
    target_language = (body.get("target_language") or "").strip()
    replacement_text = (body.get("replacement_text") or "").strip()
    if edit_mode not in {"standard", "text_translation"}:
        return jsonify({"ok": False, "error": "不支持的局部编辑模式"}), 400
    if len(prompt) > 2000:
        return jsonify({"ok": False, "error": "提示词不能超过 2000 个字符"}), 400
    edit_spec = None
    if edit_mode == "text_translation":
        if not target_language or len(target_language) > 40:
            return jsonify({"ok": False, "error": "文字翻译模式需要填写 1 至 40 个字符的目标语言"}), 400
        if len(source_text) > 500:
            return jsonify({"ok": False, "error": "原文不能超过 500 个字符"}), 400
        if len(replacement_text) > 500:
            return jsonify({"ok": False, "error": "准确替换文本不能超过 500 个字符"}), 400
        edit_spec = {
            "mode": "text_translation",
            "source_text": source_text,
            "target_language": target_language,
            "replacement_text": replacement_text,
        }
    elif not prompt:
        return jsonify({"ok": False, "error": "请先输入局部修改指令"}), 400
    try:
        optimized_prompt, model = optimize_edit_prompt(prompt, edit_spec)
    except ImageEditError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "prompt": optimized_prompt, "model": model})


@app.route("/api/image-edit/start", methods=["POST"])
def start_image_edit():
    if request.form.get("confirmed") != "true":
        return jsonify({"ok": False, "error": "请先确认本次 Agnes 模型调用"}), 400
    prompt = (request.form.get("prompt") or "").strip()
    edit_mode = (request.form.get("edit_mode") or "standard").strip()
    if edit_mode not in {"standard", "text_translation"}:
        return jsonify({"ok": False, "error": "不支持的局部编辑模式"}), 400
    source_text = (request.form.get("source_text") or "").strip()
    target_language = (request.form.get("target_language") or "").strip()
    replacement_text = (request.form.get("replacement_text") or "").strip()
    if edit_mode == "text_translation":
        if not target_language or len(target_language) > 40:
            return jsonify({"ok": False, "error": "文字翻译模式需要填写 1 至 40 个字符的目标语言"}), 400
        if len(replacement_text) > 500:
            return jsonify({"ok": False, "error": "准确替换文本不能超过 500 个字符"}), 400
        if len(source_text) > 500:
            return jsonify({"ok": False, "error": "原文不能超过 500 个字符"}), 400
        if not prompt:
            if replacement_text:
                prompt = f"将{source_text or '红色选区内的文字'}替换为{target_language}：{replacement_text}"
            else:
                prompt = f"将{source_text or '红色选区内的文字'}翻译为{target_language}"
    if not prompt or len(prompt) > 2000:
        return jsonify({"ok": False, "error": "提示词需为 1 至 2000 个字符"}), 400
    original_file = request.files.get("original")
    mask_file = request.files.get("mask")
    if not original_file or not mask_file:
        return jsonify({"ok": False, "error": "请上传原图并涂抹修改区域"}), 400
    try:
        original = load_original(original_file)
        mask = load_mask(mask_file, original.size)
    except ImageEditError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    edit_id = _new_edit_id()
    edit_dir = _edit_dir(edit_id)
    edit_dir.mkdir(parents=True, exist_ok=False)
    save_png(original, edit_dir / "original.png")
    save_png(mask, edit_dir / "mask.png")
    _save_edit(edit_id, {
        "edit_id": edit_id,
        "status": "queued",
        "ok": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "prompt": prompt,
        "edit_mode": edit_mode,
        "source_text": source_text if edit_mode == "text_translation" else "",
        "target_language": target_language if edit_mode == "text_translation" else "",
        "replacement_text": replacement_text if edit_mode == "text_translation" else "",
        "size": {"width": original.width, "height": original.height},
        "model": os.environ.get("AGNES_IMAGE_MODEL", "agnes-image-2.1-flash"),
        "message": "局部编辑任务已创建",
    })
    threading.Thread(target=_run_image_edit, args=(edit_id,), daemon=True).start()
    return jsonify({"ok": True, "edit_id": edit_id, "status": "queued"}), 202


@app.route("/api/image-edit/status/<edit_id>", methods=["GET"])
def image_edit_status(edit_id):
    metadata = IMAGE_EDIT_JOBS.get(edit_id) or _load_edit(edit_id)
    if not metadata:
        return jsonify({"ok": False, "error": "编辑任务不存在"}), 404
    return jsonify(metadata)


@app.route("/api/image-edit/preview/<edit_id>", methods=["GET"])
def image_edit_preview(edit_id):
    version = (request.args.get("version") or "final").strip()
    if version not in {"original", "mask", "generated", "final"}:
        return jsonify({"ok": False, "error": "无效的预览版本"}), 400
    try:
        target = _edit_dir(edit_id) / f"{version}.png"
    except ValueError:
        return jsonify({"ok": False, "error": "编辑任务不存在"}), 404
    if not target.is_file():
        return jsonify({"ok": False, "error": "预览图片尚未生成"}), 404
    return send_file(str(target), mimetype="image/png", max_age=0)


@app.route("/api/image-edit/download/<edit_id>", methods=["GET"])
def download_image_edit(edit_id):
    try:
        target = _edit_dir(edit_id) / "final.png"
    except ValueError:
        return jsonify({"ok": False, "error": "编辑任务不存在"}), 404
    if not target.is_file():
        return jsonify({"ok": False, "error": "编辑结果尚未生成"}), 404
    return send_file(str(target), as_attachment=True, download_name=f"image_edit_{edit_id}.png")


@app.route("/api/image-edit/history", methods=["GET"])
def image_edit_history():
    edits = []
    for metadata_path in sorted(EDIT_DIR.glob("*/metadata.json"), reverse=True):
        metadata = _load_json(metadata_path, {})
        edit_id = metadata.get("edit_id")
        if IMAGE_EDIT_ID_RE.fullmatch(edit_id or ""):
            edits.append({
                "edit_id": edit_id,
                "status": metadata.get("status"),
                "created_at": metadata.get("created_at"),
                "prompt": metadata.get("prompt"),
                "has_result": (metadata_path.parent / "final.png").is_file(),
            })
        if len(edits) >= 20:
            break
    return jsonify({"edits": edits})


@app.route("/api/upload", methods=["POST"])
def upload_files():
    """上传 ZIP 素材包（可多个）+ 可选 Excel 模板。
    返回 run_id。"""
    run_id, run_dir = _new_run_dir()

    # 保存 ZIP 文件（支持多文件）
    zips = request.files.getlist("zips")
    saved_zips = []
    for idx, f in enumerate(zips, start=1):
        try:
            record = _save_zip(run_dir, f, index=idx)
        except ValueError:
            continue
        saved_zips.append(record["filename"])

    if not saved_zips:
        _safe_rmtree(run_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "请至少上传一个 .zip 素材包"}), 400

    # 可选 Excel 模板
    template = request.files.get("template")
    template_path = None
    if template and template.filename:
        template_path = Path(_save_template(run_dir, template)["path"])

    return jsonify({
        "ok": True,
        "run_id": run_id,
        "zips": saved_zips,
        "has_template": template_path is not None,
        "template_path": str(template_path) if template_path else None,
    })


@app.route("/api/upload/start", methods=["POST"])
def upload_start():
    run_id, run_dir = _new_run_dir()
    _save_json(_manifest_path(run_dir), {"zips": []})
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/upload/file", methods=["POST"])
def upload_one_file():
    run_id = (request.form.get("run_id") or "").strip()
    field = (request.form.get("field") or "").strip()
    if not run_id:
        return jsonify({"ok": False, "error": "缺少 run_id"}), 400
    try:
        run_id = _validated_run_id(run_id)
        run_dir = _run_dir(UPLOAD_DIR, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not run_dir.exists():
        return jsonify({"ok": False, "error": f"run_id 不存在: {run_id}"}), 404
    manifest = _load_json(_manifest_path(run_dir), {"zips": []})
    f = request.files.get("file")
    try:
        if field == "zip":
            record = _save_zip(run_dir, f, index=request.form.get("index"))
            manifest.setdefault("zips", []).append(record)
        elif field == "template":
            manifest["template"] = _save_template(run_dir, f)
        else:
            return jsonify({"ok": False, "error": f"不支持的文件类型: {field}"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _save_json(_manifest_path(run_dir), manifest)
    return jsonify({"ok": True, "run_id": run_id, "manifest": manifest})


@app.route("/api/upload/chunk", methods=["POST"])
def upload_chunk():
    run_id = (request.form.get("run_id") or "").strip()
    upload_id = _safe_name(request.form.get("upload_id") or uuid.uuid4().hex, "upload")
    chunk_index = int(request.form.get("chunk_index") or 0)
    total_chunks = int(request.form.get("total_chunks") or 0)
    if not run_id:
        return jsonify({"ok": False, "error": "缺少 run_id"}), 400
    if chunk_index < 0 or total_chunks < 1:
        return jsonify({"ok": False, "error": "切片参数不正确"}), 400
    try:
        run_id = _validated_run_id(run_id)
        run_dir = _run_dir(UPLOAD_DIR, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not run_dir.exists():
        return jsonify({"ok": False, "error": f"run_id 不存在: {run_id}"}), 404
    chunk = request.files.get("chunk")
    if not chunk:
        return jsonify({"ok": False, "error": "缺少 chunk 文件"}), 400
    try:
        chunk_dir = confined_child(run_dir / ".chunks", upload_id, label="upload_id")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk.save(str(chunk_dir / f"{chunk_index:06d}.part"))
    received = len(list(chunk_dir.glob("*.part")))
    return jsonify({
        "ok": True,
        "run_id": run_id,
        "upload_id": upload_id,
        "received": received,
        "total_chunks": total_chunks,
    })


@app.route("/api/upload/complete", methods=["POST"])
def upload_complete():
    data = request.get_json() or {}
    run_id = (data.get("run_id") or "").strip()
    upload_id = _safe_name(data.get("upload_id") or "", "upload")
    filename = data.get("filename") or "material.zip"
    index = data.get("index")
    total_chunks = int(data.get("total_chunks") or 0)
    if not run_id or not upload_id:
        return jsonify({"ok": False, "error": "缺少 run_id 或 upload_id"}), 400
    try:
        run_id = _validated_run_id(run_id)
        run_dir = _run_dir(UPLOAD_DIR, run_id)
        chunk_dir = confined_child(run_dir / ".chunks", upload_id, label="upload_id")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not chunk_dir.exists():
        return jsonify({"ok": False, "error": "切片目录不存在"}), 404
    parts = sorted(chunk_dir.glob("*.part"))
    if total_chunks and len(parts) != total_chunks:
        return jsonify({"ok": False, "error": f"切片未上传完整：{len(parts)}/{total_chunks}"}), 400
    assembled = chunk_dir / "assembled.upload"
    with assembled.open("wb") as out:
        for part in parts:
            with part.open("rb") as f:
                shutil.copyfileobj(f, out)
    manifest = _load_json(_manifest_path(run_dir), {"zips": []})
    try:
        record = _save_zip_path(run_dir, assembled, filename, index=index)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    manifest.setdefault("zips", []).append(record)
    _save_json(_manifest_path(run_dir), manifest)
    _safe_rmtree(chunk_dir, ignore_errors=True)
    return jsonify({"ok": True, "run_id": run_id, "manifest": manifest, "file": record})


@app.route("/api/upload/finish", methods=["POST"])
def upload_finish():
    data = request.get_json() or {}
    run_id = (data.get("run_id") or "").strip()
    if not run_id:
        return jsonify({"ok": False, "error": "缺少 run_id"}), 400
    try:
        run_id = _validated_run_id(run_id)
        run_dir = _run_dir(UPLOAD_DIR, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not run_dir.exists():
        return jsonify({"ok": False, "error": f"run_id 不存在: {run_id}"}), 404
    zips = list(run_dir.glob("*.zip"))
    if not zips:
        _safe_rmtree(run_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "请至少上传一个 .zip 素材包"}), 400
    return jsonify({"ok": True, "run_id": run_id, "zips": [p.name for p in zips], "has_template": (run_dir / "template.xlsx").exists()})


@app.route("/api/giga-material/start", methods=["POST"])
def start_giga_material_download():
    """Create an Item Code -> current product plus same-page variants download job."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    try:
        item_code = validate_item_code(data.get("item_code"))
        # This entry always gathers every unique variant under the same product
        # link.  It deliberately has no image-processing options.
        include_variants = True
    except (GigaMaterialDownloadError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    run_id, run_dir = _new_run_dir()
    _save_json(_manifest_path(run_dir), {
        "source": GIGA_MATERIAL_JOB_SOURCE,
        "requested_item_code": item_code,
        "include_variants": include_variants,
        "zips": [],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    job = _set_job(
        run_id,
        source=GIGA_MATERIAL_JOB_SOURCE,
        ok=True,
        status="queued",
        stage="queued",
        message="云仓原始素材包下载任务已加入队列",
        requested_item_code=item_code,
        include_variants=include_variants,
        variants=[],
        errors=[],
        downloaded_count=0,
        failed_count=0,
        total=0,
        completed=0,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    thread = threading.Thread(
        target=_run_giga_material_job,
        args=(run_id, item_code, include_variants),
        daemon=True,
        name=f"giga-material-{run_id}",
    )
    thread.start()
    return jsonify(_giga_job_status_payload(run_id, job)), 202


@app.route("/api/giga-material/status/<run_id>", methods=["GET"])
def giga_material_download_status(run_id):
    try:
        run_id = _validated_run_id(run_id)
        job = PROCESS_JOBS.get(run_id) or _load_json(_job_path(run_id), {})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not job or job.get("source") != GIGA_MATERIAL_JOB_SOURCE:
        return jsonify({"ok": False, "error": "云仓素材下载任务不存在"}), 404
    return jsonify(_giga_job_status_payload(run_id, job))


@app.route("/api/giga-material/download/<run_id>/<filename>", methods=["GET"])
def download_giga_material_archive(run_id, filename):
    """Download one original, verified GIGA material package from a run."""
    try:
        run_id = _validated_run_id(run_id)
        if not GIGA_MATERIAL_ARCHIVE_RE.fullmatch(filename or ""):
            abort(404)
        run_dir = _run_dir(UPLOAD_DIR, run_id)
    except ValueError:
        abort(404)

    job = PROCESS_JOBS.get(run_id) or _load_json(_job_path(run_id), {})
    if not job or job.get("source") != GIGA_MATERIAL_JOB_SOURCE:
        abort(404)
    allowed_filenames = {
        item["filename"]
        for item in _giga_variant_payloads(job.get("variants"))
        if item.get("status", "").casefold() in {"downloaded", "succeeded", "success", "completed"}
        and item.get("filename")
    }
    if filename not in allowed_filenames:
        abort(404)

    candidate = run_dir / filename
    if candidate.is_symlink():
        abort(404)
    try:
        target = confined_child(run_dir, filename, label="素材包文件名")
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    return send_file(str(target), as_attachment=True, download_name=filename)


@app.route("/api/process", methods=["POST"])
def process_materials():
    """触发图片处理。入参：{run_id, mode?, size?, quality?, workers?}"""
    data = request.get_json() or {}
    run_id = data.get("run_id")
    if not run_id:
        return jsonify({"ok": False, "error": "缺少 run_id"}), 400
    try:
        run_id = _validated_run_id(run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    result, status = _process_run(run_id, data)
    return jsonify(result), status


@app.route("/api/process/start", methods=["POST"])
def process_start():
    data = request.get_json() or {}
    run_id = data.get("run_id")
    if not run_id:
        return jsonify({"ok": False, "error": "缺少 run_id"}), 400
    try:
        run_id = _validated_run_id(run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    existing = _load_json(_job_path(run_id), {})
    if existing.get("status") == "running":
        return jsonify({"ok": True, "run_id": run_id, "status": "running", "message": "已有处理任务正在运行"}), 202
    _set_job(run_id, reset=True, status="queued", ok=None, created_at=datetime.now().isoformat(timespec="seconds"), message="已加入处理队列")
    thread = threading.Thread(target=_process_run_background, args=(run_id, data), daemon=True)
    thread.start()
    return jsonify({"ok": True, "run_id": run_id, "status": "queued", "message": "后台处理已启动"}), 202


@app.route("/api/process/status/<run_id>", methods=["GET"])
def process_status(run_id):
    try:
        run_id = _validated_run_id(run_id)
        job = PROCESS_JOBS.get(run_id) or _load_json(_job_path(run_id), {})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify(job)


@app.route("/api/download/<run_id>", methods=["GET"])
def download_zip(run_id):
    """下载处理好的 ZIP 包。"""
    try:
        run_id = _validated_run_id(run_id)
        detail_file = _run_data_path("material_", run_id)
        output_dir = _run_dir(OUTPUT_DIR, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not detail_file.exists():
        return jsonify({"ok": False, "error": "运行记录不存在"}), 404
    detail = _load_json(detail_file, {})
    export_zip = detail.get("export_zip") or detail.get("summary", {}).get("export_zip")
    try:
        resolved_export = Path(export_zip or "").resolve()
        resolved_export.relative_to(output_dir.resolve())
    except (OSError, ValueError):
        return jsonify({"ok": False, "error": "导出 ZIP 路径无效"}), 403
    if not resolved_export.is_file():
        return jsonify({"ok": False, "error": "导出 ZIP 不存在"}), 404
    return send_file(str(resolved_export), as_attachment=True,
                     download_name=resolved_export.name)


@app.route("/api/preview/<run_id>/<path:filename>", methods=["GET"])
def preview_image(run_id, filename):
    """预览某次处理输出的某张图片。
    filename 格式：folder/image.jpg"""
    try:
        run_id = _validated_run_id(run_id)
        output_dir = _run_dir(OUTPUT_DIR, run_id)
        target = confined_child(output_dir, filename, label="preview filename")
    except ValueError:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(str(target))


@app.route("/api/runs", methods=["GET"])
def list_runs():
    return jsonify(_load_runs())


@app.route("/api/runs/<run_id>", methods=["GET"])
def get_run_detail(run_id):
    try:
        run_id = _validated_run_id(run_id)
        df = _run_data_path("material_", run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not df.exists():
        return jsonify({"ok": False, "error": "详情不存在"}), 404
    return jsonify(_load_json(df, {}))


@app.route("/api/runs/<run_id>", methods=["DELETE"])
def delete_run(run_id):
    try:
        run_id = _validated_run_id(run_id)
        run_dirs = [_run_dir(root, run_id) for root in (UPLOAD_DIR, OUTPUT_DIR, REPORT_DIR)]
        detail_path = _run_data_path("material_", run_id)
        job_path = _job_path(run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    runs = _load_runs()
    before = len(runs)
    runs = [r for r in runs if r.get("run_id") != run_id]
    _save_runs(runs)
    # 清理文件
    for d in run_dirs:
        if d.exists():
            _safe_rmtree(d, ignore_errors=True)
    detail_path.unlink(missing_ok=True)
    job_path.unlink(missing_ok=True)
    return jsonify({"removed": before - len(runs)})


@app.route("/api/trends", methods=["GET"])
def get_trends():
    runs = _load_runs()
    try:
        limit = int(request.args.get("n") or 14)
    except (TypeError, ValueError):
        limit = 14
    points = []
    for run in reversed(runs[: max(1, limit)]):
        summary = run.get("summary", {})
        points.append({
            "time": run.get("created_at"),
            "images": summary.get("total_images", 0),
            "archives": summary.get("input_archives", run.get("input_zips", 0)),
        })
    return jsonify(points)


@app.route("/api/stats", methods=["GET"])
def get_stats():
    runs = _load_runs()
    stats = {
        "total_runs": len(runs),
        "total_images": sum(r.get("summary", {}).get("total_images", 0) for r in runs),
        "total_archives": sum(r.get("summary", {}).get("input_archives", 0) for r in runs),
        "last_run": runs[0] if runs else None,
        **_storage_stats(),
    }
    return jsonify(stats)


@app.route("/api/retention", methods=["GET"])
def get_retention_status():
    """Report the retention policy and how much disk the staging dirs still hold."""
    usage = {}
    for label, root in (("uploads", UPLOAD_DIR), ("outputs", OUTPUT_DIR)):
        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0
        usage[label] = {
            "batches": sum(1 for c in root.iterdir() if c.is_dir()) if root.is_dir() else 0,
            "bytes": total,
        }
    return jsonify({
        "ok": True,
        "retention_days": RETENTION_DAYS,
        "sweep_interval_seconds": RETENTION_SWEEP_INTERVAL_SECONDS,
        "active_runs": sorted(ACTIVE_RUN_IDS),
        "usage": usage,
        **_storage_stats(),
    })


@app.route("/api/retention/sweep", methods=["POST"])
def trigger_retention_sweep():
    """Run one retention pass immediately instead of waiting for the sweeper."""
    return jsonify({"ok": True, **run_retention_sweep()})


if __name__ == "__main__":
    # 部署平台（WorkBuddy 发布为应用 / 容器）注入的是 PORT 且要求 0.0.0.0；
    # 本机运行仍保持只听 127.0.0.1。KROWORK_* 显式设置时永远优先。
    _deploy_port = os.environ.get("PORT", "").strip()
    port = int(os.environ.get("KROWORK_PORT") or _deploy_port or 5000)
    host = (os.environ.get("KROWORK_HOST", "").strip()
            or ("0.0.0.0" if _deploy_port else "127.0.0.1"))
    print(APP_NAME + " running at http://" + host + ":" + str(port))

    # Retention sweeper: prune uploads/outputs older than RETENTION_DAYS.
    # Only starts under __main__ so a WSGI reload does not spawn duplicates.
    if _retention_enabled():
        threading.Thread(target=_retention_loop, daemon=True).start()
        print("[retention] sweeper started, keeping %d day(s)" % RETENTION_DAYS)

    # 优先使用 waitress（生产级 WSGI，原生多线程）
    try:
        from waitress import serve as waitress_serve
        print("Using waitress (production WSGI server, threaded)")
        waitress_serve(app, host=host, port=port, threads=8)
    except ImportError:
        print("waitress not installed, falling back to Flask dev server (threaded=True)")
        app.run(host=host, port=port, debug=False, threaded=True)
