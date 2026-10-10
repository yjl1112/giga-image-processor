#!/usr/bin/env python3
"""GIGA 素材图片处理 JSON 输出封装（供 giga-material-processor app 调用）。

两种模式：
  --mode simple   直接处理上传的 ZIP 包列表（文件名作为 item code）
  --mode template 上传 Excel 模板 + ZIP 包，按 Excel 匹配

用法：
    .venv/bin/python3 scripts/process_materials_json.py \\
        --mode simple \\
        --input-dir /tmp/uploads/xxx \\
        --output-dir /tmp/output \\
        --size 1600 --quality 85
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from process_giga_materials import (  # noqa: E402
    process_material_archives,
    build_rows_from_template,
    match_material_zip,
    safe_name,
    ProcessResult,
)


def _to_jsonable(obj):
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    return obj


def run_simple(input_dir, output_dir, report_dir, size=1600, quality=85, workers=None):
    """直接处理 input_dir 下所有 .zip，每个 zip 文件名作为 item code。"""
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    report_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    zips = sorted(input_dir.glob("*.zip"))
    if not zips:
        return {"ok": False, "error": f"输入目录无 .zip 文件: {input_dir}"}

    archive_rows = []
    missing = []
    for zp in zips:
        # 文件名作为 item code（去掉 .zip 后缀和常见前缀如 image+file）
        stem = zp.stem
        # 常见命名: XXX_image+file.zip 或 XXX.zip
        item_code = stem.split("_image")[0].split(" image")[0] if ("image" in stem.lower()) else stem
        item_code = item_code.strip()
        archive_rows.append({
            "source_zip": str(zp),
            "expected_item_code": item_code,
            "actual_item_code": item_code,
            "output_prefix": safe_name(item_code or stem),
            "note": "",
        })

    result = process_material_archives(
        input_dir=input_dir,
        output_dir=output_dir,
        report_dir=report_dir,
        size=size,
        quality=quality,
        archive_rows=archive_rows,
        workers=workers,
    )
    return _build_payload(result, archive_rows, mode="simple")


def run_template(template_path, input_dir, output_dir, report_dir,
                 size=1600, quality=85, workers=None, sheet_name="Sheet1"):
    """按 Excel 模板匹配处理 ZIP 包。"""
    template_path = Path(template_path)
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    report_dir = Path(report_dir)
    if not template_path.exists():
        return {"ok": False, "error": f"模板文件不存在: {template_path}"}

    try:
        rows = build_rows_from_template(
            template_path=template_path,
            input_dir=input_dir,
            sheet_name=sheet_name,
        )
    except Exception as exc:
        return {"ok": False, "error": f"模板解析失败: {exc}"}

    if not rows:
        return {"ok": False, "error": "模板未匹配到任何 ZIP 素材包"}

    result = process_material_archives(
        input_dir=input_dir,
        output_dir=output_dir,
        report_dir=report_dir,
        size=size,
        quality=quality,
        archive_rows=rows,
        workers=workers,
    )
    return _build_payload(result, rows, mode="template")


def _build_payload(result: ProcessResult, archive_rows, mode):
    """构造 JSON payload。"""
    # 扫描输出目录里处理好的产品文件夹
    export_zip = Path(result.export_zip)
    output_dir = export_zip.parent
    products = []
    for d in sorted(output_dir.iterdir()) if output_dir.exists() else []:
        if d.is_dir() and not d.name.startswith("_") and not d.name.startswith("."):
            images = sorted([f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")])
            products.append({
                "folder": d.name,
                "image_count": len(images),
                "images": images[:5],  # 只取前 5 张展示
            })

    payload = {
        "ok": True,
        "mode": mode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "input_archives": len(archive_rows),
            "total_images": result.image_count,
            "product_folders": len(products),
            "export_zip": str(export_zip),
            "export_zip_name": export_zip.name,
            "export_zip_size": export_zip.stat().st_size if export_zip.exists() else 0,
            "report_csv": str(result.report_csv),
        },
        "products": products,
        "export_zip": str(export_zip),
        "report_csv": str(result.report_csv),
    }
    return _to_jsonable(payload)


def main():
    p = argparse.ArgumentParser(description="GIGA 素材图片处理 JSON 输出")
    p.add_argument("--mode", choices=["simple", "template"], default="simple",
                   help="simple=直接处理 zip 列表；template=按 Excel 模板匹配")
    p.add_argument("--input-dir", required=True, help="包含 ZIP 素材包的目录")
    p.add_argument("--output-dir", required=True, help="输出目录")
    p.add_argument("--report-dir", required=True, help="报告目录")
    p.add_argument("--template", help="Excel 模板路径（mode=template 必填）")
    p.add_argument("--sheet", default="Sheet1", help="Excel sheet 名")
    p.add_argument("--size", type=int, default=1600, help="输出图片尺寸，默认 1600")
    p.add_argument("--quality", type=int, default=85, help="JPG 质量，默认 85")
    p.add_argument("--workers", type=int, default=None, help="并发数")
    args = p.parse_args()

    try:
        if args.mode == "simple":
            payload = run_simple(
                args.input_dir, args.output_dir, args.report_dir,
                size=args.size, quality=args.quality, workers=args.workers,
            )
        else:
            if not args.template:
                raise ValueError("template 模式需要 --template")
            payload = run_template(
                args.template, args.input_dir, args.output_dir, args.report_dir,
                size=args.size, quality=args.quality, workers=args.workers,
                sheet_name=args.sheet,
            )
        print(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
