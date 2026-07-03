from __future__ import annotations

import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "core"
MODULE_PATH = CORE_DIR / "pillowmd_emoji.py"


def load_emoji_module(*, pilmoji_raises: bool = False):
    sys.modules.pop("core.pillowmd_emoji", None)

    class RecordingLogger:
        def __init__(self):
            self.info_messages: list[str] = []
            self.warning_messages: list[str] = []
            self.error_messages: list[str] = []

        def info(self, message, *args, **kwargs):
            self.info_messages.append(str(message))

        def warning(self, message, *args, **kwargs):
            self.warning_messages.append(str(message))

        def error(self, message, *args, **kwargs):
            self.error_messages.append(str(message))

    class FakePilmoji:
        calls: list[dict] = []
        should_raise = pilmoji_raises

        def __init__(self, image, *args, **kwargs):
            self.image = image

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def text(self, xy, text, fill=None, font=None, *args, **kwargs):
            self.__class__.calls.append(
                {
                    "xy": xy,
                    "text": text,
                    "fill": fill,
                    "font": font,
                    "kwargs": kwargs,
                }
            )
            if self.should_raise:
                raise RuntimeError("pilmoji boom")

        def getsize(self, text, *, font=None, spacing=4, emoji_scale_factor=None):
            return (max(len(text), 1) * 10, getattr(font, "size", 16))

    logger = RecordingLogger()

    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logger

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

    pilmoji_module = types.ModuleType("pilmoji")
    pilmoji_module.Pilmoji = FakePilmoji
    pilmoji_helpers_module = types.ModuleType("pilmoji.helpers")
    pilmoji_helpers_module.EMOJI_REGEX = re.compile("😀")

    sys.modules["pilmoji"] = pilmoji_module
    sys.modules["pilmoji.helpers"] = pilmoji_helpers_module

    core_package = types.ModuleType("core")
    core_package.__path__ = [str(CORE_DIR)]
    sys.modules["core"] = core_package

    spec = importlib.util.spec_from_file_location("core.pillowmd_emoji", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["core.pillowmd_emoji"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, logger, FakePilmoji


class FakeFont:
    def __init__(self, *, size: int = 16):
        self.size = size
        self.font_name = "main.ttf"
        self.font_y_correct = {"main.ttf": 0}

    def GetSize(self, text: str):
        return (max(len(text), 1) * 10, self.size)


class FakeClosableImage:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


class FakeLeafFont:
    def __init__(self):
        self.font = object()


class FakeMixFont:
    def __init__(self):
        self.font = object()
        self.seconde_fonts = {"emoji.ttf": FakeLeafFont()}
        self.seconde_font_dict = {"emoji.ttf": {1, 2, 3}}
        self.font_dict = {4, 5, 6}
        self.font_y_correct = {"main.ttf": 0}
        self.seconde_font_paths = ["emoji.ttf"]


class PillowMdEmojiTests(unittest.TestCase):
    def make_pillowmd_module(self):
        class FakeImageDrawPro:
            def __init__(self):
                self._image = object()
                self.text_lock_color = None
                self.text_blod_mode = False
                self.delete_line_mode = False
                self.under_line_mode = False
                self.original_calls: list[dict] = []
                self.line_calls: list[dict] = []

            def text(
                self,
                xy,
                text,
                fill=None,
                font=None,
                use_lock_color=True,
                use_blod_mode=True,
                use_delete_line_mode=True,
                use_under_line_mode=True,
                *args,
                **kwargs,
            ):
                self.original_calls.append(
                    {
                        "xy": xy,
                        "text": text,
                        "fill": fill,
                        "font": font,
                        "kwargs": kwargs,
                    }
                )

        return types.SimpleNamespace(ImageDrawPro=FakeImageDrawPro)

    def test_plain_text_keeps_original_renderer(self):
        module, _logger, fake_pilmoji = load_emoji_module()
        pillowmd = self.make_pillowmd_module()
        self.assertTrue(module.patch_pillowmd_with_pilmoji(pillowmd))

        drawer = pillowmd.ImageDrawPro()
        drawer.text((10, 20), "plain text", fill=(1, 2, 3), font=FakeFont())

        self.assertEqual(len(drawer.original_calls), 1)
        self.assertEqual(fake_pilmoji.calls, [])

    def test_emoji_text_uses_pilmoji_renderer(self):
        module, logger, fake_pilmoji = load_emoji_module()
        pillowmd = self.make_pillowmd_module()
        self.assertTrue(module.patch_pillowmd_with_pilmoji(pillowmd))

        drawer = pillowmd.ImageDrawPro()
        drawer.text((10, 20), "hello😀", fill=(1, 2, 3), font=FakeFont())

        self.assertEqual(drawer.original_calls, [])
        self.assertEqual(len(fake_pilmoji.calls), 1)
        self.assertEqual(fake_pilmoji.calls[0]["text"], "hello😀")
        self.assertTrue(
            any("pilmoji patch enabled" in message for message in logger.info_messages)
        )

    def test_emoji_render_failure_falls_back_to_original_renderer(self):
        module, logger, fake_pilmoji = load_emoji_module(pilmoji_raises=True)
        pillowmd = self.make_pillowmd_module()
        self.assertTrue(module.patch_pillowmd_with_pilmoji(pillowmd))

        drawer = pillowmd.ImageDrawPro()
        drawer.text((10, 20), "hello😀", fill=(1, 2, 3), font=FakeFont())

        self.assertEqual(len(fake_pilmoji.calls), 1)
        self.assertEqual(len(drawer.original_calls), 1)
        self.assertTrue(
            any("pilmoji render failed" in message for message in logger.warning_messages)
        )

    def test_release_render_resources_clears_font_caches_and_closes_images(self):
        module, logger, _fake_pilmoji = load_emoji_module()
        main_image = FakeClosableImage()
        extra_image = FakeClosableImage()
        shared_font = FakeMixFont()
        latex_font = FakeLeafFont()
        pillowmd = types.SimpleNamespace(
            fontCache={"normal": shared_font},
            size_cache={shared_font: {"hello": (10, 20)}},
            latex_font_cache={"latex": latex_font},
        )
        render_result = types.SimpleNamespace(
            image=main_image,
            images=[main_image, extra_image],
        )

        with mock.patch.object(module.gc, "collect") as collect_mock:
            module.release_pillowmd_render_resources(pillowmd, render_result)

        self.assertEqual(main_image.close_count, 1)
        self.assertEqual(extra_image.close_count, 1)
        self.assertEqual(pillowmd.fontCache, {})
        self.assertEqual(pillowmd.size_cache, {})
        self.assertEqual(pillowmd.latex_font_cache, {})
        self.assertIsNone(shared_font.font)
        self.assertEqual(shared_font.seconde_fonts, {})
        self.assertEqual(shared_font.seconde_font_dict, {})
        self.assertEqual(shared_font.font_dict, set())
        self.assertEqual(shared_font.font_y_correct, {})
        self.assertEqual(shared_font.seconde_font_paths, [])
        self.assertIsNone(latex_font.font)
        collect_mock.assert_called_once()
        self.assertTrue(
            any("released pillowmd render resources" in message for message in logger.info_messages)
        )


if __name__ == "__main__":
    unittest.main()
