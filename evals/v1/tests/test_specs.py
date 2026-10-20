"""The hidden tests accept a reference solution and reject the bare seed."""

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
