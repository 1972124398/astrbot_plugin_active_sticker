"""Local image files, their reviewed descriptions, and the shareable catalog."""

import base64
import hashlib
import io
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PIL import Image as PillowImage
from PIL import ImageOps

FORMATS = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}
MAX_BYTES = 16 * 1024 * 1024
STICKER_ID = re.compile(r"[0-9a-f]{16}")


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

    def summary(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "use_when": self.use_when,
            "tags": self.tags,
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
        if self.index.exists():
            rows = json.loads(self.index.read_text(encoding="utf-8"))
            for row in rows:
                row["tags"] = tuple(row.get("tags", []))
                item = Sticker(**row)
                if STICKER_ID.fullmatch(item.id) and item.extension in FORMATS.values():
                    self.items[item.id] = item
        self.write_catalog()

    @staticmethod
    def write(path, data):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    def commit(self, items):
        self.write(
            self.index,
            json.dumps([asdict(item) for item in items.values()], ensure_ascii=False, indent=2).encode(),
        )
        self.items = items
        self.write_catalog()

    def path(self, item):
        return self.images / f"{item.id}.{item.extension}"

    def available(self):
        return tuple(
            item for item in self.items.values() if item.enabled and item.description and self.path(item).is_file()
        )

    def add(self, name, data, inspected):
        ident, extension, animated = inspected
        item = self.items.get(ident)
        if item:
            if not self.path(item).is_file():
                self.write(self.path(item), data)
                self.write_catalog()
            return item, False
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()[:160]
        item = Sticker(ident, name or f"{ident}.{extension}", extension, animated)
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

    def catalog(self):
        lines = ["# 主动表情 · 表情库", ""]
        for item in self.available():
            name = " ".join(item.name.split())
            description = " ".join(item.description.split())
            usage = " ".join(item.use_when.split())
            tags = "、".join(item.tags)
            lines.append(f"- {name} [{item.id}] · {description}")
            if usage:
                lines.append(f"  - 适合 {usage}")
            if tags:
                lines.append(f"  - 标签 {tags}")
        return "\n".join(lines) + "\n"

    def write_catalog(self):
        self.write(self.root / "catalog.md", self.catalog().encode("utf-8"))
