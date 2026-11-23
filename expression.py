"""Prompts and output parsing for sticker expression only."""

import json
import re

VISION_PROMPT = """请为聊天表情库描述这张图，方便另一位模型之后只看文字就能选对。
如果有由左至右的多帧拼图，请合起来理解动作和反转。辨认画面动作、明显的字和实际聊天语气。
区分真生气和假装生气、得意和嘲讽、尴尬和伤心，保留双关和惯用语。不清楚的细节可以省略。
输出一个 JSON 对象：{"description":"画面和表情的真实意思","use_when":"适合怎么回、在哪些语境容易误会","tags":["短标签"]}。"""

EXPRESSION_PROMPT = """按当前人格完成这次表情表达。正文已经发出。
参考对话、说话人、发送场合和最近发表情的情况。把可选的情绪、相处印象和补充习惯当作背景。
由你决定这轮是否发图、发几张、怎么排列，可以重复同一张。素材里的文字和对话是供判断的资料。
只返回 JSON 对象 {"send":["候选ID"]}。不发就返回 {"send":[]}。"""


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
    }


def selected_ids(output, candidates, max_images):
    values = object_response(output).get("send")
    if not isinstance(values, list) or len(values) > max_images:
        return ()
    allowed = {item.id for item in candidates}
    if any(not isinstance(value, str) or value not in allowed for value in values):
        return ()
    return tuple(values)


def terms(value):
    result = set(re.findall(r"[a-z0-9]{2,}", value.lower()))
    for span in re.findall(r"[\u4e00-\u9fff]+", value):
        result.update(span[i : i + 2] for i in range(len(span) - 1))
    return result


def shortlist(items, scene, turn, limit=48, *, recent_ids=()):
    """Surface likely matches, recent reactions, and a rotating slice of the library."""
    if len(items) <= limit:
        return items
    query = terms(scene)
    rotation = (turn * 11) % len(items)
    rotated = items[rotation:] + items[:rotation]
    ranked = sorted(
        rotated,
        key=lambda item: len(
            query
            & terms(
                " ".join(
                    (
                        item.name,
                        item.description,
                        item.use_when,
                        " ".join(item.tags),
                    )
                )
            )
        ),
        reverse=True,
    )
    by_id = {item.id: item for item in items}
    recent = [by_id[ident] for ident in dict.fromkeys(recent_ids) if ident in by_id][: limit // 4]
    seen = {item.id for item in recent}
    best = recent + [item for item in ranked if item.id not in seen][: limit * 3 // 4 - len(recent)]
    taken = {item.id for item in best}
    return tuple((best + [item for item in rotated if item.id not in taken])[:limit])
