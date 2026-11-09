import importlib
import importlib.util
import pkgutil
import sys
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
        # the dashboard data layer needs the optional `dashboard` group (pandas)
        if module.name == "repka.dashboard_data" and importlib.util.find_spec("pandas") is None:
            continue
        importlib.import_module(module.name)
    # model runtimes live in the decider virtualenvs, never in the main package
    assert not {"torch", "transformers", "mlx"} & set(sys.modules)
