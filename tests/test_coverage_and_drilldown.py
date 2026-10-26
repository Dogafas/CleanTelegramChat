"""Unit tests for sampling transparency, media groups, and channel drill-down (Section 1 & 4)."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Chat
from typing import Any

from subs import (
    compute_coverage,
    compute_metrics,
    format_coverage_short,
    group_media_publications,
    refine_channel_history,
)
from verdict import prepare_model_texts


class MediaGroupAndCoverageTests(unittest.TestCase):
    def test_media_album_grouped_into_single_publication(self):
        # 4 фото в одном альбоме media_group_id="mg_123", подпись только у первого
        now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        m1 = Chat(
            id=101, date=now, media_group_id="mg_123", text="Подпись к альбому",
            caption=None, views=500, reactions=None, forwards=10,
        )
        m2 = Chat(
            id=102, date=now, media_group_id="mg_123", text=None,
            caption=None, views=550, reactions=None, forwards=5,
        )
        m3 = Chat(
            id=103, date=now, media_group_id="mg_123", text=None,
            caption=None, views=400, reactions=None, forwards=2,
        )
        m4 = Chat(
            id=104, date=now, media_group_id="mg_123", text=None,
            caption=None, views=500, reactions=None, forwards=1,
        )
        m_standalone = Chat(
            id=105, date=now, media_group_id=None, text="Одиночный пост",
            caption=None, views=200, reactions=None, forwards=3,
        )

        raw_messages = [m1, m2, m3, m4, m_standalone]
        pubs = group_media_publications(raw_messages)

        # 5 сообщений -> 2 публикации (1 альбом + 1 одиночный пост)
        self.assertEqual(len(pubs), 2)
        album_pub = pubs[0]
        self.assertEqual(album_pub.media_group_id, "mg_123")
        self.assertEqual(album_pub.text, "Подпись к альбому")
        self.assertEqual(album_pub.views, 550)  # максимум по альбому
        self.assertEqual(len(album_pub.message_ids), 4)
        self.assertEqual(len(album_pub.raw_messages), 4)

    def test_compute_coverage_distinguishes_media_only_vs_empty_history(self):
        now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        # 1. Посты есть, но текста нет (только медиа)
        m_no_text = Chat(id=1, date=now, text="", caption="", media_group_id=None)
        cov_media = compute_coverage([m_no_text], scan_started_at=now)
        self.assertEqual(cov_media["status"], "success")
        self.assertEqual(cov_media["raw_messages_count"], 1)
        self.assertEqual(cov_media["text_publications_count"], 0)
        self.assertIn("только медиа без текста", format_coverage_short(cov_media))

        # 2. Ноль постов в истории
        cov_empty = compute_coverage([], scan_started_at=now)
        self.assertEqual(cov_empty["status"], "unavailable")
        self.assertIn("история недоступна", format_coverage_short(cov_empty))

    def test_compute_coverage_tracks_partial_status_and_horizon(self):
        now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        old_date = now - timedelta(days=15)
        m1 = Chat(id=1, date=now, text="Пост 1")
        m2 = Chat(id=2, date=old_date, text="Пост 2")

        # Достигнут горизонт 14 дней (oldest 15d назад <= horizon 14d)
        cov = compute_coverage([m1, m2], scan_started_at=now, horizon_days=14, limit=200)
        self.assertTrue(cov["horizon_reached"])
        self.assertEqual(cov["status"], "success")

    def test_prepare_model_texts_deterministic_uniform_sampling_and_truncation(self):
        # 50 постов с разным текстом
        pubs = [
            Chat(id=i, text=f"Текст поста номер {i}" + (" X" * 600 if i == 5 else ""))
            for i in range(50)
        ]
        # Запрашиваем до 10 текстов, макс 500 символов
        prepared1, trunc1 = prepare_model_texts(pubs, max_texts=10, max_chars=500)
        prepared2, trunc2 = prepare_model_texts(pubs, max_texts=10, max_chars=500)

        # Детерминированность: повторный запуск даёт точно те же ID
        self.assertEqual([pid for pid, _ in prepared1], [pid for pid, _ in prepared2])
        self.assertEqual(len(prepared1), 10)
        # Пост #5 был обрезан, так как превысил 500 символов
        self.assertGreater(trunc1, 0)
        self.assertEqual(trunc1, trunc2)
        # Тексты строго обрезаны до max_chars
        for _, text in prepared1:
            self.assertLessEqual(len(text), 500)

    def test_prepare_model_texts_single_max_text(self):
        pubs = [
            Chat(id=i, date=datetime.now(timezone.utc), text=f"Пост {i}")
            for i in range(1, 10)
        ]
        prepared, trunc = prepare_model_texts(pubs, max_texts=1, max_chars=100)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0][0], 1)
        self.assertEqual(trunc, 0)


class ChannelDrilldownTests(unittest.TestCase):
    def test_refine_channel_history_stops_on_horizon(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        # Исходные 2 поста (новые, за вчера)
        init_msgs = [
            Chat(id=200, date=now, text="Новый 2"),
            Chat(id=199, date=now - timedelta(days=1), text="Новый 1"),
        ]

        # Старые посты в истории: от id=198 до id=180, уходящие на 20 дней назад
        history_store: list[Any] = [
            Chat(id=i, date=now - timedelta(days=(200 - i)), text=f"Архивный {i}")
            for i in range(198, 170, -1)
        ]

        def fake_get_history(cid: int, limit: int = 100, offset_id: int | None = None) -> list[Any]:
            res = [m for m in history_store if offset_id is None or m.id < offset_id]
            return res[:limit]

        app = Chat(get_chat_history=fake_get_history)
        all_msgs, _pubs, cov = refine_channel_history(
            app,
            chat_id=1,
            existing_messages=init_msgs,
            horizon_days=14,
            limit=200,
            scan_started_at=now,
        )
        # Обход остановился при достижении горизонта 14 дней
        self.assertTrue(cov["horizon_reached"])
        self.assertFalse(cov["limit_reached"])
        # Нет дубликатов сообщений
        ids = [m.id for m in all_msgs]
        self.assertEqual(len(ids), len(set(ids)))
        # Самый старый пост имеет дату <= horizon_dt
        oldest_msg = min(all_msgs, key=lambda m: m.id)
        self.assertLessEqual(oldest_msg.date, now - timedelta(days=14))

    def test_refine_channel_history_stops_on_limit(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        init_msgs = [Chat(id=50, date=now, text="Стартовый")]
        history_store = [Chat(id=i, date=now, text=f"Пост {i}") for i in range(49, 0, -1)]

        def fake_get_history(cid: int, limit: int = 100, offset_id: int | None = None) -> list[Any]:
            res = [m for m in history_store if offset_id is None or m.id < offset_id]
            return res[:limit]

        app = Chat(get_chat_history=fake_get_history)

        all_msgs, _, cov = refine_channel_history(
            app,
            chat_id=1,
            existing_messages=init_msgs,
            horizon_days=30,
            limit=15,  # предел 15 сообщений
            scan_started_at=now,
        )
        self.assertEqual(len(all_msgs), 15)
        self.assertTrue(cov["limit_reached"])

    def test_frequency_metric_calculated_on_full_publications_not_model_sample(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        # 30 публикаций за 10 дней -> 3 публ/день -> 90 публ/мес
        pubs = [
            Chat(id=i, date=now - timedelta(days=(10 * i / 30)), text=f"Пост {i}", views=100)
            for i in range(30)
        ]
        m_full = compute_metrics(Chat(title="Канал"), pubs, members=1000)
        self.assertIsNotNone(m_full["posts_per_month"])
        # Частота вычислена по всем 30 публикациям
        self.assertAlmostEqual(m_full["posts_per_month"], 90.0, delta=5.0)

        # Выборка модели берёт только 10 постов, но публикационная нагрузка канала остаётся честной
        model_sample, _ = prepare_model_texts(pubs, max_texts=10)
        self.assertEqual(len(model_sample), 10)
        self.assertEqual(m_full["posts"], 30)


if __name__ == "__main__":
    unittest.main()
