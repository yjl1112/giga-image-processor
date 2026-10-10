"""Local-image validation, Agnes editing, and deterministic masked compositing."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.error
import urllib.request

from PIL import Image, ImageChops, ImageFilter, ImageOps, UnidentifiedImageError


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_EDGE = 4096
ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
AGNES_IMAGE_API_URL = os.environ.get("AGNES_IMAGE_API_URL", "https://apihub.agnes-ai.com/v1").strip().rstrip("/")
AGNES_IMAGE_MODEL = os.environ.get("AGNES_IMAGE_MODEL", "agnes-image-2.1-flash").strip()
AGNES_TEXT_API_URL = os.environ.get("AGNES_TEXT_API_URL", AGNES_IMAGE_API_URL).strip().rstrip("/")
AGNES_PROMPT_MODEL = os.environ.get("AGNES_PROMPT_MODEL", "agnes-2.5-flash").strip()
MAX_OPTIMIZED_PROMPT_CHARS = 2000


class ImageEditError(ValueError):
    """A safe error that can be returned to the browser."""


class PromptOptimizationError(ImageEditError):
    """A safe error raised when the text model cannot optimize a prompt."""


def _read_upload(file_storage):
    content = file_storage.read(MAX_IMAGE_BYTES + 1)
    if not content:
        raise ImageEditError("图片文件为空")
    if len(content) > MAX_IMAGE_BYTES:
        raise ImageEditError("单张图片不能超过 12 MB")
    return content


def _open_image(content, label):
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageEditError(f"{label}不是有效的 PNG、JPG 或 WebP 图片") from exc
    if image.width < 2 or image.height < 2:
        raise ImageEditError(f"{label}尺寸过小")
    if image.width > MAX_IMAGE_EDGE or image.height > MAX_IMAGE_EDGE:
        raise ImageEditError(f"{label}边长不能超过 {MAX_IMAGE_EDGE} 像素")
    return image


def load_original(file_storage):
    if file_storage.mimetype not in ALLOWED_MIME_TYPES:
        raise ImageEditError("原图仅支持 PNG、JPG 或 WebP")
    return _open_image(_read_upload(file_storage), "原图")


def load_mask(file_storage, size):
    if file_storage.mimetype not in ALLOWED_MIME_TYPES:
        raise ImageEditError("遮罩仅支持 PNG、JPG 或 WebP")
    mask = _open_image(_read_upload(file_storage), "遮罩").convert("L")
    if mask.size != size:
        mask = mask.resize(size, Image.Resampling.NEAREST)
    if mask.getbbox() is None:
        raise ImageEditError("遮罩为空，请涂抹需要修改的区域")
    return mask


def save_png(image, path):
    image.save(path, format="PNG", optimize=True)


def _ratio_for(size):
    width, height = size
    ratio = width / height
    options = {
        "1:1": 1,
        "3:4": 3 / 4,
        "4:3": 4 / 3,
        "16:9": 16 / 9,
        "9:16": 9 / 16,
        "2:3": 2 / 3,
        "3:2": 3 / 2,
        "21:9": 21 / 9,
    }
    return min(options, key=lambda key: abs(options[key] - ratio))


def _is_text_translation(edit_spec):
    return isinstance(edit_spec, dict) and edit_spec.get("mode") == "text_translation"


def build_edit_prompt(prompt, edit_spec=None):
    """Build the model instruction, with an explicit guardrail for text-only edits."""
    base = (
        "图片中的半透明红色区域是唯一需要编辑的位置。不要在结果中保留红色覆盖；"
        "仅按下列要求修改该区域，其他区域保持原有内容和构图。"
    )
    if not _is_text_translation(edit_spec):
        return base + "\n用户要求：" + prompt

    source_text = str(edit_spec.get("source_text") or "").strip()
    target_language = str(edit_spec.get("target_language") or "").strip()
    replacement_text = str(edit_spec.get("replacement_text") or "").strip()
    source_clause = f"原文“{source_text}”" if source_text else "红色区域内的可读文字"
    target_clause = (
        f"替换为准确的{target_language}文本“{replacement_text}”"
        if replacement_text
        else f"翻译为{target_language}"
    )
    return (
        base
        + "\n这是严格的文字翻译/替换任务：仅将"
        + source_clause
        + target_clause
        + "。"
        + "仅替换文字字形本身；必须保留字体风格、字号、字重、颜色、位置、对齐、行数和行距。"
        + "文字下方和周围的所有非文字像素必须保持原样，包括背景颜色、渐变、纹理、阴影、边框、色块、光线和产品内容。"
        + "禁止新建或扩大色块、底纹、标签、白底、矩形或圆角框；不要保留原文，也不要改变版式来腾出空间。"
        + "若无法同时保证准确文字和背景不变，优先保持原图背景和排版，不要重绘背景。"
        + "\n用户补充："
        + prompt
    )


def _prompt_optimizer_messages(prompt, edit_spec=None):
    """Build a text-only request for the optimizer; the original image is never sent."""
    source_text = str((edit_spec or {}).get("source_text") or "").strip()
    target_language = str((edit_spec or {}).get("target_language") or "").strip()
    replacement_text = str((edit_spec or {}).get("replacement_text") or "").strip()
    system = (
        "你是电商图片局部编辑提示词专家。将用户意图改写为一段可直接交给图像编辑模型的高质量中文提示词。"
        "只输出最终提示词，不要解释、标题、列表、Markdown 或引号。"
        "不得编造用户未提及的对象、文字、颜色、位置或品牌；必须保留用户的明确要求。"
        "提示词要明确：仅编辑红色遮罩选区，遮罩外和未提及部分保持不变；避免新增水印、文字、边框或色块。"
        "最终提示词必须不超过 1800 个字符。"
    )
    if _is_text_translation(edit_spec):
        system += (
            "这是文字翻译/替换场景：提示词必须要求仅改变字形，保留字体风格、字号、字重、颜色、位置、对齐、行距、"
            "背景颜色、渐变、纹理、阴影、边框、色块和所有非文字像素。"
        )
    request_data = {
        "用户原始指令": str(prompt or "").strip(),
        "编辑模式": "文字翻译/替换" if _is_text_translation(edit_spec) else "普通局部编辑",
        "原文": source_text or None,
        "目标语言": target_language or None,
        "准确替换文本": replacement_text or None,
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "请依据以下结构化信息生成最终图像编辑提示词：\n" + json.dumps(request_data, ensure_ascii=False),
        },
    ]


def _extract_chat_completion_text(payload):
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
            if text_parts:
                return "".join(text_parts).strip()
    return ""


def _normalize_optimized_prompt(value):
    text = str(value or "").strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
    text = re.sub(r"^(?:优化(?:后)?提示词|提示词|Prompt)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_OPTIMIZED_PROMPT_CHARS:
        raise PromptOptimizationError("Agnes 返回的优化提示词超过 2000 个字符，请重试")
    if not text:
        raise PromptOptimizationError("Agnes 未返回可用的优化提示词")
    return text


def optimize_edit_prompt(prompt, edit_spec=None):
    """Use Agnes's text model to improve an image-edit instruction."""
    api_key = os.environ.get("AGNES_API_KEY", "").strip()
    if not api_key:
        raise PromptOptimizationError("未配置 AGNES_API_KEY，暂时无法使用模型优化指令")
    if not AGNES_PROMPT_MODEL:
        raise PromptOptimizationError("未配置 AGNES_PROMPT_MODEL，暂时无法使用模型优化指令")
    payload = {
        "model": AGNES_PROMPT_MODEL,
        "messages": _prompt_optimizer_messages(prompt, edit_spec),
        "temperature": 0.2,
        "max_tokens": 900,
        "stream": False,
    }
    endpoint = AGNES_TEXT_API_URL
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    upstream = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upstream, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise PromptOptimizationError("Agnes 文字模型授权失败，请检查 API Key") from exc
        raise PromptOptimizationError("Agnes 文字模型暂时无法响应，请稍后重试") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PromptOptimizationError("Agnes 文字模型暂时无法响应，请稍后重试") from exc
    return _normalize_optimized_prompt(_extract_chat_completion_text(result)), AGNES_PROMPT_MODEL


def _agnes_request(prompt, original, mask, edit_spec=None):
    api_key = os.environ.get("AGNES_API_KEY", "").strip()
    if not api_key:
        raise ImageEditError("未配置 AGNES_API_KEY，暂时无法进行局部编辑")

    # Agnes accepts image-to-image input but does not expose a dedicated inpaint
    # mask field.  Supply a translucent red guide image so the model can see the
    # intended edit region; the final compositing below still makes the boundary
    # deterministic.
    guide = original.convert("RGBA").copy()
    overlay = Image.new("RGBA", guide.size, (255, 64, 64, 0))
    guide_opacity = 0.22 if _is_text_translation(edit_spec) else 0.42
    overlay.putalpha(mask.point(lambda value: round(value * guide_opacity)))
    guide = Image.alpha_composite(guide, overlay)
    buffer = io.BytesIO()
    guide.convert("RGB").save(buffer, format="PNG", optimize=True)
    data_uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    payload = {
        "model": AGNES_IMAGE_MODEL,
        "prompt": build_edit_prompt(prompt, edit_spec),
        "size": "1K",
        "ratio": _ratio_for(original.size),
        "extra_body": {"image": [data_uri], "response_format": "b64_json"},
    }
    upstream = urllib.request.Request(
        f"{AGNES_IMAGE_API_URL}/images/generations",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upstream, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ImageEditError("Agnes 图片模型授权失败，请检查 API Key") from exc
        raise ImageEditError("Agnes 图片模型暂时无法响应，请稍后重试") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ImageEditError("Agnes 图片模型暂时无法响应，请稍后重试") from exc

    items = result.get("data") if isinstance(result, dict) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("b64_json"):
            continue
        try:
            return _open_image(base64.b64decode(str(item["b64_json"]), validate=True), "模型结果")
        except (ValueError, TypeError, ImageEditError):
            continue
    raise ImageEditError("Agnes 没有返回可用图片，请调整提示词后重试")


def generate_masked_edit(prompt, original, mask, edit_spec=None):
    generated = _agnes_request(prompt, original, mask, edit_spec)
    if generated.size != original.size:
        generated = generated.resize(original.size, Image.Resampling.LANCZOS)
    # Text replacement uses a hard edge to avoid blending model-generated colors
    # into the surrounding background. Both paths retain all unselected pixels.
    if _is_text_translation(edit_spec):
        final_mask = mask
    else:
        final_mask = ImageChops.multiply(mask.filter(ImageFilter.GaussianBlur(radius=1.2)), mask)
    final = Image.composite(generated, original, final_mask)
    return generated, final
