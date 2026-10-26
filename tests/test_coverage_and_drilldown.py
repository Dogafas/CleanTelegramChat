"""Model text sampling stays on the verdict seam."""

import unittest
from types import SimpleNamespace as Chat

from verdict import prepare_model_texts


class ModelTextTests(unittest.TestCase):
    def test_prepare_model_texts_is_deterministic_and_truncated(self):
        pubs = [
            Chat(id=i, text=f"Текст поста номер {i}" + (" X" * 600 if i == 5 else ""))
            for i in range(50)
        ]
        prepared1, trunc1 = prepare_model_texts(pubs, max_texts=10, max_chars=500)
        prepared2, trunc2 = prepare_model_texts(pubs, max_texts=10, max_chars=500)
        self.assertEqual([pid for pid, _ in prepared1], [pid for pid, _ in prepared2])
        self.assertEqual(len(prepared1), 10)
        self.assertGreater(trunc1, 0)
        self.assertEqual(trunc1, trunc2)
        for _, text in prepared1:
            self.assertLessEqual(len(text), 500)

    def test_prepare_model_texts_single_max_text(self):
        pubs = [Chat(id=i, text=f"Пост {i}") for i in range(1, 10)]
        prepared, trunc = prepare_model_texts(pubs, max_texts=1, max_chars=100)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0][0], 1)
        self.assertEqual(trunc, 0)
