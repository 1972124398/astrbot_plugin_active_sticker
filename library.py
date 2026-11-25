"""Local image files, their reviewed descriptions, and the shareable catalog."""

import base64
import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PIL import Image as PillowImage
from PIL import ImageOps

from .retrieval import CATEGORIES, CatalogIndex, inferred_categories

FORMATS = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}
MAX_BYTES = 16 * 1024 * 1024
STICKER_ID = re.compile(r"[0-9a-f]{16}")
DEFAULT_PACK = "default"


@dataclass(frozen=True)
class StickerPack:
    id: str
    name: str
    description: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class Sticker:
    id: str
    name: str
    extension: str
    animated: bool
    description: str = ""
    use_when: str = ""
    tags: tuple[str, ...] = ()
    enabled: bool = True
    revision: int = 0
    categories: tuple[str, ...] = ()
    pack_id: str = DEFAULT_PACK
    vision_result: dict | None = None

    def summary(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "use_when": self.use_when,
            "tags": self.tags,
            "categories": self.categories,
            "pack_id": self.pack_id,
        }


def inspect_image(data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("请选择 16 MB 以内的图片")
    try:
        with PillowImage.open(io.BytesIO(data)) as image:
            if image.format not in FORMATS:
                raise ValueError("支持 PNG、JPEG、GIF、WebP")
            extension = FORMATS[image.format]
            animated = getattr(image, "is_animated", False)
            image.verify()
    except (OSError, SyntaxError, PillowImage.DecompressionBombError) as error:
        raise ValueError("无法读取这张图片") from error
    return hashlib.sha256(data).hexdigest()[:16], extension, animated


def vision_image(path):
    """Build a compact PNG. Preserve the original bytes for actual delivery."""
    with PillowImage.open(path) as image:
        count = getattr(image, "n_frames", 1)
        positions = list(dict.fromkeys([0, count // 2, count - 1]))
        frames = []
        for position in positions:
            image.seek(position)
            frame = ImageOps.exif_transpose(image.convert("RGBA"))
            frame.thumbnail((384, 384))
            frames.append(frame)
        canvas = PillowImage.new("RGBA", (sum(f.width for f in frames), max(f.height for f in frames)), "white")
        offset = 0
        for frame in frames:
            canvas.alpha_composite(frame, (offset, 0))
            offset += frame.width
        output = io.BytesIO()
        canvas.convert("RGB").save(output, "PNG")
    return output.getvalue()


def preview_data(path):
    with PillowImage.open(path) as image:
        frame = ImageOps.exif_transpose(image.convert("RGBA"))
        frame.thumbnail((220, 220))
        output = io.BytesIO()
        frame.save(output, "WEBP", quality=76)
    return "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode("ascii")


class StickerLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.images = self.root / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.index = self.root / "index.json"
        self.items = {}
        self.packs = {DEFAULT_PACK: StickerPack(DEFAULT_PACK, "未分组")}
        if self.index.exists():
            rows = json.loads(self.index.read_text(encoding="utf-8"))
            if isinstance(rows, dict):
                for pack in rows.get("packs", []):
                    pack = StickerPack(**pack)
                    if pack.id == DEFAULT_PACK or STICKER_ID.fullmatch(pack.id):
                        self.packs[pack.id] = pack
                rows = rows["items"]
            for row in rows:
                row["tags"] = tuple(row.get("tags", []))
                row["categories"] = tuple(row.get("categories", []))
                item = Sticker(**row)
                if STICKER_ID.fullmatch(item.id) and item.extension in FORMATS.values():
                    if item.pack_id not in self.packs:
                        item = replace(item, pack_id=DEFAULT_PACK)
                    self.items[item.id] = item
        self.write_catalog()

    @staticmethod
    def write(path, data):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    def commit(self, items, packs=None):
        packs = self.packs if packs is None else packs
        self.write(
            self.index,
            json.dumps(
                {
                    "version": 2,
                    "packs": [asdict(pack) for pack in packs.values()],
                    "items": [asdict(item) for item in items.values()],
                },
                ensure_ascii=False,
                indent=2,
            ).encode(),
        )
        self.items = items
        self.packs = packs
        self.write_catalog()

    def save_pack(self, name, description="", enabled=True, ident=None):
        name = " ".join(name.split())[:60]
        if not name:
            raise ValueError("请填写风格包名称")
        if ident is not None and ident not in self.packs:
            raise ValueError("找不到风格包")
        if any(pack.name.casefold() == name.casefold() and pack.id != ident for pack in self.packs.values()):
            raise ValueError("风格包名称已存在")
        pack = StickerPack(ident or uuid.uuid4().hex[:16], name, " ".join(description.split())[:300], enabled)
        self.commit(self.items, {**self.packs, pack.id: pack})
        return pack

    def delete_pack(self, ident):
        if ident == DEFAULT_PACK or ident not in self.packs:
            raise ValueError("未分组不能删除，其他风格包请刷新后重试")
        packs = {key: pack for key, pack in self.packs.items() if key != ident}
        items = {
            key: replace(item, pack_id=DEFAULT_PACK, revision=item.revision + 1) if item.pack_id == ident else item
            for key, item in self.items.items()
        }
        self.commit(items, packs)

    def organize(self, ids, *, pack_id=None, enabled=None):
        if not ids or any(ident not in self.items for ident in ids):
            raise ValueError("部分图片已不存在，请刷新后重试")
        if pack_id is not None and pack_id not in self.packs:
            raise ValueError("找不到目标风格包")
        changes = {}
        if pack_id is not None:
            changes["pack_id"] = pack_id
        if enabled is not None:
            changes["enabled"] = enabled
        selected = set(ids)
        self.commit(
            {
                key: replace(item, revision=item.revision + 1, **changes) if key in selected else item
                for key, item in self.items.items()
            }
        )

    def path(self, item):
        return self.images / f"{item.id}.{item.extension}"

    def available(self, pack_id=None):
        return tuple(
            item
            for item in self.items.values()
            if item.enabled
            and item.description
            and self.packs[item.pack_id].enabled
            and (pack_id is None or item.pack_id == pack_id)
            and self.path(item).is_file()
        )

    def add(self, name, data, inspected, pack_id=DEFAULT_PACK):
        if pack_id not in self.packs:
            raise ValueError("找不到目标风格包")
        ident, extension, animated = inspected
        item = self.items.get(ident)
        if item:
            if not self.path(item).is_file():
                self.write(self.path(item), data)
                self.write_catalog()
            return item, False
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()[:160]
        item = Sticker(ident, name or f"{ident}.{extension}", extension, animated, pack_id=pack_id)
        self.write(self.path(item), data)
        self.commit({**self.items, ident: item})
        return item, True

    def update(self, current, **changes):
        if self.items.get(current.id) is not current:
            return None
        item = replace(current, revision=current.revision + 1, **changes)
        self.commit({**self.items, item.id: item})
        return item

    def delete(self, item):
        items = dict(self.items)
        del items[item.id]
        self.commit(items)
        self.path(item).unlink(missing_ok=True)

    def catalog(self, pack_id=None):
        lines = ["# 主动表情 · 表情库", ""]
        for item in self.available(pack_id):
            name = " ".join(item.name.split())
            description = " ".join(item.description.split())
            usage = " ".join(item.use_when.split())
            tags = "、".join(item.tags)
            lines.append(f"- {name} [{item.id}] · {description}")
            lines.append(f"  - 风格包 {self.packs[item.pack_id].name}")
            if usage:
                lines.append(f"  - 适合 {usage}")
            if tags:
                lines.append(f"  - 标签 {tags}")
        return "\n".join(lines) + "\n"

    def write_catalog(self):
        self.write(self.root / "catalog.md", self.catalog().encode("utf-8"))
        self.search_index = CatalogIndex(self.available())
        grouped = {ident: [] for ident in self.packs}
        for item in self.search_index.items.values():
            grouped[item.pack_id].append(item)
        self.pack_indexes = {ident: CatalogIndex(group) for ident, group in grouped.items()}
        documents = self.root / "catalog"
        documents.mkdir(exist_ok=True)
        overview = ["# 表情库分类目录", "", "按表达意图检索分类文档中的条目。", ""]
        for name in CATEGORIES:
            ids = self.search_index.groups[name]
            lines = [f"# {name}", ""]
            for ident in self.items:
                if ident in ids:
                    lines.append(json.dumps(self.items[ident].summary(), ensure_ascii=False))
            self.write(documents / f"{name}.md", "\n".join(lines).encode("utf-8"))
            if ids:
                overview.append(f"- [{name}](catalog/{name}.md) · {len(ids)} 张")
        self.write(self.root / "library.md", "\n".join(overview).encode("utf-8"))

    def pack_snapshot(self, ident):
        return (
            self.packs[ident],
            tuple(item for item in self.items.values() if item.pack_id == ident),
            self.catalog(ident),
        )

    def export_snapshot(self, pending=(), errors=None, *, documents=False):
        errors = errors or {}
        rows = []
        for item in self.items.values():
            pack = self.packs[item.pack_id]
            rows.append(
                {
                    **asdict(item),
                    "effective_categories": inferred_categories(item),
                    "pack_name": pack.name,
                    "pack_description": pack.description,
                    "pack_enabled": pack.enabled,
                    "missing": not self.path(item).is_file(),
                    "pending": item.id in pending,
                    "error": errors.get(item.id, ""),
                }
            )
        files = {}
        if documents:
            for name in ("catalog.md", "library.md", *(f"catalog/{name}.md" for name in CATEGORIES)):
                files[name] = (self.root / name).read_bytes()
        return {
            "rows": rows,
            "documents": files,
            "metadata": {
                "version": 2,
                "packs": [asdict(pack) for pack in self.packs.values()],
                "items": [asdict(item) for item in self.items.values()],
            },
        }

    def export_pack(self, ident, snapshot=None):
        """Export originals and editable metadata, including disabled/unrecognized items."""
        pack, items, catalog = self.pack_snapshot(ident) if snapshot is None else snapshot
        exports = self.root / "exports"
        exports.mkdir(exist_ok=True)
        target = exports / f"{ident}-{uuid.uuid4().hex}.zip"
        try:
            # Image formats are already compressed; avoid spending chat CPU recompressing them.
            with zipfile.ZipFile(target, "w") as archive:
                manifest = {"version": 1, "pack": asdict(pack), "items": [asdict(item) for item in items]}
                archive.writestr(
                    "pack.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                archive.writestr("catalog.md", catalog, compress_type=zipfile.ZIP_DEFLATED)
                for item in items:
                    try:
                        archive.write(self.path(item), f"images/{item.id}.{item.extension}")
                    except FileNotFoundError:
                        pass  # Keep metadata if an administrator removed the original during export.
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target
