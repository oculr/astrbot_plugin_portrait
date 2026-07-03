from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "core"
MESSAGE_PATH = CORE_DIR / "message.py"


def load_message_module():
    sys.modules.pop("core.message", None)

    class RecordingLogger:
        def __init__(self):
            self.debug_messages: list[str] = []
            self.info_messages: list[str] = []
            self.error_messages: list[str] = []

        def debug(self, message, *args, **kwargs):
            self.debug_messages.append(str(message))

        def info(self, message, *args, **kwargs):
            self.info_messages.append(str(message))

        def error(self, message, *args, **kwargs):
            self.error_messages.append(str(message))

    logger = RecordingLogger()

    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logger

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module
    sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
    sys.modules["astrbot.core.platform"] = types.ModuleType("astrbot.core.platform")
    sys.modules["astrbot.core.platform.sources"] = types.ModuleType(
        "astrbot.core.platform.sources"
    )
    sys.modules["astrbot.core.platform.sources.aiocqhttp"] = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp"
    )

    event_module = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )

    class AiocqhttpMessageEvent:
        pass

    event_module.AiocqhttpMessageEvent = AiocqhttpMessageEvent
    sys.modules[
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    ] = event_module

    core_package = types.ModuleType("core")
    core_package.__path__ = [str(CORE_DIR)]
    sys.modules["core"] = core_package

    config_module = types.ModuleType("core.config")

    class PluginConfig:
        pass

    config_module.PluginConfig = PluginConfig
    sys.modules["core.config"] = config_module

    spec = importlib.util.spec_from_file_location("core.message", MESSAGE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["core.message"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, logger


def make_message(
    message_id: int,
    timestamp: int,
    *,
    user_id: str = "10001",
    text: str | None = None,
):
    return {
        "message_id": message_id,
        "time": timestamp,
        "sender": {"user_id": user_id},
        "message": [{"type": "text", "data": {"text": text or f"msg-{message_id}"}}],
    }


class DummyConfig:
    class MessageConfig:
        def __init__(self, max_msg_count: int):
            self.cache_ttl = 60
            self.max_msg_count = max_msg_count
            self.per_query_count = 3

    def __init__(self, max_msg_count: int = 10):
        self.message = self.MessageConfig(max_msg_count)


class FakeAPI:
    def __init__(self, pages: dict[int, list[dict]]):
        self.pages = pages
        self.calls: list[int] = []

    async def call_action(self, action: str, **kwargs):
        assert action == "get_group_msg_history"
        message_seq = kwargs.get("message_seq", 0)
        self.calls.append(message_seq)
        return {"messages": self.pages.get(message_seq, [])}


class FakeBot:
    def __init__(self, pages: dict[int, list[dict]]):
        self.api = FakeAPI(pages)


class FakeEvent:
    def __init__(self, pages: dict[int, list[dict]]):
        self.bot = FakeBot(pages)

    def get_group_id(self) -> str:
        return "9527"


class MessageManagerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def get_log_field(message: str, field: str) -> str | None:
        prefix = f"{field}="
        for chunk in message.split():
            if chunk.startswith(prefix):
                return chunk[len(prefix) :]
        return None

    def assert_cache_logs_are_normalized(self, logger):
        for message in [*logger.debug_messages, *logger.info_messages]:
            self.assertTrue(message.startswith("[portrait.message_cache] "))
            self.assertIn("event=", message)
            self.assertIn("query_id=", message)

    async def test_get_user_texts_uses_oldest_anchor_and_skips_overlap(self):
        module, _logger = load_message_module()
        manager = module.MessageManager(DummyConfig())
        event = FakeEvent(
            {
                0: [
                    make_message(300, 300),
                    make_message(299, 299),
                    make_message(298, 298),
                ],
                298: [
                    make_message(298, 298),
                    make_message(297, 297),
                    make_message(296, 296),
                ],
                296: [],
                300: [
                    make_message(300, 300),
                    make_message(299, 299),
                    make_message(298, 298),
                ],
            }
        )

        result = await manager.get_user_texts(event, "10001", max_rounds=2)

        self.assertEqual(
            result.texts,
            ["msg-296", "msg-297", "msg-298", "msg-299", "msg-300"],
        )
        self.assertEqual(event.bot.api.calls[:2], [0, 298])

    async def test_returns_latest_subset_but_ordered_from_old_to_new(self):
        module, _logger = load_message_module()
        manager = module.MessageManager(DummyConfig(max_msg_count=3))
        event = FakeEvent(
            {
                0: [
                    make_message(305, 305),
                    make_message(304, 304),
                    make_message(303, 303),
                ],
                303: [
                    make_message(303, 303),
                    make_message(302, 302),
                    make_message(301, 301),
                ],
            }
        )

        result = await manager.get_user_texts(event, "10001", max_rounds=2)

        self.assertEqual(result.texts, ["msg-303", "msg-304", "msg-305"])

    async def test_reuses_group_cache_for_another_user_and_stops_on_overlap(self):
        module, logger = load_message_module()
        manager = module.MessageManager(DummyConfig(max_msg_count=2))

        first_event = FakeEvent(
            {
                0: [
                    make_message(300, 300, user_id="10001"),
                    make_message(299, 299, user_id="10002"),
                    make_message(298, 298, user_id="10001"),
                ],
            }
        )
        first_result = await manager.get_user_texts(first_event, "10001", max_rounds=3)
        debug_count = len(logger.debug_messages)
        info_count = len(logger.info_messages)

        second_event = FakeEvent(
            {
                0: [
                    make_message(302, 302, user_id="10002"),
                    make_message(301, 301, user_id="10001"),
                    make_message(300, 300, user_id="10001"),
                ],
                300: [
                    make_message(300, 300, user_id="10001"),
                    make_message(299, 299, user_id="10002"),
                    make_message(298, 298, user_id="10001"),
                ],
            }
        )
        second_result = await manager.get_user_texts(
            second_event, "10002", max_rounds=3
        )

        self.assertEqual(first_result.texts, ["msg-298", "msg-300"])
        self.assertEqual(second_result.texts, ["msg-299", "msg-302"])
        self.assertEqual(first_event.bot.api.calls, [0])
        self.assertEqual(second_event.bot.api.calls, [0])
        self.assert_cache_logs_are_normalized(logger)
        second_query_debug = logger.debug_messages[debug_count:]
        second_query_info = logger.info_messages[info_count:]
        query_ids = {
            self.get_log_field(message, "query_id")
            for message in [*second_query_debug, *second_query_info]
        }
        self.assertEqual(len(query_ids), 1)
        self.assertTrue(
            any(
                "event=page" in message
                and
                "phase=latest_sync" in message
                and "stop_reason=overlap_detected" in message
                for message in logger.debug_messages
            )
        )
        self.assertTrue(
            any(
                "event=query_summary" in message
                and
                "group=9527" in message
                and "target=10002" in message
                and "latest_sync_rounds=1" in message
                and "backfill_rounds=0" in message
                for message in logger.info_messages
            )
        )

    async def test_extends_older_history_after_incremental_overlap_when_target_still_short(
        self,
    ):
        module, logger = load_message_module()
        manager = module.MessageManager(DummyConfig(max_msg_count=2))

        first_event = FakeEvent(
            {
                0: [
                    make_message(300, 300, user_id="10001"),
                    make_message(299, 299, user_id="10002"),
                    make_message(298, 298, user_id="10001"),
                ],
            }
        )
        await manager.get_user_texts(first_event, "10001", max_rounds=3)
        debug_count = len(logger.debug_messages)
        info_count = len(logger.info_messages)

        second_event = FakeEvent(
            {
                0: [
                    make_message(301, 301, user_id="10001"),
                    make_message(300, 300, user_id="10001"),
                    make_message(299, 299, user_id="10002"),
                ],
                298: [
                    make_message(298, 298, user_id="10001"),
                    make_message(297, 297, user_id="10003"),
                    make_message(296, 296, user_id="10003"),
                ],
            }
        )
        result = await manager.get_user_texts(second_event, "10003", max_rounds=3)

        self.assertEqual(
            result.texts,
            ["msg-296", "msg-297"],
        )
        self.assertEqual(second_event.bot.api.calls, [0, 298])
        self.assert_cache_logs_are_normalized(logger)
        second_query_debug = logger.debug_messages[debug_count:]
        second_query_info = logger.info_messages[info_count:]
        query_ids = {
            self.get_log_field(message, "query_id")
            for message in [*second_query_debug, *second_query_info]
        }
        self.assertEqual(len(query_ids), 1)
        self.assertTrue(
            any(
                "event=page" in message
                and
                "phase=backfill" in message
                and "stop_reason=target_enough" in message
                for message in logger.debug_messages
            )
        )
        self.assertTrue(
            any(
                "event=query_summary" in message
                and
                "group=9527" in message
                and "target=10003" in message
                and "latest_sync_rounds=1" in message
                and "backfill_rounds=1" in message
                for message in logger.info_messages
            )
        )


if __name__ == "__main__":
    unittest.main()
