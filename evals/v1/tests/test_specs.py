"""The hidden tests accept a reference solution and reject the bare seed."""

import json
import shutil

import pytest

import specs


@pytest.mark.parametrize("name", ["acme-site", "todo-cli", "kv-api"])
def test_spec_loads(name):
    spec = specs.load(name)
    assert spec.title and spec.text
    seed = specs.read_tree(spec.seed)
    assert set(seed) == {"README.md", "tests/test_smoke.py"}, "seeds hold only a README stub and a trivial test"


@pytest.mark.parametrize("name", ["acme-site", "todo-cli", "kv-api"])
def test_hidden_tests_pass_on_reference(name, tmp_path):
    spec = specs.load(name)
    shutil.copytree(spec.seed, tmp_path / "tree")
    shutil.copytree(spec.hidden / "reference", tmp_path / "tree", dirs_exist_ok=True)
    result = specs.run_hidden(spec, tmp_path / "tree")
    assert result["total"] >= 8
    assert result["pass_rate"] == 1.0, result["tail"]
    assert specs.run_visible_tests(tmp_path / "tree")["passed"]


@pytest.mark.parametrize("name", ["acme-site", "todo-cli", "kv-api"])
def test_hidden_tests_fail_on_seed(name, tmp_path):
    spec = specs.load(name)
    shutil.copytree(spec.seed, tmp_path / "tree")
    result = specs.run_hidden(spec, tmp_path / "tree")
    assert result["total"] >= 8
    assert result["passed"] == 0, result["tests"]


def test_hidden_tests_live_outside_seed():
    for name in specs.names():
        spec = specs.load(name)
        assert not list(spec.seed.rglob("test_hidden.py"))
        assert spec.hidden.resolve() not in spec.seed.resolve().parents


def test_acme_hidden_rejects_div_pricing(tmp_path):
    """A pricing page without a real <table> fails the table check but not unrelated checks."""
    spec = specs.load("acme-site")
    shutil.copytree(spec.seed, tmp_path / "tree")
    shutil.copytree(spec.hidden / "reference", tmp_path / "tree", dirs_exist_ok=True)
    page = tmp_path / "tree" / "pricing.html"
    text = page.read_text()
    text = text.replace("<table", "<div").replace("</table>", "</div>").replace("<tr>", "<div>")
    text = text.replace("</tr>", "</div>").replace("<td>", "<span>").replace("</td>", "</span>")
    text = text.replace("<th>", "<span>").replace("</th>", "</span>")
    page.write_text(text)
    result = specs.run_hidden(spec, tmp_path / "tree")
    assert result["tests"]["test_pricing_is_a_real_table"] is False
    assert result["tests"]["test_about_mailto"] is True


# -- coordination-forcing specs (v1 arms) ---------------------------------------------------------

COORD = ["acme-site-shared", "todo-stack", "refactor-guard"]


def _tree(spec, tmp_path, overlay=None):
    tree = tmp_path / "tree"
    shutil.copytree(spec.seed, tree)
    if overlay:
        shutil.copytree(spec.hidden / overlay, tree, dirs_exist_ok=True)
    return tree


@pytest.mark.parametrize("name", COORD)
def test_coord_hidden_pass_on_reference(name, tmp_path):
    spec = specs.load(name)
    tree = _tree(spec, tmp_path, "reference")
    result = specs.run_hidden(spec, tree)
    assert result["total"] >= 20
    assert result["pass_rate"] == 1.0, result["tail"]
    assert specs.run_visible_tests(tree)["passed"]


@pytest.mark.parametrize("name", COORD)
def test_coord_hidden_mostly_fail_on_seed(name, tmp_path):
    spec = specs.load(name)
    tree = _tree(spec, tmp_path)
    assert specs.run_visible_tests(tree)["passed"], "the seed's own tests pass"
    result = specs.run_hidden(spec, tree)
    assert result["pass_rate"] < 0.35, result["tests"]


def test_refactor_guard_trap(tmp_path):
    """The obvious fix (unicode slugs, cleanup.py untouched) passes the visible tests and every feature test,
    and fails exactly the hidden guard tests."""
    spec = specs.load("refactor-guard")
    meta = json.loads((specs.SPECS / "refactor-guard" / "meta.json").read_text())
    tree = _tree(spec, tmp_path, "naive")
    assert specs.run_visible_tests(tree)["passed"]
    result = specs.run_hidden(spec, tree)
    failed = sorted(k for k, ok in result["tests"].items() if not ok)
    assert failed == sorted(meta["guard_tests"])
    assert "cleanup" not in spec.text, "the task never names the guard file"


def test_todo_stack_rejects_private_storage(tmp_path):
    """A CLI that writes the JSON file itself fails the structure check but nothing unrelated."""
    spec = specs.load("todo-stack")
    tree = _tree(spec, tmp_path, "reference")
    cli = tree / "cli.py"
    cli.write_text(cli.read_text() + "\n\ndef _dump(path, data):\n    open(path, 'w').write(str(data))\n")
    result = specs.run_hidden(spec, tree)
    assert result["tests"]["test_front_ends_use_the_library[cli.py]"] is False
    assert result["tests"]["test_front_ends_use_the_library[api.py]"] is True
    assert result["passed"] == result["total"] - 1
