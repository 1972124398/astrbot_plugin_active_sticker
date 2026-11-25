"""Local category documents and a cached text index for small retrieval results."""

import json
import math
import re
from collections import OrderedDict, defaultdict

CATEGORIES = {
    "开心": "开心 高兴 笑 哈哈 爆笑 欢乐 得意 happy laugh",
    "难过": "难过 伤心 哭 委屈 失落 悲伤 安慰 sad cry",
    "生气": "生气 愤怒 炸毛 气愤 怒 吵架 angry",
    "惊讶": "惊讶 吃惊 震惊 疑惑 问号 懵 surprise confused",
    "尴尬": "尴尬 无语 汗 捂脸 社死 害羞 awkward",
    "亲近": "亲亲 抱抱 撒娇 卖萌 贴贴 爱心 喜欢 可爱 hug love",
    "调侃": "调侃 嘲讽 阴阳 搞怪 整活 坏笑 接梗 逗 joke tease",
    "回应": "点头 摇头 同意 拒绝 收到 确认 谢谢 对不起 好的 OK yes no",
    "日常": "早安 晚安 睡觉 想睡 困困 打哈欠 吃饭 工作 上班 加油 拜拜 再见 打招呼 挥手 morning sleep",
    "其他": "",
}
INDEX_VERSION = 2
RESULT_LIMIT = 8
RESULT_CHARS = 2600
NEGATIVE = re.compile(r"不适合|不宜|不用于|不要|避免|不是|并非|容易误会|别在")


def category_names(value):
    if isinstance(value, str):
        value = re.split(r"[,，、\s]+", value)
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(name for name in value if isinstance(name, str) and name in CATEGORIES))


def terms(value):
    found = set(re.findall(r"[a-z0-9]+", value.lower()))
    for span in re.findall(r"[\u4e00-\u9fff]+", value):
        found.update(span[i : i + 2] for i in range(len(span) - 1))
        found.update(char for char in span if char not in "的了我你他她它是这那有个在着和与就把要想会用对自")
    return found


def positive_text(value):
    return " ".join(part for part in re.split(r"[，,。；;！!？?\n]", value) if not NEGATIVE.search(part))


def retrieval_fields(data):
    """Optional concise recognition fields; full reviewed wording remains authoritative."""
    value = data.get("retrieval")
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, limit in (("meaning", 100), ("tone", 40), ("text", 100), ("avoid", 120)):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            result[key] = " ".join(text.split())[:limit]
    return result


def short_text(value, limit):
    if len(value) <= limit:
        return value
    head = limit * 2 // 3
    return value[:head] + "…" + value[-(limit - head - 1) :]


def compact_profile(item):
    vision = item.vision_result or {}
    same = all(getattr(item, key) == vision.get(key) for key in ("description", "use_when")) and all(
        tuple(getattr(item, key)) == tuple(vision.get(key, ())) for key in ("tags", "categories")
    )
    concise = retrieval_fields(vision) if same else {}
    negatives = "；".join(
        part.strip()
        for part in re.split(r"[。；;\n]", item.description + "；" + item.use_when)
        if NEGATIVE.search(part)
    )
    profile = {"description": concise.get("meaning") or short_text(item.description, 180)}
    if item.use_when:
        usage = positive_text(item.use_when)
        if usage:
            profile["use_when"] = short_text(usage, 90)
    if item.tags:
        profile["tags"] = tuple(tag[:20] for tag in item.tags[:4])
    for key in ("tone", "text"):
        if concise.get(key):
            profile[key] = concise[key]
    if concise.get("avoid") or negatives:
        profile["avoid"] = concise.get("avoid") or short_text(negatives, 120)
    return profile


def inferred_categories(item):
    if item.categories:
        return item.categories
    text = (" ".join(item.tags) if item.tags else item.description).lower()
    return tuple(
        name for name, cues in CATEGORIES.items() if any(cue.lower() in text for cue in cues.split())
    ) or ("其他",)


class CatalogIndex:
    def __init__(self, items):
        items = tuple(items)
        self.items = {item.id: item for item in items}
        self.postings = defaultdict(set)
        self.weights = defaultdict(dict)
        self.groups = defaultdict(set)
        self.categories = {}
        self.profiles = {}
        self.signatures = {}
        self.lengths = {}
        self.cache = OrderedDict()
        for item in items:
            categories = inferred_categories(item)
            self.categories[item.id] = categories
            for category in categories:
                self.groups[category].add(item.id)
            profile = compact_profile(item)
            self.profiles[item.id] = profile
            self.signatures[item.id] = (
                item.description,
                item.use_when,
                tuple(item.tags),
                json.dumps(profile, sort_keys=True, ensure_ascii=False),
            )
            words = defaultdict(float)
            for text, weight in (
                (" ".join(item.tags), 4),
                (positive_text(item.description), 2),
                (positive_text(item.use_when), 1),
                (item.name, 0.4),
                (positive_text(" ".join(str(profile.get(key, "")) for key in ("description", "tone", "text"))), 2),
            ):
                for word in terms(text):
                    words[word] += weight
            self.lengths[item.id] = max(1, len(words))
            for word, weight in words.items():
                self.postings[word].add(item.id)
                self.weights[word][item.id] = weight
        self.average_length = sum(self.lengths.values()) / max(1, len(self.items))

    def directory(self):
        return {name: len(self.groups[name]) for name in CATEGORIES if self.groups[name]}

    def documents(self):
        return [
            {"分类": name, "条目数": len(self.groups[name]), "文档": f"catalog/{name}.md"}
            for name in CATEGORIES
            if self.groups[name]
        ]

    def rank(self, query, categories):
        key = (query, tuple(categories))
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        scores = defaultdict(float)
        for word in terms(query):
            rarity = math.log(1 + (len(self.items) + 0.5) / (len(self.postings.get(word, ())) + 0.5))
            for ident in self.postings.get(word, ()):
                weight = self.weights[word][ident]
                normalization = 1.2 * (0.25 + 0.75 * self.lengths[ident] / self.average_length)
                scores[ident] += rarity * weight / (weight + normalization) * (0.2 if len(word) == 1 else 1)
        # Broader emotion/action cues retrieve a category even without a literal bigram match.
        positive_query = positive_text(query).lower()
        related = category_names(list(categories)) or tuple(
            name for name, cues in CATEGORIES.items() if any(cue.lower() in positive_query for cue in cues.split())
        )
        grouped = set()
        for category in categories:
            grouped.update(self.groups.get(category, ()))
        for category in related:
            for ident in self.groups.get(category, ()):
                scores[ident] += 0.5
        pool = grouped if categories else set(scores)
        ranked = sorted(pool, key=lambda ident: (-scores[ident], ident))
        self.cache[key] = tuple(ranked)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return self.cache[key]

    def search(self, query, categories=(), *, exclude=(), limit=RESULT_LIMIT, compact=False, budget=RESULT_CHARS):
        ranked = self.rank(query[:300], tuple(categories))
        excluded = set(exclude)
        selected = []
        entries = []
        signatures = set()
        for ident in ranked:
            if ident in excluded:
                continue
            item = self.items[ident]
            profile = self.profiles[ident]
            signature = self.signatures[ident]
            if signature in signatures:
                continue
            entry = {"id": str(len(selected) + 1) if compact else ident, **profile}
            if not compact:
                entry["document"] = f"catalog/{self.categories[ident][0]}.md"
            if len(selected) >= limit:
                break
            if len(json.dumps([*entries, entry], ensure_ascii=False, separators=(",", ":"))) > budget:
                continue
            selected.append(item)
            entries.append(entry)
            signatures.add(signature)
        return tuple(selected), entries
