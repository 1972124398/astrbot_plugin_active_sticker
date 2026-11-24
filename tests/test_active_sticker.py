import asyncio
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.message_event_result import ResultContentType
from astrbot_plugin_active_sticker.expression import INTENT_PROMPT, selected_ids
from astrbot_plugin_active_sticker.library import StickerLibrary, inspect_image, vision_image
from astrbot_plugin_active_sticker.main import ActiveSticker
from PIL import Image


def sticker_image(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (30, 30), color).save(buffer, "PNG")
    return buffer.getvalue()


class Event:
    unified_msg_origin = "test:GroupMessage:library"
    _has_send_oper = True

    def __init__(self, result_type=ResultContentType.LLM_RESULT):
        self.extras = {"_trace_persona_id": "friend"}
        self.message_obj = SimpleNamespace(message_id="normal-message")
        self.result = MessageEventResult([Plain("笑得停不下来了")], result_content_type=result_type)
        self.send = AsyncMock()

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_message_outline(self):
        return "这个梗也太好笑了"

    def get_sender_name(self):
        return "小林"

    def get_group_id(self):
        return "library"

    def get_platform_name(self):
        return "test"

    def is_stopped(self):
        return self.result.is_stopped()

    def get_result(self):
        return self.result


class StickerTest(unittest.IsolatedAsyncioTestCase):
    def expression_model(self, selected, meaning="跟小林一起笑场"):
        async def respond(**kwargs):
            if kwargs["system_prompt"] == INTENT_PROMPT:
                return SimpleNamespace(completion_text=json.dumps({"want": True, "meaning": meaning}))
            return SimpleNamespace(completion_text=json.dumps({"send": selected}))

        self.context.llm_generate.side_effect = respond

    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.stars = {}
        self.context = SimpleNamespace(
            get_registered_star=self.stars.get,
            get_all_providers=lambda: [
                SimpleNamespace(
                    meta=lambda: SimpleNamespace(id="vision", model="vision"),
                    provider_config={"modalities": ["image"]},
                ),
            ],
            get_config=lambda **_: {"plugin_set": ["*"]},
            register_web_api=lambda *_: None,
            persona_manager=SimpleNamespace(get_persona_v3_by_id=lambda _: {"prompt": "遇到好梗会狂笑"}),
            get_current_chat_provider_id=AsyncMock(return_value="test"),
            llm_generate=AsyncMock(),
        )
        with patch("astrbot_plugin_active_sticker.main.StarTools.get_data_dir", return_value=Path(folder.name)):
            self.plugin = ActiveSticker(self.context, {"burst_limit": 6, "vision_provider_id": "vision"})
        self.addAsyncCleanup(self.plugin.terminate)
        self.library = self.plugin.library
        data = sticker_image()
        item, _ = self.library.add("笑出屏幕.png", data, inspect_image(data))
        self.item = self.library.update(item, description="忍不住爆笑", tags=("笑场",))

    async def test_post_reply_model_observes_persona_and_optional_mood_then_sends_burst(self):
        self.stars["astrbot_plugin_short_term_mood"] = SimpleNamespace(
            activated=True,
            star_cls=SimpleNamespace(companion_snapshot=lambda _: {"inner_state": "笑出眼泪"}),
        )
        self.expression_model([self.item.id, self.item.id])
        event = Event()
        provider_request = ProviderRequest(prompt="真好笑", system_prompt="主对话提示", contexts=[])
        before = vars(provider_request).copy()
        await self.plugin.capture(event, provider_request)
        self.context.llm_generate.assert_not_called()
        self.assertIn("本地表情库", provider_request.extra_user_content_parts[0].text)
        before["extra_user_content_parts"] = provider_request.extra_user_content_parts
        await self.plugin.after_reply(event)
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertEqual(vars(provider_request), before)
        self.assertEqual(event.result.get_plain_text(), "笑得停不下来了")
        self.assertEqual(event.send.await_count, 2)
        self.assertEqual(self.context.llm_generate.await_count, 2)
        prompt = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertEqual(prompt["人格"], "遇到好梗会狂笑")
        self.assertEqual(prompt["当前情绪"], {"inner_state": "笑出眼泪"})
        self.assertEqual(prompt["当前说话人"], "小林")
        self.assertEqual(prompt["聊天场合"], "群聊")
        next_event = Event()
        await self.plugin.capture(next_event, ProviderRequest())
        await self.plugin.after_reply(next_event)
        await asyncio.gather(*list(self.plugin.tasks))
        next_prompt = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertEqual({entry["id"] for entry in next_prompt["自己已发的表情"]}, {self.item.id})
        self.assertTrue(all(entry["after_speaker"] == "小林" for entry in next_prompt["自己已发的表情"]))

    async def test_chat_receives_only_delivered_self_expression_in_the_same_conversation(self):
        data = sticker_image("blue")
        second, _ = self.library.add("另一张.png", data, inspect_image(data))
        second = self.library.update(second, description="已经笑翻过去")
        self.expression_model([self.item.id, second.id], meaning="跟小林一起爆笑，笑翻过去")
        event = Event()
        event.send.side_effect = [None, False]
        conversation = SimpleNamespace(cid="current-chat")
        await self.plugin.capture(event, ProviderRequest(conversation=conversation))
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertEqual(event.send.await_count, 2)
        self.assertEqual(len(event.send.await_args_list[0].args[0].chain), 1)

        followup = ProviderRequest(prompt="你发的图是什么意思？", conversation=conversation)
        await self.plugin.capture(Event(), followup)
        (note,) = followup.extra_user_content_parts
        self.assertTrue(note.model_dump_for_context()["_no_save"])
        self.assertIn("你在这些回复后亲自选择并发出了表情", note.text)
        self.assertIn("跟小林一起爆笑", note.text)
        self.assertIn("笑得停不下来了", note.text)
        self.assertIn("忍不住爆笑", note.text)
        self.assertNotIn("已经笑翻过去", note.text)
        self.assertEqual(followup.prompt, "你发的图是什么意思？")
        self.assertEqual(self.context.llm_generate.await_count, 2)

        another_conversation = ProviderRequest(conversation=SimpleNamespace(cid="new-chat"))
        await self.plugin.capture(Event(), another_conversation)
        self.assertNotIn("亲自选择并发出了", another_conversation.extra_user_content_parts[0].text)

        another_persona = Event()
        another_persona.set_extra("_trace_persona_id", "other-persona")
        another_request = ProviderRequest(conversation=conversation)
        await self.plugin.capture(another_persona, another_request)
        self.assertNotIn("亲自选择并发出了", another_request.extra_user_content_parts[0].text)

    async def test_streaming_waits_for_completion_and_model_may_choose_silence(self):
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='{"want":false}')
        event = Event(ResultContentType.STREAMING_RESULT)
        await self.plugin.capture(event, ProviderRequest())
        await self.plugin.stream_finished(event)
        await self.plugin.after_reply(event)
        self.context.llm_generate.assert_not_called()
        event.result.result_content_type = ResultContentType.STREAMING_FINISH
        await self.plugin.stream_finished(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.context.llm_generate.assert_awaited_once()
        event.send.assert_not_called()

    async def test_new_turn_prevents_before_the_late_choice_is_sent(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def model(**_):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text=json.dumps({"send": [self.item.id]}))

        self.context.llm_generate.side_effect = model
        event = Event()
        await self.plugin.capture(event, ProviderRequest())
        await self.plugin.after_reply(event)
        await asyncio.wait_for(started.wait(), 3)
        await self.plugin.capture(Event(), ProviderRequest())
        release.set()
        await asyncio.gather(*list(self.plugin.tasks))
        event.send.assert_not_called()

    async def test_indexing_uses_a_picture_and_persists_the_catalog(self):
        data = sticker_image("green")
        item, added = self.library.add("新梗.png", data, inspect_image(data))
        self.assertTrue(added)
        self.assertFalse(self.library.add("copy.png", data, inspect_image(data))[1])

        async def vision(**kwargs):
            self.assertEqual(kwargs["chat_provider_id"], "vision")
            with Image.open(kwargs["image_urls"][0]) as preview:
                self.assertEqual(preview.format, "PNG")
            return SimpleNamespace(completion_text='{"description":"得意","use_when":"赢了一局","tags":["笑"]}')

        self.context.llm_generate.side_effect = vision
        self.plugin.queue_index([item])
        await self.plugin.worker
        rebuilt = StickerLibrary(self.library.root)
        self.assertEqual(rebuilt.items[item.id].use_when, "赢了一局")
        result = self.plugin.companion_sticker_catalog(Event.unified_msg_origin)
        self.assertEqual(result["items"][-1]["description"], "得意")
        self.plugin.config["link_self_cognition"] = False
        self.assertEqual(self.plugin.companion_sticker_catalog(Event.unified_msg_origin), {})
        self.assertIn("赢了一局", (self.library.root / "catalog.md").read_text("utf-8"))

    async def test_manual_edit_wins_over_hanging_recognition(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def model(**_):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text='{"description":"模型描述"}')

        self.context.llm_generate.side_effect = model
        self.plugin.queue_index([self.item])
        await asyncio.wait_for(started.wait(), 3)
        self.library.update(self.item, description="人工描述")
        release.set()
        await self.plugin.worker
        self.assertEqual(self.library.items[self.item.id].description, "人工描述")

    async def test_pending_import_waits_for_an_available_vision_model(self):
        content = sticker_image("blue")
        item, _ = self.library.add("蓝.png", content, inspect_image(content))
        self.plugin.config["vision_provider_id"] = "removed"
        self.assertEqual(self.plugin.index_unrecognized(), 0)
        self.assertFalse(self.plugin.pending)
        self.context.llm_generate.assert_not_called()
        self.plugin.config["vision_provider_id"] = ""
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='{"description":"有点惊讶"}')
        self.assertEqual(self.plugin.index_unrecognized(), 1)
        self.assertEqual(self.plugin.index_unrecognized(), 0)
        await self.plugin.worker
        self.assertEqual(self.library.items[item.id].description, "有点惊讶")

    async def test_declining_intent_never_searches_or_offers_the_library(self):
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='{"want":false}')
        event = Event()
        await self.plugin.capture(event, ProviderRequest())
        with patch.object(self.library.search_index, "search", side_effect=AssertionError("unexpected search")):
            await self.plugin.after_reply(event)
            await asyncio.gather(*list(self.plugin.tasks))
        self.context.llm_generate.assert_awaited_once()
        prompt = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertNotIn("文档匹配条目", prompt)
        self.assertNotIn("分类文档目录", prompt)
        self.assertNotIn(self.item.id, json.dumps(prompt))
        event.send.assert_not_called()

    async def test_intent_can_find_no_suitable_image_without_sending(self):
        self.expression_model([], meaning="真诚的安慰")
        event = Event()
        await self.plugin.capture(event, ProviderRequest())
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertEqual(self.context.llm_generate.await_count, 2)
        event.send.assert_not_called()
        self.assertFalse(event.get_extra("astrbot_plugin_active_sticker.turn").session.history)

    async def test_ai_can_change_search_words_and_read_a_category_document(self):
        self.context.llm_generate.side_effect = [
            SimpleNamespace(completion_text='{"want":true,"meaning":"很开心，想接着这个笑话笑"}'),
            SimpleNamespace(completion_text='{"search":{"query":"爆笑","categories":["开心"]}}'),
            SimpleNamespace(completion_text=json.dumps({"send": [self.item.id]})),
        ]
        event = Event()
        await self.plugin.capture(event, ProviderRequest())
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertEqual(self.context.llm_generate.await_count, 3)
        prompt = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertEqual(prompt["本次检索"]["categories"], ["开心"])
        self.assertEqual(prompt["文档匹配条目"][0]["document"], "catalog/开心.md")
        event.send.assert_awaited_once()

    def test_large_library_returns_small_category_results_and_migrates_old_descriptions(self):
        others = tuple(
            replace(self.item, id=f"{index:016x}", description="难过流泪", categories=("难过",))
            for index in range(100)
        )
        self.library.commit({item.id: item for item in (*others, self.item)})
        rebuilt = StickerLibrary(self.library.root)
        matches, entries = rebuilt.search_index.search("爆笑", ("开心",))
        self.assertEqual([item.id for item in matches], [self.item.id])
        matches, entries = rebuilt.search_index.search("难过", ("难过",))
        self.assertLessEqual(len(matches), 12)
        self.assertLessEqual(len(json.dumps(entries, ensure_ascii=False)), 6100)
        self.assertIn(self.item.id, (rebuilt.root / "catalog/开心.md").read_text("utf-8"))
        self.assertNotIn(self.item.id, (rebuilt.root / "library.md").read_text("utf-8"))

    def test_legacy_category_uses_tags_instead_of_negative_usage_examples(self):
        item = self.library.update(
            self.item,
            description="满头问号，不是生气或伤心",
            tags=("困惑", "问号"),
            use_when="不适合生气时用，容易误会成撒娇",
        )
        self.assertEqual(self.library.search_index.categories[item.id], ("惊讶",))

    async def test_successful_expression_survives_a_plugin_reload(self):
        self.expression_model([self.item.id])
        event = Event()
        conversation = SimpleNamespace(cid="saved-chat")
        await self.plugin.capture(event, ProviderRequest(conversation=conversation))
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        with patch("astrbot_plugin_active_sticker.main.StarTools.get_data_dir", return_value=self.library.root):
            reloaded = ActiveSticker(self.context, self.plugin.config)
        request = ProviderRequest(conversation=conversation)
        await reloaded.capture(Event(), request)
        self.assertIn("忍不住爆笑", request.extra_user_content_parts[0].text)
        self.assertIn("跟小林一起笑场", request.extra_user_content_parts[0].text)
        await reloaded.terminate()

    def test_invalid_model_ids_cannot_select_files(self):
        self.assertEqual(selected_ids({"send": ["../../example"]}, [self.item], 6), ())

    def test_animated_vision_preview_keeps_multiple_frames(self):
        path = self.library.root / "animated.gif"
        first = Image.new("RGB", (24, 24), "red")
        second = Image.new("RGB", (24, 24), "green")
        first.save(path, save_all=True, append_images=[second])
        with Image.open(io.BytesIO(vision_image(path))) as strips:
            self.assertEqual(strips.size, (48, 24))
