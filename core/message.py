from __future__ import annotations

import sqlite3
from astrbot.api import logger
from dataclasses import dataclass
from hashlib import sha1
from itertools import count
from time import time
from typing import Any

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig


_UNSET = object()
_LOG_PREFIX = "[portrait.message_cache]"


# =========================
# cache models
# =========================


@dataclass
class _GroupCacheState:
    oldest_cursor: str | int | None
    updated_at: float


@dataclass
class _PageStoreResult:
    inserted_count: int
    overlap_detected: bool


@dataclass
class _PhaseRunResult:
    rounds: int
    scanned_messages: int
    stop_reason: str


@dataclass
class MessageQueryResult:
    """
    消息查询结果对象
    """

    texts: list[str]
    scanned_messages: int
    from_cache: bool

    @property
    def count(self) -> int:
        return len(self.texts)

    @property
    def is_empty(self) -> bool:
        return not self.texts


class _SQLiteMessageCache:
    def __init__(self):
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row
        self._closed = False
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_state (
                group_id TEXT PRIMARY KEY,
                oldest_cursor TEXT,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                message_key TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                message_time INTEGER NOT NULL,
                text TEXT,
                created_at REAL NOT NULL,
                UNIQUE(group_id, message_key)
            );

            CREATE INDEX IF NOT EXISTS idx_group_sender_time
            ON group_messages(group_id, sender_id, message_time DESC, id DESC);
            """
        )

    def close(self):
        if self._closed:
            return
        self._db.close()
        self._closed = True

    def __del__(self):
        self.close()

    def clear(self):
        with self._db:
            self._db.execute("DELETE FROM group_messages")
            self._db.execute("DELETE FROM group_state")

    def clear_group(self, group_id: str):
        with self._db:
            self._db.execute(
                "DELETE FROM group_messages WHERE group_id = ?",
                (group_id,),
            )
            self._db.execute(
                "DELETE FROM group_state WHERE group_id = ?",
                (group_id,),
            )

    def has_group_messages(self, group_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM group_messages WHERE group_id = ? LIMIT 1",
            (group_id,),
        ).fetchone()
        return row is not None

    def get_group_state(self, group_id: str) -> _GroupCacheState | None:
        row = self._db.execute(
            "SELECT oldest_cursor, updated_at FROM group_state WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        oldest_cursor = row["oldest_cursor"]
        if isinstance(oldest_cursor, str) and oldest_cursor.isdigit():
            oldest_cursor = int(oldest_cursor)
        return _GroupCacheState(
            oldest_cursor=oldest_cursor,
            updated_at=float(row["updated_at"]),
        )

    def touch_group(
        self,
        group_id: str,
        *,
        oldest_cursor: str | int | None | object = _UNSET,
    ):
        now = time()
        current = self.get_group_state(group_id)

        if oldest_cursor is _UNSET:
            cursor_value = current.oldest_cursor if current else None
        else:
            cursor_value = None if oldest_cursor is None else str(oldest_cursor)

        with self._db:
            if current is None:
                self._db.execute(
                    """
                    INSERT INTO group_state(group_id, oldest_cursor, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (group_id, cursor_value, now),
                )
            else:
                self._db.execute(
                    """
                    UPDATE group_state
                    SET oldest_cursor = ?, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (cursor_value, now, group_id),
                )

    def add_message(
        self,
        group_id: str,
        message_key: str,
        sender_id: str,
        message_time: int,
        text: str | None,
    ) -> bool:
        with self._db:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO group_messages(
                    group_id,
                    message_key,
                    sender_id,
                    message_time,
                    text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, message_key, sender_id, message_time, text, time()),
            )
        return cursor.rowcount > 0

    def get_user_texts(self, group_id: str, user_id: str, limit: int) -> list[str]:
        rows = self._db.execute(
            """
            SELECT text
            FROM (
                SELECT text, message_time, id
                FROM group_messages
                WHERE group_id = ?
                  AND sender_id = ?
                  AND COALESCE(text, '') <> ''
                ORDER BY message_time DESC, id DESC
                LIMIT ?
            ) recent_messages
            ORDER BY message_time ASC, id ASC
            """,
            (group_id, user_id, limit),
        ).fetchall()
        return [str(row["text"]) for row in rows]


# =========================
# message manager
# =========================


class MessageManager:
    """
    群级共享缓存的消息管理器

    特性：
    - 同一群查询任意用户都会复用已缓存的群消息
    - 每次查询都会先从最新消息增量追平到缓存边界
    - 若目标用户消息仍不足，再从群缓存的最旧边界继续向更老历史扩展
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config.message
        self._cache = _SQLiteMessageCache()
        self._query_seq = count(1)

    def _next_query_id(self) -> str:
        return f"q{next(self._query_seq):06d}"

    # =========================
    # cache helpers
    # =========================

    def _get_fresh_group_state(self, group_id: str) -> _GroupCacheState | None:
        state = self._cache.get_group_state(group_id)
        if not state:
            return None

        if time() - state.updated_at > self.cfg.cache_ttl:
            self._cache.clear_group(group_id)
            return None

        if not self._cache.has_group_messages(group_id):
            self._cache.clear_group(group_id)
            return None

        return state

    def clear_cache(self):
        self._cache.clear()

    def close(self):
        self._cache.close()

    @staticmethod
    def _log_phase_page(
        *,
        query_id: str,
        phase: str,
        group_id: str,
        message_seq: str | int,
        received_count: int,
        inserted_count: int,
        overlap_detected: bool,
        next_message_seq: str | int | None,
        stop_reason: str,
    ):
        logger.debug(
            f"{_LOG_PREFIX} event=page query_id={query_id} phase={phase} "
            f"group={group_id} "
            f"cursor={message_seq} received_count={received_count} "
            f"inserted_count={inserted_count} overlap_detected={overlap_detected} "
            f"next_cursor={next_message_seq} stop_reason={stop_reason}"
        )

    @staticmethod
    def _log_phase_stop(
        *,
        query_id: str,
        phase: str,
        group_id: str,
        message_seq: str | int,
        stop_reason: str,
    ):
        logger.debug(
            f"{_LOG_PREFIX} event=phase_stop query_id={query_id} phase={phase} "
            f"group={group_id} "
            f"cursor={message_seq} stop_reason={stop_reason}"
        )

    @staticmethod
    def _log_query_summary(
        *,
        query_id: str,
        group_id: str,
        target_id: str,
        max_rounds: int,
        had_group_cache: bool,
        from_cache: bool,
        latest_sync: _PhaseRunResult,
        backfill: _PhaseRunResult,
        final_text_count: int,
        final_scanned_messages: int,
    ):
        logger.info(
            f"{_LOG_PREFIX} event=query_summary query_id={query_id} "
            f"group={group_id} target={target_id} max_rounds={max_rounds} "
            f"had_group_cache={had_group_cache} from_cache={from_cache} "
            f"latest_sync_rounds={latest_sync.rounds} "
            f"latest_sync_scanned={latest_sync.scanned_messages} "
            f"latest_sync_stop_reason={latest_sync.stop_reason} "
            f"backfill_rounds={backfill.rounds} "
            f"backfill_scanned={backfill.scanned_messages} "
            f"backfill_stop_reason={backfill.stop_reason} "
            f"final_text_count={final_text_count} "
            f"final_scanned_messages={final_scanned_messages}"
        )

    @staticmethod
    def _log_phase_error(
        *,
        query_id: str,
        phase: str,
        group_id: str,
        message_seq: str | int,
        error: Exception,
    ):
        logger.error(
            f"{_LOG_PREFIX} event=phase_error query_id={query_id} "
            f"phase={phase} group={group_id} cursor={message_seq} error={error}"
        )

    # =========================
    # message parsing
    # =========================

    @staticmethod
    def _extract_message_identity(msg: dict[str, Any]) -> str | None:
        for key in ("message_seq", "real_id", "seq", "message_id"):
            value = msg.get(key)
            if value is not None and value != "":
                return str(value)
        return None

    @staticmethod
    def _extract_text(msg: dict[str, Any]) -> str:
        message = msg.get("message", [])

        if isinstance(message, str):
            return message.strip()

        return "".join(
            seg.get("data", {}).get("text", "")
            for seg in message
            if seg.get("type") == "text"
        ).strip()

    def _build_message_key(self, group_id: str, msg: dict[str, Any]) -> str:
        identity = self._extract_message_identity(msg)
        if identity:
            return f"{group_id}:id:{identity}"

        sender_id = str(msg.get("sender", {}).get("user_id", ""))
        timestamp = int(msg.get("time", 0) or 0)
        text = self._extract_text(msg)
        digest = sha1(" ".join(text.split()).encode("utf-8")).hexdigest()
        return f"{group_id}:fallback:{sender_id}:{timestamp}:{digest}"

    @staticmethod
    def _get_chunk_earliest_message(
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        first_msg = messages[0]
        last_msg = messages[-1]

        if first_msg.get("time", 0) <= last_msg.get("time", 0):
            return first_msg
        return last_msg

    def _get_next_group_cursor(
        self,
        messages: list[dict[str, Any]],
    ) -> str | int | None:
        earliest_msg = self._get_chunk_earliest_message(messages)

        for key in ("message_seq", "real_id", "seq", "message_id"):
            value = earliest_msg.get(key)
            if value is not None and value != "":
                return value
        return None

    def _store_page_messages(
        self,
        group_id: str,
        messages: list[dict[str, Any]],
    ) -> _PageStoreResult:
        inserted_count = 0
        overlap_detected = False

        for msg in messages:
            message_key = self._build_message_key(group_id, msg)
            sender_id = str(msg.get("sender", {}).get("user_id", ""))
            message_time = int(msg.get("time", 0) or 0)
            text = self._extract_text(msg) or None

            inserted = self._cache.add_message(
                group_id=group_id,
                message_key=message_key,
                sender_id=sender_id,
                message_time=message_time,
                text=text,
            )
            if inserted:
                inserted_count += 1
            else:
                overlap_detected = True

        self._cache.touch_group(group_id)
        return _PageStoreResult(
            inserted_count=inserted_count,
            overlap_detected=overlap_detected,
        )

    async def _fetch_group_messages(
        self,
        event: AiocqhttpMessageEvent,
        group_id: str,
        message_seq: str | int,
    ) -> list[dict[str, Any]]:
        result: dict[str, Any] = await event.bot.api.call_action(
            "get_group_msg_history",
            group_id=group_id,
            message_seq=message_seq,
            count=self.cfg.per_query_count,
            reverseOrder=True,
        )
        return result.get("messages", [])

    async def _sync_latest_messages(
        self,
        event: AiocqhttpMessageEvent,
        group_id: str,
        *,
        query_id: str,
        max_rounds: int,
    ) -> _PhaseRunResult:
        rounds = 0
        scanned_messages = 0
        message_seq: str | int = 0
        stop_reason = "round_limit_reached"

        while rounds < max_rounds:
            try:
                messages = await self._fetch_group_messages(event, group_id, message_seq)
            except Exception as e:
                self._log_phase_error(
                    query_id=query_id,
                    phase="latest_sync",
                    group_id=group_id,
                    message_seq=message_seq,
                    error=e,
                )
                stop_reason = "exception"
                break

            if not messages:
                self._log_phase_stop(
                    query_id=query_id,
                    phase="latest_sync",
                    group_id=group_id,
                    message_seq=message_seq,
                    stop_reason="empty_page",
                )
                stop_reason = "empty_page"
                break

            scanned_messages += len(messages)
            page_result = self._store_page_messages(group_id, messages)
            next_message_seq = self._get_next_group_cursor(messages)
            rounds += 1

            page_stop_reason = "continue"

            if page_result.overlap_detected:
                page_stop_reason = "overlap_detected"
                self._log_phase_page(
                    query_id=query_id,
                    phase="latest_sync",
                    group_id=group_id,
                    message_seq=message_seq,
                    received_count=len(messages),
                    inserted_count=page_result.inserted_count,
                    overlap_detected=page_result.overlap_detected,
                    next_message_seq=next_message_seq,
                    stop_reason=page_stop_reason,
                )
                stop_reason = page_stop_reason
                break

            if next_message_seq is None:
                page_stop_reason = "missing_next_cursor"
                self._log_phase_page(
                    query_id=query_id,
                    phase="latest_sync",
                    group_id=group_id,
                    message_seq=message_seq,
                    received_count=len(messages),
                    inserted_count=page_result.inserted_count,
                    overlap_detected=page_result.overlap_detected,
                    next_message_seq=next_message_seq,
                    stop_reason=page_stop_reason,
                )
                stop_reason = page_stop_reason
                break

            if message_seq and str(next_message_seq) == str(message_seq):
                page_stop_reason = "cursor_not_advanced"
                self._log_phase_page(
                    query_id=query_id,
                    phase="latest_sync",
                    group_id=group_id,
                    message_seq=message_seq,
                    received_count=len(messages),
                    inserted_count=page_result.inserted_count,
                    overlap_detected=page_result.overlap_detected,
                    next_message_seq=next_message_seq,
                    stop_reason=page_stop_reason,
                )
                stop_reason = page_stop_reason
                break

            self._log_phase_page(
                query_id=query_id,
                phase="latest_sync",
                group_id=group_id,
                message_seq=message_seq,
                received_count=len(messages),
                inserted_count=page_result.inserted_count,
                overlap_detected=page_result.overlap_detected,
                next_message_seq=next_message_seq,
                stop_reason=page_stop_reason,
            )
            message_seq = next_message_seq

        return _PhaseRunResult(
            rounds=rounds,
            scanned_messages=scanned_messages,
            stop_reason=stop_reason,
        )

    async def _backfill_older_messages(
        self,
        event: AiocqhttpMessageEvent,
        group_id: str,
        target_id: str,
        *,
        query_id: str,
        start_cursor: str | int,
        max_rounds: int,
    ) -> _PhaseRunResult:
        rounds = 0
        scanned_messages = 0
        message_seq: str | int = start_cursor
        stop_reason = "round_limit_reached"

        while rounds < max_rounds:
            if (
                len(
                    self._cache.get_user_texts(
                        group_id,
                        target_id,
                        self.cfg.max_msg_count,
                    )
                )
                >= self.cfg.max_msg_count
            ):
                self._log_phase_stop(
                    query_id=query_id,
                    phase="backfill",
                    group_id=group_id,
                    message_seq=message_seq,
                    stop_reason="target_enough",
                )
                stop_reason = "target_enough"
                break

            try:
                messages = await self._fetch_group_messages(event, group_id, message_seq)
            except Exception as e:
                self._log_phase_error(
                    query_id=query_id,
                    phase="backfill",
                    group_id=group_id,
                    message_seq=message_seq,
                    error=e,
                )
                stop_reason = "exception"
                break

            if not messages:
                self._log_phase_stop(
                    query_id=query_id,
                    phase="backfill",
                    group_id=group_id,
                    message_seq=message_seq,
                    stop_reason="empty_page",
                )
                stop_reason = "empty_page"
                break

            scanned_messages += len(messages)
            page_result = self._store_page_messages(group_id, messages)
            next_message_seq = self._get_next_group_cursor(messages)
            rounds += 1
            page_stop_reason = "continue"

            if next_message_seq is None:
                page_stop_reason = "missing_next_cursor"
                self._log_phase_page(
                    query_id=query_id,
                    phase="backfill",
                    group_id=group_id,
                    message_seq=message_seq,
                    received_count=len(messages),
                    inserted_count=page_result.inserted_count,
                    overlap_detected=page_result.overlap_detected,
                    next_message_seq=next_message_seq,
                    stop_reason=page_stop_reason,
                )
                stop_reason = page_stop_reason
                break

            if message_seq and str(next_message_seq) == str(message_seq):
                page_stop_reason = "cursor_not_advanced"
                self._log_phase_page(
                    query_id=query_id,
                    phase="backfill",
                    group_id=group_id,
                    message_seq=message_seq,
                    received_count=len(messages),
                    inserted_count=page_result.inserted_count,
                    overlap_detected=page_result.overlap_detected,
                    next_message_seq=next_message_seq,
                    stop_reason=page_stop_reason,
                )
                stop_reason = page_stop_reason
                break

            if page_result.inserted_count == 0:
                page_stop_reason = "all_cached"
                self._log_phase_page(
                    query_id=query_id,
                    phase="backfill",
                    group_id=group_id,
                    message_seq=message_seq,
                    received_count=len(messages),
                    inserted_count=page_result.inserted_count,
                    overlap_detected=page_result.overlap_detected,
                    next_message_seq=next_message_seq,
                    stop_reason=page_stop_reason,
                )
                stop_reason = page_stop_reason
                break

            target_count = len(
                self._cache.get_user_texts(group_id, target_id, self.cfg.max_msg_count)
            )
            if target_count >= self.cfg.max_msg_count:
                page_stop_reason = "target_enough"
                self._log_phase_page(
                    query_id=query_id,
                    phase="backfill",
                    group_id=group_id,
                    message_seq=message_seq,
                    received_count=len(messages),
                    inserted_count=page_result.inserted_count,
                    overlap_detected=page_result.overlap_detected,
                    next_message_seq=next_message_seq,
                    stop_reason=page_stop_reason,
                )
                stop_reason = page_stop_reason
                message_seq = next_message_seq
                self._cache.touch_group(group_id, oldest_cursor=message_seq)
                break

            self._log_phase_page(
                query_id=query_id,
                phase="backfill",
                group_id=group_id,
                message_seq=message_seq,
                received_count=len(messages),
                inserted_count=page_result.inserted_count,
                overlap_detected=page_result.overlap_detected,
                next_message_seq=next_message_seq,
                stop_reason=page_stop_reason,
            )
            message_seq = next_message_seq
            self._cache.touch_group(group_id, oldest_cursor=message_seq)

        return _PhaseRunResult(
            rounds=rounds,
            scanned_messages=scanned_messages,
            stop_reason=stop_reason,
        )

    # =========================
    # public api
    # =========================

    async def get_user_texts(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
        *,
        max_rounds: int,
    ) -> MessageQueryResult:
        """
        获取指定用户在群内的历史文本消息
        """
        group_id = str(event.get_group_id())
        target_id = str(target_id)
        query_id = self._next_query_id()

        scanned_messages = 0
        rounds_left = max_rounds

        state = self._get_fresh_group_state(group_id)
        has_group_cache = state is not None
        latest_sync_result = _PhaseRunResult(
            rounds=0,
            scanned_messages=0,
            stop_reason="skipped_no_group_cache",
        )
        backfill_result = _PhaseRunResult(
            rounds=0,
            scanned_messages=0,
            stop_reason="skipped_target_enough",
        )

        if has_group_cache and rounds_left > 0:
            latest_sync_result = await self._sync_latest_messages(
                event,
                group_id,
                query_id=query_id,
                max_rounds=rounds_left,
            )
            rounds_left -= latest_sync_result.rounds
            scanned_messages += latest_sync_result.scanned_messages
            state = self._get_fresh_group_state(group_id)

        texts = self._cache.get_user_texts(group_id, target_id, self.cfg.max_msg_count)

        if len(texts) < self.cfg.max_msg_count and rounds_left > 0:
            start_cursor = state.oldest_cursor if state and state.oldest_cursor else 0
            backfill_result = await self._backfill_older_messages(
                event,
                group_id,
                target_id,
                query_id=query_id,
                start_cursor=start_cursor,
                max_rounds=rounds_left,
            )
            rounds_left -= backfill_result.rounds
            scanned_messages += backfill_result.scanned_messages
        elif rounds_left <= 0:
            backfill_result = _PhaseRunResult(
                rounds=0,
                scanned_messages=0,
                stop_reason="skipped_round_limit_reached",
            )

        texts = self._cache.get_user_texts(group_id, target_id, self.cfg.max_msg_count)
        final_texts = texts[: self.cfg.max_msg_count]
        from_cache = scanned_messages == 0

        self._log_query_summary(
            query_id=query_id,
            group_id=group_id,
            target_id=target_id,
            max_rounds=max_rounds,
            had_group_cache=has_group_cache,
            from_cache=from_cache,
            latest_sync=latest_sync_result,
            backfill=backfill_result,
            final_text_count=len(final_texts),
            final_scanned_messages=scanned_messages,
        )

        return MessageQueryResult(
            texts=final_texts,
            scanned_messages=scanned_messages,
            from_cache=from_cache,
        )
