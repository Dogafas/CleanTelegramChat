"""Архитектурный гейт слоёв: CLI-сценарии сверху, ядро снизу.

Правила: ядро не импортирует CLI-сценарии; сценарии не импортируют
друг друга (каждый — самостоятельный запуск).

import-linter и pytest-archon требуют пакетную раскладку (каталоги с
__init__.py), репо плоский — поэтому ast без зависимостей.
"""

import ast
import unittest
from pathlib import Path

CLI = {"clean_my_bots", "clean_my_messages", "clean_my_subs", "people", "sort_chats"}
CORE = {
    "bots",
    "catalog",
    "chat_folder",
    "dialogs",
    "overlap",
    "plan",
    "purge",
    "review",
    "session",
    "subs",
    "ui",
    "verdict",
}
ROOT = Path(__file__).resolve().parent.parent


def _imported_roots(path: Path) -> set[str]:
    """Имена верхнеуровневых модулей, которые импортирует файл."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


class LayerTests(unittest.TestCase):
    def test_every_root_module_is_classified(self):
        stems = {p.stem for p in ROOT.glob("*.py")}
        unknown = stems - CORE - CLI
        self.assertEqual(unknown, set(), f"отнеси новые модули к слою: {sorted(unknown)}")

    def test_core_never_imports_cli(self):
        for path in sorted(ROOT.glob("*.py")):
            if path.stem not in CORE:
                continue
            leaked = _imported_roots(path) & CLI
            self.assertEqual(leaked, set(), f"{path.name} импортирует CLI {sorted(leaked)}")

    def test_cli_scenarios_do_not_import_each_other(self):
        for path in sorted(ROOT.glob("*.py")):
            if path.stem not in CLI:
                continue
            others = _imported_roots(path) & (CLI - {path.stem})
            self.assertEqual(others, set(), f"{path.name} импортирует сценарий {sorted(others)}")


if __name__ == "__main__":
    unittest.main()
