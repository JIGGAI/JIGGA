"""Packaging guards — the bundled recipes must be reachable both in the source
tree and in a pip/wheel install (where they ship inside the package as
`jigga/examples`, force-included by pyproject). If `examples_dir()` ever stops
resolving to the recipes, a fresh `pip install jigga` breaks `recipes` /
`init --examples`."""

from __future__ import annotations

from jigga.core.paths import examples_dir


def test_examples_dir_contains_bundled_recipes():
    recipes = examples_dir() / "recipes"
    assert recipes.is_dir(), f"bundled recipes dir missing at {recipes}"
    assert (recipes / "marketing-team.md").exists()


def test_examples_dir_prefers_packaged_layout(monkeypatch, tmp_path):
    """When a `jigga/examples` dir exists next to the package (the wheel
    layout), it wins over the repo-root fallback."""
    import jigga.core.paths as paths

    fake_pkg = tmp_path / "jigga" / "core"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "paths.py").write_text("", encoding="utf-8")
    packaged_examples = tmp_path / "jigga" / "examples"
    (packaged_examples / "recipes").mkdir(parents=True)
    monkeypatch.setattr(paths, "__file__", str(fake_pkg / "paths.py"))
    assert examples_dir() == packaged_examples
