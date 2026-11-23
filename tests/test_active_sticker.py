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
from astrbot_plugin_active_sticker.expression import selected_ids, shortlist
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

    def get_result(self):
        return self.result


class StickerTest(unittest.IsolatedAsyncioTestCase):
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
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps({"send": [self.item.id, self.item.id]}),
        )
        event = Event()
        provider_request = ProviderRequest(prompt="真好笑", system_prompt="主对话提示", contexts=[])
        before = vars(provider_request).copy()
        await self.plugin.capture(event, provider_request)
        self.context.llm_generate.assert_not_called()
        await self.plugin.after_reply(event)
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertEqual(vars(provider_request), before)
        self.assertEqual(event.result.get_plain_text(), "笑得停不下来了")
        self.assertEqual(event.send.await_count, 2)
        self.assertEqual(self.context.llm_generate.await_count, 1)
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
        self.assertEqual({entry["id"] for entry in next_prompt["已发表情"]}, {self.item.id})
        self.assertTrue(all(entry["after_speaker"] == "小林" for entry in next_prompt["已发表情"]))

    async def test_streaming_waits_for_completion_and_model_may_choose_silence(self):
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='{"send":[]}')
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

    def test_recent_sticker_remains_available_for_a_callback_in_a_large_library(self):
        others = tuple(replace(self.item, id=str(index), description="大家都笑疯了") for index in range(100))
        picked = shortlist((*others, self.item), "笑疯了", 3, recent_ids=(self.item.id, "removed"))
        self.assertEqual(len(picked), 48)
        self.assertIn(self.item, picked)

    def test_invalid_model_ids_cannot_select_files(self):
        self.assertEqual(selected_ids('{"send":["../../example"]}', [self.item], 6), ())

    def test_animated_vision_preview_keeps_multiple_frames(self):
        path = self.library.root / "animated.gif"
        first = Image.new("RGB", (24, 24), "red")
        second = Image.new("RGB", (24, 24), "green")
        first.save(path, save_all=True, append_images=[second])
        with Image.open(io.BytesIO(vision_image(path))) as strips:
            self.assertEqual(strips.size, (48, 24))
