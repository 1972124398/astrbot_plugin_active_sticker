"""Prompts and output parsing for sticker expression only."""

import json
import re

from .retrieval import category_names

CHAT_EXPRESSION_PROMPT = """【主动表情插件：表情包使用方式】
本会话已启用「主动表情」（astrbot_plugin_active_sticker），当前有可用的本地表情图片。
需要用表情包表达自己或回应用户的表情请求时，优先使用主动表情的本地图库。
先按人格和当前对话自然回复；正文送达后，插件会让你继续决定想不想发表情、想表达什么，
再按这个意图检索本地描述文档、选择贴切的图片并发送。无需在主对话调用检索或发送工具，
也无需自己查文件路径、执行代码或调用通用发图工具。没有合适的图片时可以不发。
不要先调用 wechat_find_stickers 或 wechat_send_sticker 来检查本插件是否可用；
微信原生表情收藏与本地图库独立，原生收藏为空不代表你没有表情包，也无需先同步微信收藏。
只有用户明确要求使用微信原生收藏中的表情时，才考虑对应的原生工具。
按当前插件状态理解能力，不沿用旧对话里「没有表情库／不能发表情」的结论。
发送前不要声称图片已发出，也不要向用户解释这些内部步骤或用括号动作假装已经发图。
是否使用表情仍由人格、意愿和语境决定，不要求每轮都发。"""

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
