"""Unit tests for exact channel overlap analysis (Section 2)."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace as Chat

from overlap import (
    _match_forwards,
    _PairContext,
    build_overlap_indices,
    check_duplicate_group_unsubscribes,
    extract_urls,
    find_channel_overlaps,
    normalize_text,
    normalize_url,
)
from subs import Publication


class OverlapNormalizationTests(unittest.TestCase):
    def test_normalize_text_preserves_numbers_and_negations(self):
        text = "  Это  не   10,   а 20 новых  функций в обновлении!  "
        norm = normalize_text(text)
        self.assertIn("не 10, а 20", norm)
        self.assertNotIn("   ", norm)

    def test_normalize_text_short_phrases_ignored(self):
        self.assertEqual(normalize_text("Привет"), "")
        self.assertEqual(normalize_text("Доброе утро!"), "")
        self.assertEqual(normalize_text("Подпишись"), "")

    def test_normalize_url_normalizes_scheme_and_host_preserves_query(self):
        url1 = "HTTP://Example.COM:80/path/to/article/?utm_source=tg&id=42#part1"
        url2 = "http://example.com/path/to/article?utm_source=tg&id=42#part1"
        self.assertEqual(normalize_url(url1), url2)

    def test_normalize_url_different_query_not_collapsed(self):
        url1 = "https://example.com/item?id=1"
        url2 = "https://example.com/item?id=2"
        self.assertNotEqual(normalize_url(url1), normalize_url(url2))

    def test_extract_urls_from_text_and_entities(self):
        text = "Читайте разбор: https://habr.com/ru/articles/123/ и в канале"
        entity = Chat(url="https://github.com/kurigram/kurigram")
        urls = extract_urls(text, entities=[entity])
        self.assertIn("https://habr.com/ru/articles/123", urls)
        self.assertIn("https://github.com/kurigram/kurigram", urls)


class OverlapDetectionTests(unittest.TestCase):
    def test_exact_text_duplicate_found_and_asymmetric(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        dup_text = "Подробный аналитический разбор ситуации на рынке за октябрь с цифрами."

        p_a1 = Publication(id=1, date=now, text=dup_text)
        p_a2 = Publication(id=2, date=now, text="Уникальный пост канала A без совпадений в других.")
        chat_a = Chat(id=101, title="Канал A", username="chan_a")

        p_b1 = Publication(id=10, date=now, text=dup_text)
        p_b2 = Publication(id=11, date=now, text="Уникальный пост 1 канала B без совпадений.")
        p_b3 = Publication(id=12, date=now, text="Уникальный пост 2 канала B без совпадений.")
        p_b4 = Publication(id=13, date=now, text="Уникальный пост 3 канала B без совпадений.")
        chat_b = Chat(id=102, title="Канал B", username="chan_b")

        channels_data = [
            {"chat": chat_a, "publications": [p_a1, p_a2]},
            {"chat": chat_b, "publications": [p_b1, p_b2, p_b3, p_b4]},
        ]

        overlaps = find_channel_overlaps(channels_data)
        self.assertEqual(len(overlaps), 1)
        res = overlaps[0]
        self.assertTrue(res["has_comparable_window"])
        self.assertEqual(res["dup_count_a"], 1)
        self.assertEqual(res["dup_count_b"], 1)
        # Асимметричные доли: 1/2 (50%) в A и 1/4 (25%) в B
        self.assertAlmostEqual(res["overlap_rate_a"], 0.5)
        self.assertAlmostEqual(res["overlap_rate_b"], 0.25)
        self.assertEqual(res["unmatched_a"], 1)
        self.assertEqual(res["unmatched_b"], 3)

    def test_pure_forward_vs_shared_origin(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        chat_a = Chat(id=1, title="A", username="a")
        chat_b = Chat(id=2, title="B", username="b")

        # 1. Чистая пересылка: одинаковый fwd и одинаковый текст
        p_a = Publication(
            id=1, date=now, text="Текст оригинала статьи",
            forward_from_chat_id=999, forward_from_message_id=5,
        )
        p_b = Publication(
            id=2, date=now, text="Текст оригинала статьи",
            forward_from_chat_id=999, forward_from_message_id=5,
        )
        ctx = _PairContext(
            id_a=1, id_b=2, chat_a=chat_a, chat_b=chat_b,
            ids_a={1}, ids_b={2}, map_a={1: p_a}, map_b={2: p_b}
        )
        _, fwd_idx, _ = build_overlap_indices([
            {"chat": chat_a, "publications": [p_a]},
            {"chat": chat_b, "publications": [p_b]},
        ])
        dup_a, dup_b, pure_fwd, shared_orig = _match_forwards(ctx, fwd_idx)
        self.assertEqual(len(pure_fwd), 1)
        self.assertEqual(len(shared_orig), 0)
        self.assertIn(1, dup_a)
        self.assertIn(2, dup_b)

        # 2. Общее происхождение: одинаковый fwd, но авторские комментарии различаются
        p_b_custom = Publication(
            id=3, date=now, text="Мой авторский критический комментарий к статье",
            forward_from_chat_id=999, forward_from_message_id=5
        )
        ctx2 = _PairContext(
            id_a=1, id_b=2, chat_a=chat_a, chat_b=chat_b,
            ids_a={1}, ids_b={3}, map_a={1: p_a}, map_b={3: p_b_custom}
        )
        _, fwd_idx2, _ = build_overlap_indices([
            {"chat": chat_a, "publications": [p_a]},
            {"chat": chat_b, "publications": [p_b_custom]},
        ])
        dup_a2, dup_b2, pure_fwd2, shared_orig2 = _match_forwards(ctx2, fwd_idx2)
        self.assertEqual(len(pure_fwd2), 0)
        self.assertEqual(len(shared_orig2), 1)
        self.assertEqual(len(dup_a2), 0)
        self.assertEqual(len(dup_b2), 0)

    def test_shared_url_with_different_text_not_counted_as_text_duplicate(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        chat_a = Chat(id=1, title="A", username="a")
        chat_b = Chat(id=2, title="B", username="b")
        p_a = Publication(
            id=1, date=now, text="Канал A пишет про новость https://example.com/story1 подробно."
        )
        p_b = Publication(
            id=2, date=now, text="Канал B считает новость фейком https://example.com/story1 кратко."
        )
        channels = [
            {"chat": chat_a, "publications": [p_a]},
            {"chat": chat_b, "publications": [p_b]},
        ]
        overlaps = find_channel_overlaps(channels)
        self.assertEqual(len(overlaps), 1)
        res = overlaps[0]
        # URL общий, но тексты разные: текстовых дублей нет!
        self.assertEqual(res["dup_count_a"], 0)
        self.assertEqual(res["dup_count_b"], 0)
        self.assertEqual(len(res["shared_urls"]), 1)

    def test_incomparable_windows_reported(self):
        date_a = datetime(2026, 9, 1, tzinfo=UTC)
        date_b = datetime(2026, 10, 20, tzinfo=UTC)
        chat_a = Chat(id=1, title="A", username="a")
        chat_b = Chat(id=2, title="B", username="b")
        dup_text = "Одинаковый текст новости, опубликованный с разницей в два месяца."
        p_a = Publication(id=1, date=date_a, text=dup_text)
        p_b = Publication(id=2, date=date_b, text=dup_text)
        channels = [
            {"chat": chat_a, "publications": [p_a]},
            {"chat": chat_b, "publications": [p_b]},
        ]
        overlaps = find_channel_overlaps(channels)
        self.assertEqual(len(overlaps), 1)
        self.assertFalse(overlaps[0]["has_comparable_window"])
        self.assertEqual(overlaps[0]["status"], "incomparable_windows")


class DuplicateGroupWarningsTests(unittest.TestCase):
    def test_warning_when_all_sources_of_duplicate_topic_selected(self):
        chat_a = Chat(id=10, title="Крипта 1")
        chat_b = Chat(id=20, title="Крипта 2")
        overlaps = [{
            "chat_a": chat_a,
            "chat_b": chat_b,
            "has_comparable_window": True,
            "overlap_rate_a": 0.8,
            "overlap_rate_b": 0.75,
        }]
        # Пользователь выбрал ОБА канала на отписку
        warnings = check_duplicate_group_unsubscribes({10, 20}, overlaps, threshold=0.5)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Крипта 1", warnings[0])
        self.assertIn("Крипта 2", warnings[0])

    def test_no_warning_when_one_source_is_retained(self):
        chat_a = Chat(id=10, title="Крипта 1")
        chat_b = Chat(id=20, title="Крипта 2")
        overlaps = [{
            "chat_a": chat_a,
            "chat_b": chat_b,
            "has_comparable_window": True,
            "overlap_rate_a": 0.8,
            "overlap_rate_b": 0.75,
        }]
        # Пользователь оставляет Крипта 2, отписывается только от Крипта 1
        warnings = check_duplicate_group_unsubscribes({10}, overlaps, threshold=0.5)
        self.assertEqual(len(warnings), 0)


if __name__ == "__main__":
    unittest.main()
