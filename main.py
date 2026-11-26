"""AstrBot hooks for a post-reply expression, and the sticker library's WebUI."""

import asyncio
import json
import tempfile
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import PluginUploadFile, error_response, file_response, json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import ResultContentType
from starlette.background import BackgroundTask

from .exports import write_export
from .expression import (
    CHAT_EXPRESSION_PROMPT,
    EXPRESSION_PROMPT,
    INTENT_PROMPT,
    VISION_PROMPT,
    fields,
    object_response,
    selected_ids,
)
from .library import DEFAULT_PACK, MAX_BYTES, Sticker, StickerLibrary, inspect_image, preview_data, vision_image
from .retrieval import CATEGORIES, category_names, inferred_categories, retrieval_fields
from .styles import PackLease, choose_pack

NAME = "astrbot_plugin_active_sticker"
TURN = NAME + ".turn"


@dataclass(frozen=True)
class SentSticker:
    turn: int
    sent_at: float
    item: Sticker
    speaker: str
    reply: str
    meaning: str


@dataclass
class Session:
    turns: int = 0
    history: deque[tuple[str, str, SentSticker]] = field(default_factory=lambda: deque(maxlen=12))
    packs: OrderedDict[str, PackLease] = field(default_factory=OrderedDict)

    def sent_in(self, persona_id, conversation_id):
        return [
            sent
            for saved_persona, saved_conversation, sent in self.history
            if saved_persona == persona_id and saved_conversation == conversation_id
        ]


@dataclass
class Turn:
    session: Session
    number: int
    origin: str
    user: str
    persona: str
    persona_id: str
    conversation_id: str
    recent: list
    kind: str
    speaker: str
    group_chat: bool
    reply_images: bool = False
    reply: str = ""
    scheduled: bool = False
    pack_id: str = ""


def message_text(value):
    if isinstance(value, list):
        return "\n".join(
            part.get("text", "")
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("_no_save")
        )
    return value if isinstance(value, str) else ""


def recent_dialogue(request):
    recent = []
    for message in (request.contexts or [])[-18:]:
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}:
            content = message_text(message.get("content")).strip()
            if content:
                recent.append({"role": message["role"], "text": content[-900:]})
    return recent[-6:]


class ActiveSticker(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.library = StickerLibrary(StarTools.get_data_dir(NAME))
        self.sessions = OrderedDict()
        self.sending = {}
        self.receipts_path = self.library.root / "sent_stickers.json"
        self.receipts_lock = asyncio.Lock()
        self.load_sent_stickers()
        self.pending = {}
        self.index_errors = {}
        self.worker = None
        self.rebuild_worker = None
        self.rebuild_error = ""
        self.tasks = set()
        self.closing = False
        for route, handler, method in (
            ("settings", self.web_settings, "GET"),
            ("settings/save", self.web_save_settings, "POST"),
            ("library", self.web_library, "GET"),
            ("preview", self.web_preview, "GET"),
            ("upload", self.web_upload, "POST"),
            ("edit", self.web_edit, "POST"),
            ("delete", self.web_delete, "POST"),
            ("index", self.web_index, "POST"),
            ("index/rebuild", self.web_rebuild_index, "POST"),
            ("catalog", self.web_catalog, "GET"),
            ("export/csv", self.web_export_csv, "GET"),
            ("export/documents", self.web_export_documents, "GET"),
            ("packs/save", self.web_save_pack, "POST"),
            ("packs/delete", self.web_delete_pack, "POST"),
            ("organize", self.web_organize, "POST"),
            ("packs/export", self.web_export_pack, "GET"),
        ):
            context.register_web_api(f"/{NAME}/{route}", handler, [method], "主动表情面板")

    async def initialize(self):
        self.index_unrecognized()

    @filter.on_astrbot_loaded()
    async def resume_index(self):
        self.index_unrecognized()

    async def terminate(self):
        self.closing = True
        tasks = list(self.tasks)
        if self.worker:
            tasks.append(self.worker)
        if self.rebuild_worker:
            tasks.append(self.rebuild_worker)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def load_sent_stickers(self):
        if not self.receipts_path.exists():
            return
        try:
            saved = json.loads(self.receipts_path.read_text(encoding="utf-8"))
            for origin, data in saved.items():
                session = Session(turns=data["turns"])
                for key, lease in list(data.get("packs", {}).items())[-32:]:
                    session.packs[key] = PackLease(**lease)
                for persona, conversation, record in data["history"]:
                    record["item"] = Sticker(**record["item"])
                    session.history.append((persona, conversation, SentSticker(**record)))
                self.sessions[origin] = session
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.sessions.clear()
            logger.warning("主动表情暂时无法读取已发送记录")

    async def save_sent_stickers(self, *, raise_on_error=False):
        async with self.receipts_lock:
            saved = {
                origin: {
                    "turns": session.turns,
                    "history": [
                        (persona, conversation, asdict(sent)) for persona, conversation, sent in session.history
                    ],
                    "packs": {key: asdict(lease) for key, lease in session.packs.items()},
                }
                for origin, session in self.sessions.items()
                if session.history or session.packs
            }
            try:
                await asyncio.to_thread(
                    self.library.write,
                    self.receipts_path,
                    json.dumps(saved, ensure_ascii=False).encode("utf-8"),
                )
            except OSError:
                if raise_on_error:
                    raise
                logger.warning("主动表情暂时无法保存已发送记录")

    async def companion_clear(self, origin, *, persona):
        if origin in self.sending.values():
            return "busy"
        self.sessions.pop(origin, None)
        await self.save_sent_stickers(raise_on_error=True)
        return "cleared"

    def enabled_in(self, origin, name=NAME):
        allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
        star = self.context.get_registered_star(name)
        return allowed is None or any(
            value and value in allowed
            for value in (
                "*",
                name,
                getattr(star, "plugin_id", None),
            )
        )

    def linked(self, role, origin):
        if not self.config.get("interop_enabled", True) or not self.config.get("link_" + role, True):
            return {}
        name = "astrbot_plugin_" + role
        try:
            star = self.context.get_registered_star(name)
            if not star or not star.activated or not self.enabled_in(origin, name):
                return {}
            peer = star.star_cls
            config = getattr(peer, "config", {})
            if getattr(peer, "closing", False) or not all(
                config.get(key, True)
                for key in (
                    "enabled",
                    "interop_enabled",
                    "link_active_sticker",
                )
            ):
                return {}
            value = peer.companion_snapshot(origin)
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001 - optional peers are isolated
            return {}

    def companion_sticker_catalog(self, origin):
        """Read-only catalog for cognition and other optional consumers."""
        if (
            not origin
            or self.closing
            or not self.config.get("enabled", True)
            or not self.config.get("interop_enabled", True)
            or not self.config.get("link_self_cognition", True)
            or not self.enabled_in(origin)
        ):
            return {}
        items = self.library.available()
        return (
            {
                "version": 1,
                "items": [item.summary() for item in items],
                "packs": [asdict(pack) for pack in self.library.packs.values() if pack.enabled],
            }
            if items
            else {}
        )

    def get_persona(self, event, request):
        persona_id = event.get_extra("_trace_persona_id")
        if persona_id == "[%None]":
            return ""
        if persona_id == "_chatui_default_":
            from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT

            return (
                CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
                if event.get_extra("enable_default_system_prompt") is not False
                else ""
            )
        if persona_id:
            persona = self.context.persona_manager.get_persona_v3_by_id(persona_id)
            if persona:
                return str(persona.get("prompt") or "")
        return request.system_prompt or ""

    @filter.on_llm_request(priority=100)
    async def capture(self, event: AstrMessageEvent, request):
        if self.closing or not self.config.get("enabled", True) or event.get_extra(TURN):
            return
        envelope = event.get_extra("w1_event")
        message_id = str(event.message_obj.message_id or "")
        if (
            event.get_extra("codex_bridge")
            or message_id.startswith(("codex-job:", "codex-report:", "proactive:codex_bridge:"))
            or isinstance(envelope, dict)
            and envelope.get("type") == "task_event"
        ):
            return
        proactive = message_id.startswith("proactive:") or any(
            event.get_extra(role) for role in ("play_together", "self_amusement")
        )
        origin = event.unified_msg_origin
        if not self.enabled_in(origin):
            return
        persona_id = event.get_extra("_trace_persona_id") or ""
        conversation_id = getattr(request.conversation, "cid", "") or ""
        # Keep the character separate from the request-scoped capability instructions.
        persona = self.get_persona(event, request)[-12000:]
        session = self.sessions.setdefault(origin, Session())
        available_packs = {item.pack_id for item in self.library.available()}
        pack_id = choose_pack(
            session.packs,
            json.dumps([persona_id, conversation_id]),
            [ident for ident in self.library.packs if ident in available_packs],
            time.time(),
            max(1, int(self.config.get("pack_idle_minutes", 360))) * 60,
            max(0, min(100, int(self.config.get("pack_switch_percent", 50)))) / 100,
        )
        self.include_sent_stickers(request, session.sent_in(persona_id, conversation_id), pack_id)
        self.sessions.move_to_end(origin)
        if len(self.sessions) > 512:
            self.sessions.popitem(last=False)
        session.turns += 1
        event.set_extra(
            TURN,
            Turn(
                session=session,
                number=session.turns,
                origin=origin,
                user="" if proactive else (event.get_message_outline() or "")[-1800:],
                persona=persona,
                persona_id=persona_id,
                conversation_id=conversation_id,
                recent=recent_dialogue(request),
                kind="主动发言" if proactive else "聊天回复",
                speaker="" if proactive else event.get_sender_name()[:100],
                group_chat=bool(event.get_group_id()),
                pack_id=pack_id,
            ),
        )
        await self.save_sent_stickers()

    def include_sent_stickers(self, request, sent_stickers, pack_id=None):
        count = len(self.library.available(pack_id)) if pack_id != "" else 0
        notes = []
        if count:
            if CHAT_EXPRESSION_PROMPT not in (request.system_prompt or ""):
                request.system_prompt = "\n\n".join(
                    part for part in (request.system_prompt, CHAT_EXPRESSION_PROMPT) if part
                )
            notes.append(
                f"主动表情插件的本地表情库当前有 {count} 张可用且已描述的图片。需要表情包时优先使用这个插件。"
                "正常回复后会自动进入表情意图判断与检索选图，无需在主对话调用工具。"
                "微信原生收藏的查询结果只描述原生收藏，不影响这里的可用图片。"
            )
            if pack_id:
                notes.append(
                    "当前短期对话只使用风格包："
                    + self.library.packs[pack_id].name
                    + "。没有贴切图片可以不发，风格包不决定你的性格或表达意愿。"
                )
        replies = {}
        for sent in sent_stickers:
            entry = replies.setdefault(
                sent.turn,
                {
                    "回复摘录": sent.reply,
                    "我发送的表情": [],
                },
            )
            if sent.speaker:
                entry["回应对象"] = sent.speaker
            if sent.meaning:
                entry["我当时想表达"] = sent.meaning
            entry["我发送的表情"].append(sent.item.description[:200])

        if replies:
            details = json.dumps(list(replies.values()), ensure_ascii=False)
            notes.append(
                "本会话中你在这些回复后亲自选择并发出了表情。发送工具按你的选择附图。"
                "这是已经发生的对话记录，供你理解用户对表情的回应。\n" + details
            )
        if notes:
            request.extra_user_content_parts.append(TextPart(text="\n".join(notes)).mark_as_temp())

    @filter.on_llm_response(priority=-200)
    async def remember_reply(self, event: AstrMessageEvent, response):
        turn = event.get_extra(TURN)
        if (
            turn
            and getattr(response, "role", "") == "assistant"
            and not getattr(response, "tools_call_args", None)
        ):
            turn.reply = (getattr(response, "completion_text", None) or "").strip()[-3500:]

    @filter.on_agent_done(priority=-200)
    async def agent_done(self, event: AstrMessageEvent, run_context, response):
        await self.remember_reply(event, response)

    @filter.after_message_sent(priority=-200)
    async def after_reply(self, event: AstrMessageEvent):
        result = event.get_result()
        if result and result.result_content_type == ResultContentType.LLM_RESULT and event._has_send_oper:
            await self.schedule_expression(event, result)

    @filter.on_decorating_result(priority=-200)
    async def stream_finished(self, event: AstrMessageEvent):
        # On 4.29 native streams have already been delivered at STREAMING_FINISH.
        result = event.get_result()
        if result and result.result_content_type == ResultContentType.STREAMING_FINISH and event._has_send_oper:
            await self.schedule_expression(event, result)

    def current(self, turn):
        return (
            not self.closing
            and self.config.get("enabled", True)
            and self.sessions.get(turn.origin) is turn.session
            and turn.session.turns == turn.number
            and self.enabled_in(turn.origin)
        )

    async def schedule_expression(self, event, result):
        turn = event.get_extra(TURN)
        if not turn or turn.scheduled or not self.current(turn):
            return
        delivered_text = (result.get_plain_text() or "").strip()
        turn.reply = (delivered_text or turn.reply)[-3500:]
        turn.reply_images = any(isinstance(part, Image) for part in result.chain)
        if not turn.reply or not turn.pack_id or not self.library.available(turn.pack_id):
            return
        turn.scheduled = True
        task = asyncio.create_task(self.express(event, turn))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # These request-scoped adapters close delivery when their pipeline exits.
        if event.get_platform_name() in {"webchat", "wecom_ai_bot"}:
            await asyncio.gather(task, return_exceptions=True)

    async def express(self, event, turn):
        try:
            if not self.current(turn):
                return
            mood = self.linked("short_term_mood", turn.origin)
            cognition = self.linked("self_cognition", turn.origin)
            sent_here = turn.session.sent_in(turn.persona_id, turn.conversation_id)
            recent_stickers = [
                {
                    "id": sent.item.id,
                    "image": sent.item.description,
                    "after_speaker": sent.speaker,
                    "turns_ago": turn.number - sent.turn,
                    "seconds_ago": max(0, int(time.time() - sent.sent_at)),
                }
                for sent in reversed(sent_here)
            ]
            scene = {
                "人格": turn.persona,
                "额外表情习惯": self.config.get("expression_style", ""),
                "场景": turn.kind,
                "聊天场合": "群聊" if turn.group_chat else "私聊",
                "当前说话人": turn.speaker,
                "最近对话": turn.recent,
                "对方这句话": turn.user,
                "已经发出的回复": turn.reply,
                "回复已经附图": turn.reply_images,
                "自己已发的表情": recent_stickers,
                "当前情绪": mood,
                "长期表达习惯": {key: cognition[key] for key in ("character", "people") if key in cognition},
            }
            provider = self.config.get(
                "expression_provider_id"
            ) or await self.context.get_current_chat_provider_id(turn.origin)
            reply = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider,
                    system_prompt=INTENT_PROMPT,
                    prompt=json.dumps(scene, ensure_ascii=False, separators=(",", ":")),
                ),
                timeout=15,
            )
            intent = object_response(reply.completion_text)
            meaning = intent.get("meaning")
            meaning = " ".join(meaning.split())[:160] if isinstance(meaning, str) else ""
            if intent.get("want") is not True or not meaning or not self.current(turn):
                return
            max_images = max(1, min(20, int(self.config.get("burst_limit", 6))))
            ids = ()
            query = meaning
            categories = ()
            seen = set()
            searches = set()
            for _ in range(3):
                if not self.current(turn):
                    return
                search_index = self.library.pack_indexes.get(turn.pack_id)
                if search_index is None or not search_index.items:
                    return
                search_key = (query, tuple(categories))
                if search_key in searches:
                    break
                searches.add(search_key)
                candidates, entries = search_index.search(query, categories, exclude=seen, compact=True)
                seen.update(item.id for item in candidates)
                selection_scene = {
                    "我已决定的表达意图": meaning,
                    "已经发出的回复": turn.reply[-500:],
                    "当前说话人": turn.speaker,
                    "本组可用图片数上限": max_images,
                    "当前风格包": self.library.packs[turn.pack_id].name,
                    "选图约束": "只使用当前风格包。没有贴切素材就不发，不跨包补图。",
                    "分类文档目录": search_index.directory(),
                    "本次检索": {"query": query, "categories": categories},
                    "文档匹配条目": entries,
                }
                selection = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider,
                        system_prompt=EXPRESSION_PROMPT,
                        prompt=json.dumps(selection_scene, ensure_ascii=False, separators=(",", ":")),
                    ),
                    timeout=15,
                )
                choice = object_response(selection.completion_text)
                if "send" in choice:
                    ids = selected_ids(choice, candidates, max_images)
                    break
                search = choice.get("search")
                if not isinstance(search, dict) or not isinstance(search.get("query"), str):
                    break
                query = search["query"][:300]
                categories = category_names(search.get("categories", []))
            for index, ident in enumerate(ids):
                if index:
                    await asyncio.sleep(0.45)
                if not self.current(turn):
                    break
                item = self.library.items.get(ident)
                pack = self.library.packs.get(turn.pack_id)
                if (
                    not pack
                    or not pack.enabled
                    or not item
                    or item.pack_id != turn.pack_id
                    or not item.enabled
                    or not item.description
                    or not self.library.path(item).is_file()
                ):
                    continue
                self.sending[id(turn)] = turn.origin
                try:
                    delivered = await asyncio.wait_for(
                        event.send(
                            MessageChain([Image.fromFileSystem(str(self.library.path(item)))]),
                        ),
                        timeout=8,
                    )
                finally:
                    self.sending.pop(id(turn), None)
                if delivered is False or event.is_stopped():
                    break
                sent = SentSticker(turn.number, time.time(), item, turn.speaker, turn.reply[:240], meaning)
                turn.session.history.append((turn.persona_id, turn.conversation_id, sent))
                await self.save_sent_stickers()
        except Exception as error:  # noqa: BLE001 - isolate provider and adapter failures
            logger.warning("主动表情发送跳过 (%s)", type(error).__name__)

    def vision_provider(self):
        selected = self.config.get("vision_provider_id")
        for provider in self.context.get_all_providers():
            if selected:
                if provider.meta().id == selected:
                    return selected
            elif "image" in (provider.provider_config.get("modalities") or []):
                return provider.meta().id
        return ""

    def index_unrecognized(self):
        return self.queue_index(
            item for item in self.library.items.values() if item.enabled and not item.description
        )

    def queue_index(self, items):
        if self.closing or not self.vision_provider():
            return 0
        queued = 0
        for item in items:
            if item.id not in self.pending and self.library.path(item).is_file():
                self.index_errors.pop(item.id, None)
                self.pending[item.id] = item
                queued += 1
        if self.pending and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self.index_images())
        return queued

    async def index_images(self):
        while self.pending:
            ident, item = next(iter(self.pending.items()))
            try:
                if self.library.items.get(ident) is not item:
                    continue
                provider = self.vision_provider()
                if not provider:
                    self.index_errors[ident] = "请在插件配置选择支持图片输入的识图模型，然后重试"
                    continue
                data = await asyncio.to_thread(vision_image, self.library.path(item))
                with tempfile.TemporaryDirectory(prefix="active-sticker-") as folder:
                    preview = Path(folder) / "frames.png"
                    await asyncio.to_thread(preview.write_bytes, data)
                    result = await asyncio.wait_for(
                        self.context.llm_generate(
                            chat_provider_id=provider,
                            prompt=VISION_PROMPT,
                            image_urls=[str(preview)],
                        ),
                        timeout=60,
                    )
                values = fields(object_response(result.completion_text))
                if not values["description"]:
                    if self.library.items.get(ident) is item:
                        self.index_errors[ident] = "模型没有返回有效描述，可重试或手动填写"
                else:
                    vision_result = {
                        **values,
                        "retrieval": retrieval_fields(object_response(result.completion_text)),
                        "provider": provider,
                        "recognized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    latest = self.library.items.get(ident)
                    if latest is not None:
                        # Organizing a picture preserves the recognition; manual wording still wins.
                        keys = ("description", "use_when", "tags", "categories")
                        unchanged = all(getattr(latest, key) == getattr(item, key) for key in keys)
                        self.library.update(latest, **(values if unchanged else {}), vision_result=vision_result)
            except Exception as error:  # noqa: BLE001 - isolate provider and adapter failures
                if self.library.items.get(ident) is item:
                    self.index_errors[ident] = f"识图失败（{type(error).__name__}），可重试或手动填写"
            finally:
                self.pending.pop(ident, None)
                latest = self.library.items.get(ident)
                if latest is not None and latest is not item and latest.enabled and not latest.description:
                    self.queue_index([latest])

    async def web_settings(self):
        providers = []
        for provider in self.context.get_all_providers():
            meta = provider.meta()
            providers.append(
                {
                    "id": meta.id,
                    "model": meta.model,
                    "vision": "image" in (provider.provider_config.get("modalities") or []),
                }
            )
        values = {key: self.config.get(key, spec["default"]) for key, spec in self.config.schema.items()}
        return json_response({"values": values, "providers": providers})

    async def web_save_settings(self):
        values = await request.json(default=None)
        if not isinstance(values, dict) or not self.config.schema.keys() >= values.keys():
            return error_response("配置字段无效", status_code=400)
        for key, value in values.items():
            spec = self.config.schema[key]
            kind = spec["type"]
            valid = (
                (kind == "bool" and type(value) is bool)
                or (kind in {"string", "text"} and isinstance(value, str))
                or (kind == "int" and type(value) is int and spec["min"] <= value <= spec["max"])
            )
            if not valid:
                return error_response(f"{spec['description']}填写有误", status_code=400)

        if values:
            previous = dict(self.config)
            try:
                self.config.save_config(values)
            except OSError:
                self.config.clear()
                self.config.update(previous)
                logger.exception("主动表情保存配置失败")
                return error_response("配置保存失败，请重试", status_code=500)
            if "vision_provider_id" in values:
                self.index_unrecognized()
        return json_response({"saved": True})

    async def web_library(self):
        return json_response(
            {
                "items": [
                    {
                        **asdict(item),
                        "categories": inferred_categories(item),
                        "pending": item.id in self.pending,
                        "error": self.index_errors.get(item.id, ""),
                        "missing": not self.library.path(item).is_file(),
                    }
                    for item in self.library.items.values()
                ],
                "vision_configured": bool(self.vision_provider()),
                "categories": list(CATEGORIES),
                "search_index": {
                    **self.library.index_stats,
                    "rebuilding": bool(self.rebuild_worker and not self.rebuild_worker.done()),
                    "error": self.rebuild_error,
                },
                "packs": [
                    {
                        **asdict(pack),
                        "count": sum(item.pack_id == pack.id for item in self.library.items.values()),
                        "ready": len(self.library.pack_indexes[pack.id].items),
                    }
                    for pack in self.library.packs.values()
                ],
            }
        )

    async def web_preview(self):
        item = self.library.items.get(request.query.get("id", ""))
        if not item or not self.library.path(item).is_file():
            return error_response("找不到图片", status_code=404)
        try:
            preview = await asyncio.to_thread(preview_data, self.library.path(item))
        except (OSError, ValueError):
            return error_response("无法预览这张图片", status_code=422)
        return json_response({"src": preview})

    async def web_upload(self):
        upload = (await request.files()).get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("请选择图片", status_code=400)
        try:
            data = await upload.read(MAX_BYTES + 1)
            inspected = await asyncio.to_thread(inspect_image, data)
        except ValueError as error:
            return error_response(str(error), status_code=400)
        item, added = self.library.add(upload.filename or "", data, inspected)
        if item.enabled and not item.description:
            self.queue_index([item])
        return json_response({"id": item.id, "added": added})

    async def web_edit(self):
        data = await request.json(default={})
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return error_response("缺少图片 ID", status_code=400)
        item = self.library.items.get(data["id"])
        if not item:
            return error_response("找不到图片", status_code=404)
        if not isinstance(data.get("enabled"), bool):
            return error_response("缺少启用状态", status_code=400)
        values = fields(data)
        if "categories" not in data:
            values["categories"] = item.categories
        pack_id = data.get("pack_id", item.pack_id)
        if not isinstance(pack_id, str) or pack_id not in self.library.packs:
            return error_response("找不到风格包", status_code=400)
        values["pack_id"] = pack_id
        if "name" in data:
            if not isinstance(data["name"], str) or not data["name"].strip():
                return error_response("请填写图片名称", status_code=400)
            values["name"] = " ".join(data["name"].split())[:160]
        self.library.update(item, **values, enabled=data["enabled"])
        self.index_errors.pop(item.id, None)
        return json_response({"saved": True})

    async def web_delete(self):
        data = await request.json(default={})
        item = (
            self.library.items.get(data["id"])
            if isinstance(data, dict) and isinstance(data.get("id"), str)
            else None
        )
        if not item:
            return error_response("找不到图片", status_code=404)
        self.library.delete(item)
        self.index_errors.pop(item.id, None)
        return json_response({"deleted": True})

    async def web_index(self):
        data = await request.json(default={})
        if not isinstance(data, dict):
            return error_response("无效的识图请求", status_code=400)
        if not self.vision_provider():
            return error_response("请先在设置中选一个可用的识图模型", status_code=400)
        if "id" not in data:
            return json_response({"queued": self.index_unrecognized()})
        ident = data["id"]
        item = self.library.items.get(ident) if isinstance(ident, str) else None
        if not item or not self.library.path(item).is_file():
            return error_response("找不到图片", status_code=404)
        return json_response({"queued": self.queue_index([item])})

    async def web_rebuild_index(self):
        if self.closing:
            return error_response("插件正在关闭", status_code=409)
        running = self.rebuild_worker is not None and not self.rebuild_worker.done()
        if not running:
            self.rebuild_error = ""
            self.rebuild_worker = asyncio.create_task(self.rebuild_index())
        return json_response({"queued": not running, "running": True})

    async def rebuild_index(self):
        try:
            for _ in range(3):
                items, packs = self.library.items, self.library.packs
                available = self.library.available()
                state = await asyncio.to_thread(self.library.build_index, available, tuple(packs))
                if self.closing:
                    return
                if self.library.items is items and self.library.packs is packs:
                    self.library.write_catalog(state)
                    return
            self.rebuild_error = "图库仍在变化，已保留最新自动索引；可在整理或识图结束后重试"
        except Exception as error:  # noqa: BLE001 - management failure must not affect chat
            self.rebuild_error = "索引整理失败，请重试"
            logger.warning("主动表情索引整理失败 (%s)", type(error).__name__)

    async def web_catalog(self):
        return file_response(self.library.root / "catalog.md", filename="catalog.md", content_type="text/markdown")

    async def export_library(self, *, documents=False):
        snapshot = self.library.export_snapshot(self.pending, self.index_errors, documents=documents)
        path = await asyncio.to_thread(write_export, self.library.root, snapshot, documents=documents)
        response = file_response(
            path,
            filename="sticker-library-documents.zip" if documents else "stickers.csv",
            content_type="application/zip" if documents else "text/csv; charset=utf-8",
        )
        response.background = BackgroundTask(path.unlink, missing_ok=True)
        return response

    async def web_export_csv(self):
        return await self.export_library()

    async def web_export_documents(self):
        return await self.export_library(documents=True)

    async def web_save_pack(self):
        data = await request.json(default={})
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("name"), str)
            or not isinstance(data.get("description", ""), str)
            or type(data.get("enabled", True)) is not bool
            or ("id" in data and not isinstance(data["id"], str))
        ):
            return error_response("风格包信息无效", status_code=400)
        try:
            pack = self.library.save_pack(
                data["name"], data.get("description", ""), data.get("enabled", True), data.get("id")
            )
        except ValueError as error:
            return error_response(str(error), status_code=400)
        return json_response({"pack": asdict(pack)})

    async def web_delete_pack(self):
        data = await request.json(default={})
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return error_response("缺少风格包 ID", status_code=400)
        try:
            self.library.delete_pack(data["id"])
        except ValueError as error:
            return error_response(str(error), status_code=400)
        self.index_unrecognized()
        return json_response({"deleted": True})

    async def web_organize(self):
        data = await request.json(default={})
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("ids"), list)
            or not all(isinstance(ident, str) for ident in data["ids"])
            or not ("pack_id" in data or "enabled" in data)
            or ("pack_id" in data and not isinstance(data["pack_id"], str))
            or ("enabled" in data and type(data["enabled"]) is not bool)
        ):
            return error_response("整理请求无效", status_code=400)
        try:
            self.library.organize(data["ids"], pack_id=data.get("pack_id"), enabled=data.get("enabled"))
        except ValueError as error:
            return error_response(str(error), status_code=400)
        self.index_unrecognized()
        return json_response({"saved": True})

    async def web_export_pack(self):
        ident = request.query.get("id", DEFAULT_PACK)
        if ident not in self.library.packs:
            return error_response("找不到风格包", status_code=404)
        snapshot = self.library.pack_snapshot(ident)
        path = await asyncio.to_thread(self.library.export_pack, ident, snapshot)
        response = file_response(path, filename=f"stickers-{ident}.zip", content_type="application/zip")
        response.background = BackgroundTask(path.unlink, missing_ok=True)
        return response
