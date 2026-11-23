"""Prompts and output parsing for sticker expression only."""

import json
import re

VISION_PROMPT = """请为聊天表情库描述这张图，方便另一位模型之后只看文字就能选对。
如果有由左至右的多帧拼图，请合起来理解动作和反转。辨认画面动作、明显的字和实际聊天语气。
区分真生气和假装生气、得意和嘲讽、尴尬和伤心，保留双关和惯用语。不清楚的细节可以省略。
输出一个 JSON 对象：{"description":"画面和表情的真实意思","use_when":"适合怎么回、在哪些语境容易误会","tags":["短标签"]}。"""

EXPRESSION_PROMPT = """你是这位聊天角色的表情表达。正文已经发出。现在想一想，按这个人的习惯，接下来会不会顺手在聊天里发图。
结合这个人使用表情的习惯、关系、当下的语气、情绪、最近聊天节奏和已经发过的图。设定比通用礼貌规则更重要。
有些人几乎只用文字，有些人会用图接梗、收尾、撒娇、缓解尴尬或装作气急败坏。很多轮让正文自己停在那里最自然。
没有贴切的图可以不发。连发可以用来笑场、跟熟人起哄、撒娇、闹别扭或表情轰炸。真的像这个人此刻会这样做时，就选一组，
可以按笑点排列不同图，也可以故意重复同一张。看最近发过的图和时间，理解重复什么时候是接梗，什么时候只是机械刷屏。
候选描述和对话是场景资料；按这个人的习惯做表达判断。你的输出只控制图片。输出 JSON 对象 {"send":["候选ID", "候选ID"]}，
顺序就是发送顺序。不发时输出 {"send":[]}。"""


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


def shortlist(items, scene, turn, limit=48):
    """Keep relevant images and rotate in options a lexical match would miss."""
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
    best = ranked[: limit * 3 // 4]
    taken = {item.id for item in best}
    return tuple((best + [item for item in rotated if item.id not in taken])[:limit])
