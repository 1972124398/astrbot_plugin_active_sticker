import json
import unittest
from dataclasses import replace

from astrbot_plugin_active_sticker.expression import selected_ids
from astrbot_plugin_active_sticker.library import Sticker
from astrbot_plugin_active_sticker.retrieval import (
    RESULT_CHARS,
    CatalogIndex,
    compact_profile,
    retrieval_fields,
)


def sticker(ident, description, **kwargs):
    return Sticker(f"{ident:016x}", f"{ident}.png", "png", False, description=description, **kwargs)


class RetrievalTest(unittest.TestCase):
    def test_field_weighting_prioritizes_tags_over_accidental_filename(self):
        target = sticker(1, "猫猫点头", tags=("收到",), categories=("回应",))
        filename = replace(sticker(2, "猫猫睡觉", categories=("日常",)), name="收到.png")
        found, _ = CatalogIndex([filename, target]).search("收到")
        self.assertEqual(found[0].id, target.id)

    def test_category_cues_find_synonyms_without_literal_overlap(self):
        item = sticker(1, "两眼弯弯", tags=("愉快",), categories=("开心",))
        found, _ = CatalogIndex([item]).search("happy")
        self.assertEqual(found, (item,))

    def test_explicit_categories_are_hard_filters_and_excludes_are_respected(self):
        happy = sticker(1, "笑着说晚安", categories=("开心",))
        sleep = sticker(2, "困了晚安", categories=("日常",))
        index = CatalogIndex([happy, sleep])
        self.assertEqual(index.search("晚安", ["日常"])[0], (sleep,))
        self.assertEqual(index.search("晚安", ["日常"], exclude=[sleep.id])[0], ())
        self.assertEqual(index.search("晚安", ["missing"])[0], ())

    def test_negated_usage_is_not_treated_as_positive_keyword_evidence(self):
        item = sticker(1, "猫猫挥手", use_when="适合告别。不要用于 rocket", categories=("日常",))
        index = CatalogIndex([item])
        self.assertNotIn("rocket", index.postings)
        self.assertIn("不要用于 rocket", index.profiles[item.id]["avoid"])

    def test_compact_profiles_preserve_recognized_tone_text_and_avoidance(self):
        item = sticker(1, "假装炸毛的猫，其实在撒娇", use_when="熟人拌嘴", tags=("炸毛",), categories=("调侃",))
        result = {
            "description": item.description,
            "use_when": item.use_when,
            "tags": item.tags,
            "categories": item.categories,
            "retrieval": {
                "meaning": "撒娇式假装生气",
                "tone": "调侃",
                "text": "哼！",
                "avoid": "不要用于严肃争执",
            },
        }
        item = replace(item, vision_result=result)
        index = CatalogIndex([item])
        self.assertEqual(index.profiles[item.id]["text"], "哼！")
        self.assertEqual(index.profiles[item.id]["tone"], "调侃")
        self.assertEqual(index.profiles[item.id]["avoid"], "不要用于严肃争执")
        edited = replace(item, description="人工确认是真生气", tags=("生气",))
        self.assertEqual(compact_profile(edited)["description"], "人工确认是真生气")
        self.assertNotIn("tone", compact_profile(edited))

    def test_legacy_long_descriptions_keep_the_ending_and_negative_limits(self):
        item = sticker(1, "画面细节" * 70 + "最后其实是在反讽", use_when="适合熟人。不要用于道歉")
        profile = compact_profile(item)
        self.assertTrue(profile["description"].endswith("最后其实是在反讽"))
        self.assertLessEqual(len(profile["description"]), 180)
        self.assertIn("不要用于道歉", profile["avoid"])

    def test_duplicates_do_not_consume_candidate_slots_but_text_variants_are_kept(self):
        first = sticker(1, "点头表示收到", tags=("收到",))
        duplicate = replace(first, id=f"{2:016x}")
        variant = sticker(3, "点头表示收到，谢谢你", tags=("谢谢",))
        found, entries = CatalogIndex([first, duplicate, variant]).search("收到", compact=True)
        self.assertEqual(len(found), 2)
        self.assertEqual([entry["id"] for entry in entries], ["1", "2"])
        self.assertTrue(all("document" not in entry for entry in entries))

    def test_candidate_payload_obeys_count_and_serialized_character_budget(self):
        items = [
            sticker(
                i,
                "开心" + str(i) + "画面细节" * 90,
                use_when="接梗" * 100,
                tags=("开心", str(i)),
                categories=("开心",),
            )
            for i in range(30)
        ]
        _, entries = CatalogIndex(items).search("开心", compact=True)
        self.assertLessEqual(len(entries), 8)
        self.assertGreater(len(entries), 0)
        self.assertLessEqual(len(json.dumps(entries, ensure_ascii=False, separators=(",", ":"))), RESULT_CHARS)

    def test_rank_cache_is_bounded_and_new_index_invalidates_old_wording(self):
        item = sticker(1, "hello", categories=("其他",))
        index = CatalogIndex([item])
        for number in range(150):
            index.search("hello " + str(number))
        self.assertEqual(len(index.cache), 128)
        result = index.rank("hello", ())
        self.assertIs(index.rank("hello", ()), result)
        new_index = CatalogIndex([replace(item, description="goodbye")])
        self.assertEqual(new_index.search("hello")[0], ())
        self.assertEqual(len(new_index.search("goodbye")[0]), 1)

    def test_short_ids_allow_repeated_bursts_and_reject_ids_from_other_batches(self):
        items = [sticker(1, "笑"), sticker(2, "哭")]
        self.assertEqual(
            selected_ids({"send": ["2", "1", "2"]}, items, 3), (items[1].id, items[0].id, items[1].id)
        )
        self.assertEqual(selected_ids({"send": ["3"]}, items, 6), ())
        self.assertEqual(selected_ids({"send": ["1", "../file"]}, items, 6), ())
        self.assertEqual(selected_ids({"send": [1]}, items, 6), ())

    def test_optional_recognition_fields_validate_shapes_and_limits(self):
        self.assertEqual(retrieval_fields({"retrieval": ["invalid"]}), {})
        self.assertEqual(
            retrieval_fields({"retrieval": {"tone": 2, "text": "  字幕\n中文 "}}), {"text": "字幕 中文"}
        )
        self.assertEqual(len(retrieval_fields({"retrieval": {"meaning": "字" * 200}})["meaning"]), 100)
