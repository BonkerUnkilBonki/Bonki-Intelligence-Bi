"""Pre-flight checks — run this before packaging a build.

Two classes of bug have shipped from editing this project by hand:

  1. a function defined TWICE, where the older copy silently wins;
  2. a module-level name that is USED but no longer DEFINED, because a bulk edit
     removed the assignment along with a function.

Both are invisible to `py_compile` and only fail at runtime, on the host, in front
of the user. This catches them in a second.

    python tools/selfcheck.py
"""
import ast
import builtins
import importlib
import os
import py_compile
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
problems = []


def check_compile():
    for name in sorted(os.listdir(APP)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(APP, name)
        try:
            py_compile.compile(path, doraise=True, cfile="/tmp/_sc.pyc")
        except Exception as e:
            problems.append(f"COMPILE {name}: {e}")


def check_duplicates():
    for name in sorted(os.listdir(APP)):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(APP, name), encoding="utf-8").read()
        tree = ast.parse(src)
        seen = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen:
                    problems.append(
                        f"DUPLICATE {name}: '{node.name}' defined twice "
                        f"(lines {seen[node.name]} and {node.lineno}) — the later "
                        f"one wins, which is usually the stale copy")
                else:
                    seen[node.name] = node.lineno


def check_undefined_module_names():
    """Names used at module level but never defined or imported."""
    for name in sorted(os.listdir(APP)):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(APP, name), encoding="utf-8").read()
        tree = ast.parse(src)
        defined, used = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    defined.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    used.add(node.id)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                for n in ast.walk(node.target):
                    if isinstance(n, ast.Name):
                        defined.add(n.id)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                for n in ast.walk(node.optional_vars):
                    if isinstance(n, ast.Name):
                        defined.add(n.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        unknown = used - defined - set(dir(builtins))
        # function-local variables are covered above; anything left is suspicious
        for u in sorted(unknown):
            if u.startswith("_"):
                continue
            problems.append(f"UNDEFINED {name}: '{u}' is used but never defined")


def check_imports():
    sys.path.insert(0, ROOT)
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:SELFCHECK")
    os.environ.setdefault("USE_POLLING", "1")
    for mod in ("db", "telegram", "gitly", "agent", "rose", "handlers", "lowmem",
                "progress", "modes", "browse", "userbot", "modules"):
        try:
            importlib.import_module(f"app.{mod}")
        except Exception as e:
            problems.append(f"IMPORT app.{mod}: {type(e).__name__}: {e}")


def check_runtime_smoke():
    """Exercise the pieces that break when a constant disappears."""
    sys.path.insert(0, ROOT)
    try:
        from app import gitly
        assert gitly.WEB_UA, "WEB_UA missing"
        assert gitly._SIZE_UNITS, "_SIZE_UNITS missing"
        assert gitly._parse_size("107 MB") > 0
        assert gitly.normalize_repo("https://github.com/a/b.git") == "a/b"
        assert gitly.chunk_text("x" * 5000)
        rel = {"tag_name": "v1", "name": "N", "body": "notes", "prerelease": False,
               "html_url": "u"}
        assert "#" in gitly.build_changelog_text(rel, "a/b")
        assert gitly.asset_sig([{"name": "a", "size": 1, "id": 1}])

        # Actually call the scraper with a fake HTTP layer. A constant that was
        # deleted by a bulk edit shows up here as a NameError.
        import types

        class _Resp:
            status_code = 200
            ok = True
            text = ('<entry><title>v1</title>'
                    '<link href="https://github.com/a/b/releases/tag/v1"/>'
                    '<content type="html">- fixed</content>'
                    '<updated>2026-01-01T00:00:00Z</updated></entry>')

        gitly.requests = types.SimpleNamespace(
            get=lambda *a, **k: _Resp(), post=None)
        got = gitly.fetch_releases_web("a/b", 1)
        assert got and got[0]["tag_name"] == "v1", "scraper returned nothing"
        assert gitly._web_get("http://x")
    except Exception as e:
        problems.append(f"SMOKE gitly: {type(e).__name__}: {e}")


def main():
    check_compile()
    check_duplicates()
    check_undefined_module_names()
    check_imports()
    check_runtime_smoke()
    # only report real failures; the undefined-name pass is heuristic
    hard = [p for p in problems if not p.startswith("UNDEFINED")]
    soft = [p for p in problems if p.startswith("UNDEFINED")]
    for p in hard:
        print("FAIL:", p)
    if soft:
        print(f"({len(soft)} heuristic 'possibly undefined' notes — check if relevant)")
        for p in soft[:8]:
            print("   note:", p)
    if hard:
        print(f"\n{len(hard)} problem(s) — do NOT ship this build.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
