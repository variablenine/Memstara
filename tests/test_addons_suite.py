"""Runs each add-on's own tests (addons/*/tests/test*.py) with the core's, so one command
covers both:

    python3 -m unittest discover -s tests

With addons/ deleted (or empty) this adds nothing. An add-on's tests can also be run on
their own: python3 -m unittest discover -s addons/<name>/tests
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ADDONS = Path(__file__).resolve().parent.parent / "addons"


def _failed(name: str, error: BaseException) -> unittest.TestCase:
    def test(self):
        raise ImportError("%s couldn't be imported: %s: %s" % (name, type(error).__name__, error))
    cls = type("FailedImport", (unittest.TestCase,), {"test_import": test})
    cls.__qualname__ = cls.__name__ = "AddonTestsFailedToImport_" + name.replace(".", "_")
    return cls("test_import")


def addon_test_files():
    """[(add-on name, file)] for every addons/<name>/tests/test*.py, in name order."""
    if not ADDONS.is_dir():
        return []
    out = []
    for d in sorted(ADDONS.iterdir(), key=lambda p: p.name):
        tests = d / "tests"
        if d.name.startswith((".", "_")) or not tests.is_dir():
            continue
        out.extend((d.name, f) for f in sorted(tests.glob("test*.py")) if f.is_file())
    return out


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    for addon, path in addon_test_files():
        name = "addontests_%s_%s" % (addon, path.stem)
        folder = str(path.parent)
        if folder not in sys.path:      # what discover -s does: the tests' own helpers import by name
            sys.path.insert(0, folder)
        mod = sys.modules.get(name)
        if mod is None:
            try:
                spec = importlib.util.spec_from_file_location(name, str(path))
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                spec.loader.exec_module(mod)
            except Exception as e:
                sys.modules.pop(name, None)
                suite.addTest(_failed("addons/%s/tests/%s" % (addon, path.name), e))
                continue
        suite.addTests(loader.loadTestsFromModule(mod))
    return suite
