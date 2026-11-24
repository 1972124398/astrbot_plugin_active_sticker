"""Local category documents and a cached text index for small retrieval results."""

import json
import re
from collections import defaultdict

CATEGORIES = {
    "开心": "开心 高兴 笑 哈哈 爆笑 欢乐 得意 happy laugh",
    "难过": "难过 伤心 哭 委屈 失落 悲伤 安慰 sad cry",
    "生气": "生气 愤怒 炸毛 气愤 怒 吵架 angry",
    "惊讶": "惊讶 吃惊 震惊 疑惑 问号 懵 surprise confused",
    "尴尬": "尴尬 无语 汗 捂脸 社死 害羞 awkward",
    "亲近": "亲亲 抱抱 撒娇 卖萌 贴贴 爱心 喜欢 可爱 hug love",
    "调侃": "调侃 嘲讽 阴阳 搞怪 整活 坏笑 接梗 逗 joke tease",
    "回应": "点头 摇头 同意 拒绝 收到 确认 谢谢 对不起 好的 OK yes no",
    "日常": "早安 晚安 睡觉 吃饭 工作 上班 加油 拜拜 再见 morning sleep",
    "其他": "",
}


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
        if len(span) == 1:
            found.add(span)
    return found


def inferred_categories(item):
    if item.categories:
        return item.categories
    text = " ".join((item.description, item.use_when, " ".join(item.tags))).lower()
    return tuple(
        name for name, cues in CATEGORIES.items() if any(cue.lower() in text for cue in cues.split())
    ) or ("其他",)


class CatalogIndex:
    def __init__(self, items):
        self.items = {item.id: item for item in items}
        self.postings = defaultdict(set)
        self.groups = defaultdict(set)
        self.categories = {}
        for item in items:
            categories = inferred_categories(item)
            self.categories[item.id] = categories
            for category in categories:
                self.groups[category].add(item.id)
            for word in terms(" ".join((item.name, item.description, item.use_when, " ".join(item.tags)))):
                self.postings[word].add(item.id)

    def documents(self):
        return [
            {"分类": name, "条目数": len(self.groups[name]), "文档": f"catalog/{name}.md"}
            for name in CATEGORIES
            if self.groups[name]
        ]

    def search(self, query, categories=(), *, exclude=(), limit=12):
        scores = defaultdict(int)
        for word in terms(query):
            for ident in self.postings.get(word, ()):
                scores[ident] += 1
        grouped = set()
        for category in categories:
            grouped.update(self.groups.get(category, ()))
        pool = (grouped or set(scores)) - set(exclude)
        ranked = sorted(pool, key=lambda ident: (-scores[ident], ident))
        selected = []
        entries = []
        size = 0
        for ident in ranked:
            item = self.items[ident]
            entry = {
                "id": item.id,
                "description": item.description[:220],
                "use_when": item.use_when[:140],
                "tags": item.tags[:6],
                "document": f"catalog/{self.categories[ident][0]}.md",
            }
            entry_size = len(json.dumps(entry, ensure_ascii=False))
            if len(selected) >= limit or size + entry_size > 6000:
                break
            selected.append(item)
            entries.append(entry)
            size += entry_size
        return tuple(selected), entries
