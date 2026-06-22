from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name,
        REPO_ROOT / path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bump_version = _load_script(".github/bump_version.py", "bump_version")
fetch_version = _load_script(".github/fetch_version.py", "fetch_version")


def test_infer_bump_uses_towncrier_fragment_precedence(tmp_path) -> None:
    changelog_dir = tmp_path / "changelog.d"
    changelog_dir.mkdir()
    (changelog_dir / "feature.added.md").write_text("Add a feature.\n")
    (changelog_dir / "api.breaking.md").write_text("Break an API.\n")

    assert bump_version.infer_bump(changelog_dir) == "major"


def test_infer_bump_rejects_missing_fragments(tmp_path) -> None:
    changelog_dir = tmp_path / "changelog.d"
    changelog_dir.mkdir()

    with pytest.raises(SystemExit):
        bump_version.infer_bump(changelog_dir)


def test_current_version_prefers_highest_pyproject_changelog_or_tag(
    tmp_path,
    monkeypatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.1.0"\n')
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [0.2.0] - 2026-06-22\n")
    monkeypatch.setattr(
        bump_version,
        "get_git_tag_versions",
        lambda _repo_root: ["0.3.0"],
    )

    assert (
        bump_version.get_current_version(pyproject, changelog, tmp_path)
        == "0.3.0"
    )


def test_update_file_replaces_project_version(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.1.0"\n')

    bump_version.update_file(pyproject, "0.2.0")

    assert 'version = "0.2.0"' in pyproject.read_text()


def test_fetch_version_reads_project_version(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.2.3"\n')

    assert fetch_version.fetch_version(pyproject) == "1.2.3"
