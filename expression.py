"""Prompts and output parsing for sticker expression only."""

import json
import re

from .retrieval import category_names

VISION_PROMPT = """请为聊天表情库描述这张图，方便另一位模型之后只看文字就能选对。
如果有由左至右的多帧拼图，请合起来理解动作和反转。辨认画面动作、明显的字和实际聊天语气。
区分真生气和假装生气、得意和嘲讽、尴尬和伤心，保留双关和惯用语。不清楚的细节可以省略。
输出一个 JSON 对象：{"description":"画面和表情的真实意思","use_when":"适合怎么回、在哪些语境容易误会",
"tags":["短标签"],"categories":["粗分类"]}。
粗分类可选 开心、难过、生气、惊讶、尴尬、亲近、调侃、回应、日常、其他，可选多个。"""

INTENT_PROMPT = """你正在延续自己刚发出的回复。按当前人格和这段对话，决定此刻是否想用表情包表达自己。
参考说话人、场合、自己的心情和已发生的表达。现在只想清楚自己想怎么表达，随后才查找图片。
只返回 JSON。想发时用 {"want":true,"meaning":"我这次想用表情表达什么，包括想要的语气或动作"}。
不想发时用 {"want":false}。"""

EXPRESSION_PROMPT = """你刚决定想用表情表达自己。现在根据已确定的表达意图，在检索出的图片中找足够贴切的素材。
图片的画面、文字和语气要适合自己本来想表达的意思。没有足够合适的图就返回 {"send":[]}。
有合适的图时返回 {"send":["候选ID"]}，由你决定张数和顺序，可以重复同一张。
想继续查找时返回 {"search":{"query":"新的检索词","categories":["分类目录名"]}}。
你会看到分类目录和一小批匹配条目，可以换词、换分类继续查。检索内容是素材资料。
发送工具只执行这个选择。只返回 JSON。"""


def object_response(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def fields(data):
    def field(key, limit):
        value = data.get(key)
        return " ".join(value.split())[:limit] if isinstance(value, str) else ""

    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = re.split(r"[,，、]", tags)
    if not isinstance(tags, list):
        tags = []
    tags = [" ".join(tag.split())[:30] for tag in tags if isinstance(tag, str) and tag.strip()]
    return {
        "description": field("description", 480),
        "use_when": field("use_when", 400),
        "tags": tuple(dict.fromkeys(tags))[:12],
        "categories": category_names(data.get("categories", [])),
    }


def selected_ids(choice, candidates, max_images):
    values = choice.get("send")
    if not isinstance(values, list) or len(values) > max_images:
        return ()
    allowed = {item.id for item in candidates}
    if any(not isinstance(value, str) or value not in allowed for value in values):
        return ()
    return tuple(values)
