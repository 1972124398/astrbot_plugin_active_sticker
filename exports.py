"""Complete, spreadsheet-friendly library exports from a single management snapshot."""

import csv
import io
import json
import uuid
import zipfile
from pathlib import Path

CSV_FIELDS = (
    "图片ID",
    "文件名",
    "图片相对路径",
    "风格包ID",
    "风格包",
    "风格包说明",
    "风格包启用",
    "图片启用",
    "源文件存在",
    "格式",
    "动图",
    "识图状态",
    "可使用",
    "画面与语气",
    "适合怎么回",
    "标签",
    "表达分类",
    "自动识图描述",
    "自动识图适用场景",
    "自动识图标签",
    "自动识图分类",
    "识图模型",
    "识图时间（UTC）",
    "最近识图错误",
)


def csv_cell(value):
    text = str(value)
    # Quoting alone does not prevent spreadsheet applications from evaluating a cell.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def csv_bytes(snapshot):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_FIELDS)
    for item in snapshot["rows"]:
        vision = item.get("vision_result") or {}
        state = (
            "识图中"
            if item["pending"]
            else "识图失败"
            if item["error"]
            else ("已有描述" if item["description"] else "待识图")
        )
        ready = item["enabled"] and item["pack_enabled"] and item["description"] and not item["missing"]
        values = (
            item["id"],
            item["name"],
            f"images/{item['id']}.{item['extension']}",
            item["pack_id"],
            item["pack_name"],
            item["pack_description"],
            "是" if item["pack_enabled"] else "否",
            "是" if item["enabled"] else "否",
            "否" if item["missing"] else "是",
            item["extension"],
            "是" if item["animated"] else "否",
            state,
            "是" if ready else "否",
            item["description"],
            item["use_when"],
            "、".join(item["tags"]),
            "、".join(item["effective_categories"]),
            vision.get("description", ""),
            vision.get("use_when", ""),
            "、".join(vision.get("tags", [])),
            "、".join(vision.get("categories", [])),
            vision.get("provider", ""),
            vision.get("recognized_at", ""),
            item["error"],
        )
        writer.writerow(csv_cell(value) for value in values)
    # BOM allows Excel to recognize Chinese text when the CSV is opened directly.
    return output.getvalue().encode("utf-8-sig")


def write_export(root, snapshot, *, documents=False):
    folder = Path(root) / "exports"
    folder.mkdir(exist_ok=True)
    path = folder / f"library-{uuid.uuid4().hex}.{'zip' if documents else 'csv'}"
    try:
        data = csv_bytes(snapshot)
        if documents:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("stickers.csv", data)
                archive.writestr("index.json", json.dumps(snapshot["metadata"], ensure_ascii=False, indent=2))
                for name, content in snapshot["documents"].items():
                    archive.writestr(name, content)
                archive.writestr(
                    "导出说明.md",
                    """# 图片库文档导出

- stickers.csv：全库图片列表，包含停用、待识图和源文件缺失的条目，不受面板筛选条件影响。
- index.json：完整的图片、风格包和已保存的自动识图结果元数据。
- catalog.md：插件生成的可用图片描述总表。
- library.md 和 catalog/：插件生成的可用图片分类目录及描述文档。

「画面与语气／适合怎么回／标签／表达分类」是当前使用的结果，包含人工整理。
「自动识图」列是本版起保存的最近一次成功识图结果；未留存或未成功识图时为空。
时间采用 UTC。CSV 使用 UTF-8 BOM，含换行或逗号的文本遵循 CSV 引号规则。
为避免表格软件执行文本公式，危险前缀前加单引号；index.json 保留原始文本。
导出期间仍在识图的条目保留导出时的状态，可在识图结束后重新导出。
此包包含文档和元数据，原图请使用风格包的「打包下载」。
""",
                )
        else:
            path.write_bytes(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
