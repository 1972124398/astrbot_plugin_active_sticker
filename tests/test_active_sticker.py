import asyncio
import base64
import csv
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.message_event_result import ResultContentType
from astrbot_plugin_active_sticker.exports import csv_bytes, csv_cell, write_export
from astrbot_plugin_active_sticker.expression import CHAT_EXPRESSION_PROMPT, INTENT_PROMPT, selected_ids
from astrbot_plugin_active_sticker.library import StickerLibrary, inspect_image, vision_image, wechat_animation_gif
from astrbot_plugin_active_sticker.main import ActiveSticker
from astrbot_plugin_active_sticker.styles import choose_pack
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

    async def test_companion_cleanup_invalidates_turn_and_removes_receipts(self):
        event = Event()
        self.expression_model([self.item.id])
        await self.plugin.capture(event, ProviderRequest(prompt="真好笑", contexts=[]))
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertTrue(self.plugin.sessions[event.unified_msg_origin].history)
        self.assertEqual(await self.plugin.companion_clear(event.unified_msg_origin, persona="friend"), "cleared")
        self.assertNotIn(event.unified_msg_origin, self.plugin.sessions)
        self.assertIn(self.item.id, self.library.items)
        self.plugin.load_sent_stickers()
        self.assertNotIn(event.unified_msg_origin, self.plugin.sessions)

    async def test_cancelled_receipt_write_cannot_overwrite_conversation_cleanup(self):
        event = Event()
        await self.plugin.capture(event, ProviderRequest(prompt="真好笑", contexts=[]))
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        write = self.library.write

        def delayed_write(path, data):
            if json.loads(data):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=3):
                    raise TimeoutError("Test did not release the receipt writer")
            write(path, data)

        with patch.object(self.library, "write", side_effect=delayed_write):
            save = asyncio.create_task(self.plugin.save_sent_stickers())
            cleanup = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                for _ in range(2):
                    save.cancel()
                    await asyncio.sleep(0)
                cleanup = asyncio.create_task(self.plugin.companion_clear(event.unified_msg_origin, persona="friend"))
                await asyncio.sleep(0)
                self.assertTrue(self.plugin.receipts_lock.locked())
                self.assertFalse(save.done())
            finally:
                release.set()
                await asyncio.gather(save, return_exceptions=True)
                if cleanup is not None:
                    await cleanup
        self.assertTrue(save.cancelled())
        self.assertEqual(json.loads(self.plugin.receipts_path.read_bytes()), {})

    async def test_wechat_native_uses_gif_sticker_path_only_when_enabled(self):
        buffer = io.BytesIO()
        first = Image.new("RGB", (24, 24), "red")
        second = Image.new("RGB", (24, 24), "green")
        first.save(buffer, format="WEBP", save_all=True, append_images=[second], duration=[80, 140], loop=2)
        original = buffer.getvalue()
        item, _ = self.library.add("动画.webp", original, inspect_image(original))
        item = self.library.update(item, description="一段动图")

        for platform, enabled, compression, converted in (
            ("wechat_native", True, "auto", True),
            ("wechat_native", False, "auto", False),
            ("wechat_native", True, "none", False),
            ("wechat_native", False, "none", False),
            ("test", True, "auto", False),
        ):
            with self.subTest(platform=platform, enabled=enabled, compression=compression):
                event = Event()
                event.get_platform_name = lambda: platform
                event.adapter = SimpleNamespace(
                    native_config=SimpleNamespace(send_gif_as_sticker=enabled, gif_sticker_compression=compression)
                )
                self.expression_model([item.id])
                await self.plugin.capture(event, ProviderRequest())
                with patch("astrbot_plugin_active_sticker.main.asyncio.wait_for", wraps=asyncio.wait_for) as wait:
                    await self.plugin.after_reply(event)
                    await asyncio.gather(*list(self.plugin.tasks))
                event.send.assert_awaited_once()
                timeouts = [call.kwargs.get("timeout") for call in wait.call_args_list]
                self.assertEqual(8 in timeouts, platform != "wechat_native")
                (component,) = event.send.await_args.args[0].chain
                if converted:
                    data = base64.b64decode(component.file.removeprefix("base64://"))
                    with Image.open(io.BytesIO(data)) as gif:
                        self.assertEqual((gif.format, gif.n_frames, gif.info["loop"]), ("GIF", 2, 2))
                        self.assertEqual(gif.info["duration"], 80)
                        gif.seek(1)
                        self.assertEqual(gif.info["duration"], 140)
                else:
                    self.assertEqual(Path(component.path), self.library.path(item))
        self.assertEqual(self.library.path(item).read_bytes(), original)

    def test_wechat_apng_conversion_skips_poster_frame(self):
        path = self.library.root / "animation.png"
        poster = Image.new("RGBA", (24, 24), "red")
        first = Image.new("RGBA", (24, 24), "green")
        second = Image.new("RGBA", (24, 24), "blue")
        poster.save(path, save_all=True, append_images=[first, second], default_image=True, duration=[70, 160])
        with Image.open(io.BytesIO(wechat_animation_gif(path))) as gif:
            self.assertEqual(gif.n_frames, 2)
            self.assertEqual(gif.convert("RGB").getpixel((0, 0)), (0, 128, 0))
            self.assertEqual(gif.info["duration"], 70)
            gif.seek(1)
            self.assertEqual(gif.info["duration"], 160)

    async def test_wechat_sends_original_gif_bytes_without_conversion(self):
        buffer = io.BytesIO()
        frames = [Image.new("RGB", (24, 24), color) for color in ("red", "green")]
        frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=[80, 140])
        original = buffer.getvalue()
        item, _ = self.library.add("动画.gif", original, inspect_image(original))
        item = self.library.update(item, description="原始 GIF")
        event = Event()
        event.get_platform_name = lambda: "wechat_native"
        event.adapter = SimpleNamespace(
            native_config=SimpleNamespace(send_gif_as_sticker=True, gif_sticker_compression="auto")
        )
        self.expression_model([item.id], meaning="原始 GIF")
        await self.plugin.capture(event, ProviderRequest())
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        event.send.assert_awaited_once()
        (component,) = event.send.await_args.args[0].chain
        self.assertEqual(Path(component.path).read_bytes(), original)

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
        self.assertEqual(provider_request.system_prompt, "主对话提示\n\n" + CHAT_EXPRESSION_PROMPT)
        before["system_prompt"] = provider_request.system_prompt
        before["extra_user_content_parts"] = provider_request.extra_user_content_parts
        await self.plugin.after_reply(event)
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertEqual(vars(provider_request), before)
        self.assertEqual(event.result.get_plain_text(), "笑得停不下来了")
        self.assertEqual(event.send.await_count, 2)
        self.assertEqual(self.context.llm_generate.await_count, 2)
        prompt = json.loads(self.context.llm_generate.await_args_list[0].kwargs["prompt"])
        self.assertEqual(prompt["人格"], "遇到好梗会狂笑")
        self.assertEqual(prompt["当前情绪"], {"inner_state": "笑出眼泪"})
        self.assertEqual(prompt["当前说话人"], "小林")
        self.assertEqual(prompt["聊天场合"], "群聊")
        next_event = Event()
        await self.plugin.capture(next_event, ProviderRequest())
        await self.plugin.after_reply(next_event)
        await asyncio.gather(*list(self.plugin.tasks))
        next_prompt = json.loads(self.context.llm_generate.await_args_list[-2].kwargs["prompt"])
        self.assertEqual({entry["id"] for entry in next_prompt["自己已发的表情"]}, {self.item.id})
        self.assertTrue(all(entry["after_speaker"] == "小林" for entry in next_prompt["自己已发的表情"]))

    async def test_priority_is_automatic_per_request_without_changing_character_or_tools(self):
        event = Event()
        event.set_extra("_trace_persona_id", None)
        request = ProviderRequest(prompt="发个表情", system_prompt="话少，偶尔接梗")
        original_tools = request.func_tool
        await self.plugin.capture(event, request)
        self.assertTrue(request.system_prompt.startswith("话少，偶尔接梗\n\n"))
        self.assertIn(CHAT_EXPRESSION_PROMPT, request.system_prompt)
        self.assertEqual(event.get_extra("astrbot_plugin_active_sticker.turn").persona, "话少，偶尔接梗")
        self.assertIs(request.func_tool, original_tools)
        self.assertEqual(request.prompt, "发个表情")
        (note,) = request.extra_user_content_parts
        self.assertTrue(note.model_dump_for_context()["_no_save"])
        self.assertIn("1 张可用", note.text)
        await self.plugin.capture(event, request)
        self.assertEqual(request.system_prompt.count(CHAT_EXPRESSION_PROMPT), 1)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        fresh = ProviderRequest()
        await self.plugin.capture(Event(), fresh)
        self.assertEqual(fresh.system_prompt, CHAT_EXPRESSION_PROMPT)
        self.context.llm_generate.assert_not_called()

    async def test_priority_is_absent_when_no_images_can_be_sent(self):
        for changes in ({"enabled": False}, {"description": ""}):
            with self.subTest(changes=changes):
                self.library.update(self.library.items[self.item.id], **changes)
                request = ProviderRequest(system_prompt="原人格")
                await self.plugin.capture(Event(), request)
                self.assertEqual(request.system_prompt, "原人格")
                self.assertFalse(request.extra_user_content_parts)
                self.library.update(self.library.items[self.item.id], enabled=True, description="忍不住爆笑")
        self.library.path(self.item).unlink()
        request = ProviderRequest(system_prompt="原人格")
        await self.plugin.capture(Event(), request)
        self.assertEqual(request.system_prompt, "原人格")
        self.assertFalse(request.extra_user_content_parts)

    async def test_disabled_sessions_and_bridge_events_receive_no_priority(self):
        for kind in ("disabled", "not_allowed", "bridge"):
            with self.subTest(kind=kind):
                self.plugin.config["enabled"] = kind != "disabled"
                self.context.get_config = lambda **_: {"plugin_set": [] if kind == "not_allowed" else ["*"]}
                event = Event()
                if kind == "bridge":
                    event.set_extra("codex_bridge", True)
                request = ProviderRequest(system_prompt="原人格")
                await self.plugin.capture(event, request)
                self.assertEqual(request.system_prompt, "原人格")
                self.assertFalse(request.extra_user_content_parts)
                self.assertIsNone(event.get_extra("astrbot_plugin_active_sticker.turn"))

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
        self.assertEqual(self.library.items[self.item.id].vision_result["description"], "模型描述")

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
            SimpleNamespace(completion_text='{"want":true,"meaning":"xyznotfound"}'),
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
        self.assertEqual(prompt["分类文档目录"], {"开心": 1})
        self.assertEqual(prompt["文档匹配条目"][0]["id"], "1")
        self.assertNotIn("document", prompt["文档匹配条目"][0])
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

    def test_legacy_library_migrates_without_losing_images_or_descriptions(self):
        from dataclasses import asdict

        row = asdict(self.item)
        row.pop("pack_id")
        row.pop("vision_result")
        self.library.index.write_text(json.dumps([row]), "utf-8")
        migrated = StickerLibrary(self.library.root)
        self.assertEqual(migrated.items[self.item.id].pack_id, "default")
        self.assertEqual(migrated.available(), (self.item,))
        pack = migrated.save_pack("猫猫", "猫猫风格")
        migrated.organize([self.item.id], pack_id=pack.id)
        reloaded = StickerLibrary(self.library.root)
        self.assertEqual(reloaded.items[self.item.id].pack_id, pack.id)
        self.assertEqual(reloaded.packs[pack.id].name, "猫猫")

    def test_pack_management_preserves_files_and_exports_reviewable_metadata(self):
        pack = self.library.save_pack("猫猫")
        self.library.organize([self.item.id], pack_id=pack.id, enabled=False)
        with zipfile.ZipFile(self.library.export_pack(pack.id)) as archive:
            manifest = json.loads(archive.read("pack.json"))
            self.assertFalse(manifest["items"][0]["enabled"])
            self.assertEqual(manifest["items"][0]["description"], self.item.description)
            self.assertIn(f"images/{self.item.id}.png", archive.namelist())
        self.library.delete_pack(pack.id)
        self.assertEqual(self.library.items[self.item.id].pack_id, "default")
        self.assertTrue(self.library.path(self.item).is_file())
        with self.assertRaises(ValueError):
            self.library.delete_pack("default")
        with self.assertRaises(ValueError):
            self.library.save_pack("未分组")
        with self.assertRaises(ValueError):
            self.library.organize(["missing", self.item.id], enabled=True)
        self.assertFalse(self.library.items[self.item.id].enabled)

    def test_idle_refresh_rolls_once_and_keeps_continuous_dialogue_in_one_pack(self):
        leases = OrderedDict()
        with (
            patch("astrbot_plugin_active_sticker.styles.random.choice", side_effect=lambda choices: choices[0]),
            patch("astrbot_plugin_active_sticker.styles.random.random", return_value=0.25) as roll,
        ):
            self.assertEqual(choose_pack(leases, "chat", ["a", "b"], 0, 21600, 0.5), "a")
            self.assertEqual(choose_pack(leases, "chat", ["a", "b"], 21000, 21600, 0.5), "a")
            self.assertEqual(choose_pack(leases, "chat", ["a", "b"], 22000, 21600, 0.5), "a")
            roll.assert_not_called()
            self.assertEqual(choose_pack(leases, "chat", ["a", "b"], 43600, 21600, 0.5), "b")
            self.assertEqual(choose_pack(leases, "chat", ["a", "b"], 43601, 21600, 0.5), "b")
            roll.assert_called_once()
            self.assertEqual(choose_pack(leases, "other-persona", ["a", "b"], 43601, 21600, 0.5), "a")

    def test_zero_probability_one_pack_and_unavailable_pack_recovery(self):
        leases = OrderedDict()
        self.assertEqual(choose_pack(leases, "chat", ["a"], 0, 1, 1), "a")
        self.assertEqual(choose_pack(leases, "chat", ["a"], 2, 1, 1), "a")
        self.assertEqual(choose_pack(leases, "chat", ["a", "b"], 4, 1, 0), "a")
        self.assertEqual(choose_pack(leases, "chat", ["b"], 5, 1, 0), "b")
        self.assertEqual(choose_pack(leases, "chat", [], 6, 1, 0), "")

    async def test_locked_pack_filters_every_search_and_rejects_foreign_model_ids(self):
        pack = self.library.save_pack("另一种画风")
        data = sticker_image("blue")
        foreign, _ = self.library.add("外包.png", data, inspect_image(data), pack_id=pack.id)
        foreign = self.library.update(foreign, description="爆笑", categories=("开心",))
        self.expression_model([foreign.id])
        event = Event()
        with patch("astrbot_plugin_active_sticker.styles.random.choice", return_value="default"):
            await self.plugin.capture(event, ProviderRequest())
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        prompt = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertNotIn(foreign.id, json.dumps(prompt))
        self.assertEqual({entry["id"] for entry in prompt["文档匹配条目"]}, {"1"})
        event.send.assert_not_called()

    async def test_moving_or_disabling_chosen_pack_during_selection_prevents_delivery(self):
        pack = self.library.save_pack("另一种画风")
        for operation in ("move", "disable"):
            with self.subTest(operation=operation):
                self.library.organize([self.item.id], pack_id="default")
                self.library.save_pack("未分组", enabled=True, ident="default")

                async def respond(**kwargs):
                    if kwargs["system_prompt"] == INTENT_PROMPT:
                        return SimpleNamespace(completion_text='{"want":true,"meaning":"爆笑"}')
                    if operation == "move":
                        self.library.organize([self.item.id], pack_id=pack.id)
                    else:
                        self.library.save_pack("未分组", enabled=False, ident="default")
                    return SimpleNamespace(completion_text=json.dumps({"send": [self.item.id]}))

                self.context.llm_generate.side_effect = respond
                event = Event()
                await self.plugin.capture(event, ProviderRequest())
                await self.plugin.after_reply(event)
                await asyncio.gather(*list(self.plugin.tasks))
                event.send.assert_not_called()

    async def test_pack_choice_is_saved_even_when_ai_declines_expression(self):
        pack = self.library.save_pack("猫猫")
        self.library.organize([self.item.id], pack_id=pack.id)
        event = Event()
        await self.plugin.capture(event, ProviderRequest())
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='{"want":false}')
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        with patch("astrbot_plugin_active_sticker.main.StarTools.get_data_dir", return_value=self.library.root):
            reloaded = ActiveSticker(self.context, self.plugin.config)
        self.addAsyncCleanup(reloaded.terminate)
        self.assertFalse(next(iter(reloaded.sessions.values())).history)
        self.assertEqual(next(iter(next(iter(reloaded.sessions.values())).packs.values())).pack_id, pack.id)

    async def test_default_idle_is_six_hours_and_missing_original_recovers_at_next_turn(self):
        pack = self.library.save_pack("猫猫")
        data = sticker_image("blue")
        other, _ = self.library.add("另一包.png", data, inspect_image(data), pack_id=pack.id)
        self.library.update(other, description="笑出声")
        self.plugin.config["pack_switch_percent"] = 100
        with (
            patch("astrbot_plugin_active_sticker.styles.random.choice", side_effect=lambda choices: choices[0]),
            patch("astrbot_plugin_active_sticker.main.time.time", return_value=100),
        ):
            first = Event()
            await self.plugin.capture(first, ProviderRequest())
        self.assertEqual(first.get_extra("astrbot_plugin_active_sticker.turn").pack_id, "default")
        with patch("astrbot_plugin_active_sticker.main.time.time", return_value=100 + 21599):
            second = Event()
            await self.plugin.capture(second, ProviderRequest())
        self.assertEqual(second.get_extra("astrbot_plugin_active_sticker.turn").pack_id, "default")
        with patch("astrbot_plugin_active_sticker.main.time.time", return_value=100 + 21599 + 21600):
            third = Event()
            await self.plugin.capture(third, ProviderRequest())
        self.assertEqual(third.get_extra("astrbot_plugin_active_sticker.turn").pack_id, pack.id)
        self.library.path(other).unlink()
        fourth = Event()
        await self.plugin.capture(fourth, ProviderRequest())
        self.assertEqual(fourth.get_extra("astrbot_plugin_active_sticker.turn").pack_id, "default")

    async def test_all_packs_disabled_advertises_no_available_sticker_capability(self):
        self.library.save_pack("未分组", enabled=False, ident="default")
        request = ProviderRequest(system_prompt="原人格")
        event = Event()
        await self.plugin.capture(event, request)
        await self.plugin.after_reply(event)
        self.assertEqual(request.system_prompt, "原人格")
        self.context.llm_generate.assert_not_called()

    async def test_export_snapshot_survives_pack_deletion_and_download_is_cleaned(self):
        pack = self.library.save_pack("猫猫")
        self.library.organize([self.item.id], pack_id=pack.id)
        snapshot = self.library.pack_snapshot(pack.id)
        self.library.delete_pack(pack.id)
        path = await asyncio.to_thread(self.library.export_pack, pack.id, snapshot)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(json.loads(archive.read("pack.json"))["items"][0]["pack_id"], pack.id)
            self.assertIn(f"images/{self.item.id}.png", archive.namelist())
        request = SimpleNamespace(query={"id": "default"})
        with patch("astrbot_plugin_active_sticker.main.request", request):
            response = await self.plugin.web_export_pack()
        self.assertTrue(Path(response.path).is_file())
        await response.background()
        self.assertFalse(Path(response.path).exists())

    async def test_csv_covers_all_images_and_preserves_auto_results_after_manual_edit(self):
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text='{"description":"自动识图：开心猫猫","use_when":"朋友接梗",'
            '"tags":["猫猫","笑场"],"categories":["开心","调侃"]}'
        )
        self.plugin.queue_index([self.item])
        await self.plugin.worker
        item = self.library.items[self.item.id]
        self.library.update(item, name='猫猫,"表情".png', description="人工描述\n第二行", enabled=False)
        pack = self.library.save_pack("不启用的风格", enabled=False)
        data = sticker_image("blue")
        missing, _ = self.library.add("待识图.png", data, inspect_image(data), pack_id=pack.id)
        self.library.path(missing).unlink()
        snapshot = self.library.export_snapshot(pending=[missing.id], errors={missing.id: "源图读取失败"})
        data = csv_bytes(snapshot)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        rows = {row["图片ID"]: row for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig")))}
        self.assertEqual(len(rows), 2)
        edited = rows[self.item.id]
        self.assertEqual(edited["文件名"], '猫猫,"表情".png')
        self.assertEqual(edited["画面与语气"], "人工描述\n第二行")
        self.assertEqual(edited["自动识图描述"], "自动识图：开心猫猫")
        self.assertEqual(edited["自动识图分类"], "开心、调侃")
        self.assertEqual(edited["识图模型"], "vision")
        self.assertTrue(edited["识图时间（UTC）"].endswith("+00:00"))
        self.assertEqual(edited["可使用"], "否")
        self.assertEqual(rows[missing.id]["风格包"], pack.name)
        self.assertEqual(rows[missing.id]["源文件存在"], "否")
        self.assertEqual(rows[missing.id]["识图状态"], "识图中")
        self.assertEqual(rows[missing.id]["最近识图错误"], "源图读取失败")
        self.assertEqual(rows[missing.id]["自动识图描述"], "")
        self.assertEqual(
            StickerLibrary(self.library.root).items[self.item.id].vision_result["tags"], ["猫猫", "笑场"]
        )

    def test_csv_formula_prefixes_are_text_and_empty_export_still_has_headers(self):
        for text in ("=1+1", "+cmd", "-1", "@example", "  =SUM(A1)", "\tvalue", "\rvalue", "\nvalue"):
            self.assertEqual(csv_cell(text), "'" + text)
        self.assertEqual(csv_cell("正常文本，含逗号"), "正常文本，含逗号")
        self.assertEqual(len(list(csv.reader(io.StringIO(csv_bytes({"rows": []}).decode("utf-8-sig"))))), 1)

    def test_documents_export_contains_generated_files_and_complete_frozen_metadata(self):
        data = sticker_image("blue")
        unrecognized, _ = self.library.add("待识图.png", data, inspect_image(data))
        self.library.update(self.item, name="=HYPERLINK(test)", enabled=False)
        snapshot = self.library.export_snapshot(documents=True)
        generated = {name: (self.library.root / name).read_bytes() for name in snapshot["documents"]}
        self.library.delete(self.library.items[self.item.id])
        path = write_export(self.library.root, snapshot, documents=True)
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("stickers.csv", archive.namelist())
            self.assertIn("导出说明.md", archive.namelist())
            self.assertIn("catalog/开心.md", archive.namelist())
            for name, content in generated.items():
                self.assertEqual(archive.read(name), content)
            metadata = json.loads(archive.read("index.json"))
            self.assertEqual({item["id"] for item in metadata["items"]}, {self.item.id, unrecognized.id})
            self.assertEqual(metadata["items"][0]["name"], "=HYPERLINK(test)")
            rows = list(csv.DictReader(io.StringIO(archive.read("stickers.csv").decode("utf-8-sig"))))
            self.assertEqual(rows[0]["文件名"], "'=HYPERLINK(test)")

    async def test_csv_and_document_downloads_use_unique_files_and_clean_up(self):
        for handler, filename in (
            (self.plugin.web_export_csv, "stickers.csv"),
            (self.plugin.web_export_documents, "sticker-library-documents.zip"),
        ):
            with self.subTest(filename=filename):
                first = await handler()
                second = await handler()
                self.assertEqual(first.filename, filename)
                self.assertNotEqual(first.path, second.path)
                self.assertTrue(Path(first.path).is_file())
                await first.background()
                self.assertFalse(Path(first.path).exists())
                self.assertTrue(Path(second.path).is_file())
                await second.background()

    async def test_failed_recognition_keeps_previous_successful_result(self):
        self.context.llm_generate.return_value = SimpleNamespace(completion_text='{"description":"原自动识图"}')
        self.plugin.queue_index([self.item])
        await self.plugin.worker
        item = self.library.items[self.item.id]
        self.context.llm_generate.return_value = SimpleNamespace(completion_text="invalid")
        self.plugin.queue_index([item])
        await self.plugin.worker
        self.assertEqual(self.library.items[item.id].vision_result["description"], "原自动识图")
        snapshot = self.library.export_snapshot(errors=self.plugin.index_errors)
        rows = list(csv.DictReader(io.StringIO(csv_bytes(snapshot).decode("utf-8-sig"))))
        self.assertEqual(rows[0]["识图状态"], "识图失败")
        self.assertEqual(rows[0]["自动识图描述"], "原自动识图")

    async def test_selection_prompt_uses_intent_without_resending_persona_history_or_peers(self):
        self.expression_model(["1", "1"])
        event = Event()
        await self.plugin.capture(event, ProviderRequest())
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        intent = json.loads(self.context.llm_generate.await_args_list[0].kwargs["prompt"])
        selection = json.loads(self.context.llm_generate.await_args_list[1].kwargs["prompt"])
        self.assertIn("人格", intent)
        for key in ("人格", "最近对话", "当前情绪", "长期表达习惯", "自己已发的表情"):
            self.assertNotIn(key, selection)
        self.assertLess(list(selection).index("分类文档目录"), list(selection).index("我已决定的表达意图"))
        self.assertEqual(selection["文档匹配条目"][0]["id"], "1")
        self.assertNotIn(self.item.id, json.dumps(selection))
        self.assertEqual(event.send.await_count, 2)

    async def test_identical_search_requests_stop_without_repeating_model_calls(self):
        meaning = "爆笑"
        self.context.llm_generate.side_effect = [
            SimpleNamespace(completion_text=json.dumps({"want": True, "meaning": meaning})),
            SimpleNamespace(completion_text=json.dumps({"search": {"query": meaning}})),
        ]
        event = Event()
        await self.plugin.capture(event, ProviderRequest())
        await self.plugin.after_reply(event)
        await asyncio.gather(*list(self.plugin.tasks))
        self.assertEqual(self.context.llm_generate.await_count, 2)
        event.send.assert_not_called()

    async def test_manual_reindex_needs_no_provider_and_preserves_reviewed_metadata(self):
        self.library.update(self.item, use_when="人工用途", categories=("调侃",))
        before = self.library.index.read_bytes()
        self.context.get_all_providers = lambda: []
        first = await self.plugin.web_rebuild_index()
        worker = self.plugin.rebuild_worker
        second = await self.plugin.web_rebuild_index()
        self.assertEqual(json.loads(first.body), {"queued": True, "running": True})
        self.assertEqual(json.loads(second.body), {"queued": False, "running": True})
        self.assertIs(self.plugin.rebuild_worker, worker)
        await worker
        self.assertEqual(self.library.index.read_bytes(), before)
        self.context.llm_generate.assert_not_called()
        index = json.loads((self.library.root / "retrieval-index.json").read_text("utf-8"))
        self.assertEqual(index["items"], 1)
        self.assertEqual(index["profiles"][0]["categories"], ["调侃"])
        self.assertIn("retrieval-index.json", self.library.export_snapshot(documents=True)["documents"])

    async def test_reindex_discards_stale_snapshot_if_manual_edit_occurs_while_building(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def delayed(function, *args, **kwargs):
            nonlocal calls
            result = function(*args, **kwargs)
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return result

        with patch("astrbot_plugin_active_sticker.main.asyncio.to_thread", side_effect=delayed):
            await self.plugin.web_rebuild_index()
            await asyncio.wait_for(started.wait(), 3)
            self.library.update(self.item, description="人工修改后的新含义")
            release.set()
            await self.plugin.rebuild_worker
        self.assertEqual(calls, 2)
        self.assertEqual(self.library.search_index.profiles[self.item.id]["description"], "人工修改后的新含义")

    async def test_reindex_failure_is_reported_and_existing_index_remains_usable(self):
        existing = self.library.search_index
        with patch.object(self.library, "build_index", side_effect=OSError("test")):
            await self.plugin.web_rebuild_index()
            await self.plugin.rebuild_worker
        self.assertIn("失败", self.plugin.rebuild_error)
        self.assertIs(self.library.search_index, existing)
        self.assertTrue(existing.search("爆笑")[0])
        await self.plugin.web_rebuild_index()
        await self.plugin.rebuild_worker
        self.assertFalse(self.plugin.rebuild_error)

    async def test_structured_vision_result_becomes_compact_profile_and_csv_fields(self):
        self.context.llm_generate.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "description": "猫猫假装发怒，其实是在逗朋友",
                    "use_when": "熟人接梗",
                    "tags": ["调侃"],
                    "categories": ["调侃"],
                    "retrieval": {
                        "meaning": "撒娇假装生气",
                        "tone": "开玩笑",
                        "text": "哼！",
                        "avoid": "不要用于严肃争执",
                    },
                }
            )
        )
        self.plugin.queue_index([self.item])
        await self.plugin.worker
        profile = self.library.search_index.profiles[self.item.id]
        self.assertEqual(profile["description"], "撒娇假装生气")
        self.assertEqual(profile["avoid"], "不要用于严肃争执")
        reloaded = StickerLibrary(self.library.root)
        self.assertEqual(reloaded.search_index.profiles[self.item.id], profile)
        rows = list(csv.DictReader(io.StringIO(csv_bytes(reloaded.export_snapshot()).decode("utf-8-sig"))))
        self.assertEqual(rows[0]["图中文字"], "哼！")

    def test_new_import_time_is_persisted_and_duplicate_edit_or_move_does_not_change_it(self):
        item = self.library.items[self.item.id]
        self.assertTrue(item.imported_at.endswith("+00:00"))
        self.assertFalse(item.import_time_estimated)
        pack = self.library.save_pack("新风格")
        self.library.organize([item.id], pack_id=pack.id)
        self.library.update(self.library.items[item.id], description="人工修改")
        duplicate, added = self.library.add("重复.png", sticker_image(), inspect_image(sticker_image()))
        self.assertFalse(added)
        self.assertEqual(duplicate.imported_at, item.imported_at)
        reloaded = StickerLibrary(self.library.root)
        self.assertEqual(reloaded.items[item.id].imported_at, item.imported_at)
        rows = list(csv.DictReader(io.StringIO(csv_bytes(reloaded.export_snapshot()).decode("utf-8-sig"))))
        self.assertEqual(rows[0]["导入时间（UTC）"], item.imported_at)
        self.assertEqual(rows[0]["导入时间来源"], "导入记录")

    def test_legacy_import_time_is_estimated_once_from_file_and_missing_images_remain_unknown(self):
        from dataclasses import asdict
        from datetime import datetime, timezone

        row = asdict(self.item)
        row.pop("imported_at")
        row.pop("import_time_estimated")
        stamp = 1700000000
        os.utime(self.library.path(self.item), (stamp, stamp))
        missing = {**row, "id": "0000000000000000"}
        self.library.index.write_text(json.dumps([row, missing]), "utf-8")
        reloaded = StickerLibrary(self.library.root)
        item = reloaded.items[self.item.id]
        self.assertEqual(
            item.imported_at, datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
        )
        self.assertTrue(item.import_time_estimated)
        self.assertEqual(reloaded.items[missing["id"]].imported_at, "")
        self.assertFalse(reloaded.items[missing["id"]].import_time_estimated)
        self.assertEqual(item.description, self.item.description)
        self.assertEqual(item.revision, self.item.revision)
        os.utime(reloaded.path(item), (stamp + 10000, stamp + 10000))
        again = StickerLibrary(self.library.root)
        self.assertEqual(again.items[item.id].imported_at, item.imported_at)
        rows = list(csv.DictReader(io.StringIO(csv_bytes(again.export_snapshot()).decode("utf-8-sig"))))
        self.assertEqual(rows[0]["导入时间来源"], "文件时间估算")
        self.assertEqual(rows[1]["导入时间来源"], "未知")

    async def test_moving_unrecognized_image_requeues_without_losing_manual_pack(self):
        pack = self.library.save_pack("猫猫")
        item = self.library.update(self.item, description="")
        started = asyncio.Event()
        release = asyncio.Event()

        async def respond(**_):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text='{"description":"识图描述"}')

        self.context.llm_generate.side_effect = respond
        self.plugin.queue_index([item])
        await asyncio.wait_for(started.wait(), 3)
        self.library.organize([item.id], pack_id=pack.id)
        release.set()
        await self.plugin.worker
        self.assertEqual(self.library.items[item.id].description, "识图描述")
        self.assertEqual(self.library.items[item.id].pack_id, pack.id)

    async def test_management_endpoints_reject_bad_batches_and_keep_unknown_fields_safe(self):
        request = SimpleNamespace(json=AsyncMock(return_value={"name": "猫猫"}))
        with patch("astrbot_plugin_active_sticker.main.request", request):
            response = await self.plugin.web_save_pack()
            pack = json.loads(response.body)["pack"]
            request.json.return_value = {"ids": [self.item.id], "pack_id": pack["id"]}
            self.assertEqual((await self.plugin.web_organize()).status_code, 200)
            self.assertEqual(self.library.items[self.item.id].pack_id, pack["id"])
            for invalid in (
                {"ids": [self.item.id], "enabled": "false"},
                {"ids": [self.item.id], "pack_id": []},
                {"ids": []},
                {"ids": [self.item.id, "missing"], "enabled": False},
            ):
                request.json.return_value = invalid
                self.assertEqual((await self.plugin.web_organize()).status_code, 400)
            self.assertTrue(self.library.items[self.item.id].enabled)
            request.json.return_value = {"id": pack["id"]}
            self.assertEqual((await self.plugin.web_delete_pack()).status_code, 200)
            self.assertTrue(self.library.path(self.item).is_file())
