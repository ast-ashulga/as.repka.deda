import importlib
import pkgutil
from pathlib import Path

import repka

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_all_python_files_compile():
    for folder in (REPO_ROOT / "src", REPO_ROOT / "tests", REPO_ROOT / "deciders" / "workers"):
        if not folder.exists():
            continue
        for path in folder.rglob("*.py"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_all_repka_modules_import():
    for module in pkgutil.walk_packages(repka.__path__, "repka."):
        importlib.import_module(module.name)
