from __future__ import annotations

import gc
from typing import Any

from PIL import ImageDraw as PILImageDraw
from astrbot.api import logger


_LOG_PREFIX = "[portrait.pilmoji]"
_PATCH_ATTR = "__portrait_pilmoji_patched__"


def patch_pillowmd_with_pilmoji(pillowmd_module: Any) -> bool:
    image_draw_cls = getattr(pillowmd_module, "ImageDrawPro", None)
    if image_draw_cls is None or not hasattr(image_draw_cls, "text"):
        logger.warning(f"{_LOG_PREFIX} ImageDrawPro not found, skip pilmoji patch")
        return False

    current_text = image_draw_cls.text
    if getattr(current_text, _PATCH_ATTR, False):
        return True

    try:
        from pilmoji import Pilmoji
        from pilmoji.helpers import EMOJI_REGEX
    except Exception as exc:
        logger.warning(f"{_LOG_PREFIX} pilmoji unavailable, skip patch: {exc}")
        return False

    original_text = current_text

    def patched_text(
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
        if not isinstance(text, str) or not text or EMOJI_REGEX.search(text) is None:
            return original_text(
                self,
                xy,
                text,
                fill,
                font,
                use_lock_color,
                use_blod_mode,
                use_delete_line_mode,
                use_under_line_mode,
                *args,
                **kwargs,
            )

        if font is None:
            raise SyntaxError("font为必选项")

        if self.text_lock_color is not None and use_lock_color:
            fill = self.text_lock_color

        font_name = getattr(font, "font_name", "")
        font_y_correct = getattr(font, "font_y_correct", {}) or {}
        font_size = getattr(font, "size", 0)
        move_y = font_y_correct.get(font_name, 0)
        move_y = round(move_y * font_size / 100) if move_y else 0

        def draw_once(offset_x: int = 0, offset_y: int = 0) -> tuple[int, int]:
            with Pilmoji(self._image) as pilmoji:
                pilmoji.text(
                    (xy[0] + offset_x, xy[1] + offset_y - move_y),
                    text,
                    fill,
                    font,
                    *args,
                    **kwargs,
                )
                return pilmoji.getsize(
                    text,
                    font=font,
                    spacing=kwargs.get("spacing", 4),
                    emoji_scale_factor=kwargs.get("emoji_scale_factor"),
                )

        try:
            width, _height = draw_once()
            if self.text_blod_mode and use_blod_mode:
                for offset_x, offset_y in [(-1, 0), (1, 0)]:
                    draw_once(offset_x, offset_y)
        except Exception as exc:
            logger.warning(
                f"{_LOG_PREFIX} pilmoji render failed, fallback to pillowmd text: {exc}"
            )
            return original_text(
                self,
                xy,
                text,
                fill,
                font,
                use_lock_color,
                use_blod_mode,
                use_delete_line_mode,
                use_under_line_mode,
                *args,
                **kwargs,
            )

        if self.delete_line_mode and use_delete_line_mode:
            PILImageDraw.ImageDraw.line(
                self,
                (
                    xy[0],
                    xy[1] + int(font_size / 2),
                    xy[0] + width,
                    xy[1] + int(font_size / 2),
                ),
                fill,
                int(font_size / 10) + 1,
            )

        if self.under_line_mode and use_under_line_mode:
            PILImageDraw.ImageDraw.line(
                self,
                (
                    xy[0],
                    xy[1] + font_size + 2,
                    xy[0] + width,
                    xy[1] + font_size + 2,
                ),
                fill,
                int(font_size / 10) + 1,
            )

    setattr(patched_text, _PATCH_ATTR, True)
    image_draw_cls.text = patched_text
    logger.info(f"{_LOG_PREFIX} pilmoji patch enabled")
    return True


def _best_effort_release_font(font_obj: Any):
    if font_obj is None:
        return

    nested_fonts = getattr(font_obj, "seconde_fonts", None)
    if isinstance(nested_fonts, dict):
        for nested_font in nested_fonts.values():
            try:
                setattr(nested_font, "font", None)
            except Exception:
                pass
        nested_fonts.clear()

    for attr_name, empty_value in (
        ("seconde_font_dict", {}),
        ("font_dict", set()),
        ("font_y_correct", {}),
        ("seconde_font_paths", []),
    ):
        attr = getattr(font_obj, attr_name, None)
        if hasattr(attr, "clear"):
            try:
                attr.clear()
            except Exception:
                pass
        else:
            try:
                setattr(font_obj, attr_name, empty_value)
            except Exception:
                pass

    try:
        setattr(font_obj, "font", None)
    except Exception:
        pass


def _close_render_images(render_result: Any):
    if render_result is None:
        return

    seen_ids: set[int] = set()
    images = []

    main_image = getattr(render_result, "image", None)
    if main_image is not None:
        images.append(main_image)

    extra_images = getattr(render_result, "images", None)
    if extra_images:
        images.extend(extra_images)

    for image in images:
        if image is None or id(image) in seen_ids:
            continue
        seen_ids.add(id(image))
        close = getattr(image, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def release_pillowmd_render_resources(
    pillowmd_module: Any | None,
    render_result: Any | None = None,
):
    _close_render_images(render_result)

    if pillowmd_module is None:
        gc.collect()
        logger.info(f"{_LOG_PREFIX} released pillowmd render resources")
        return

    for cache_name in ("fontCache", "size_cache", "latex_font_cache"):
        cache = getattr(pillowmd_module, cache_name, None)
        if not isinstance(cache, dict):
            continue

        if cache_name == "fontCache":
            for font_obj in list(cache.values()):
                _best_effort_release_font(font_obj)
        elif cache_name == "latex_font_cache":
            for font_obj in list(cache.values()):
                _best_effort_release_font(font_obj)
        elif cache_name == "size_cache":
            for font_obj in list(cache.keys()):
                _best_effort_release_font(font_obj)

        cache.clear()

    gc.collect()
    logger.info(f"{_LOG_PREFIX} released pillowmd render resources")
