from __future__ import annotations

from pathlib import Path

import pytest
from asil_ingest import (
    SourceLanguage,
    iter_source_files,
    language_of,
    resolve_repo,
)
from asil_ingest.repo_cloner import _parse_remote_spec  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# spec parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        (
            "https://github.com/tiangolo/fastapi",
            ("tiangolo", "fastapi", "https://github.com/tiangolo/fastapi"),
        ),
        (
            "https://github.com/tiangolo/fastapi.git",
            ("tiangolo", "fastapi", "https://github.com/tiangolo/fastapi.git"),
        ),
        (
            "git@github.com:tiangolo/fastapi.git",
            ("tiangolo", "fastapi", "git@github.com:tiangolo/fastapi.git"),
        ),
        (
            "tiangolo/fastapi",
            ("tiangolo", "fastapi", "https://github.com/tiangolo/fastapi.git"),
        ),
        (
            "litestar-org/litestar",
            ("litestar-org", "litestar", "https://github.com/litestar-org/litestar.git"),
        ),
    ],
)
def test_parse_remote_spec(spec: str, expected: tuple[str, str, str]) -> None:
    assert _parse_remote_spec(spec) == expected


def test_parse_remote_spec_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="can't resolve"):
        _parse_remote_spec("not a real spec")


# ---------------------------------------------------------------------------
# local-path resolution
# ---------------------------------------------------------------------------


def test_resolve_repo_local_path(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1")
    resolved = resolve_repo(str(tmp_path), cache_dir=tmp_path / "cache")
    assert resolved.is_local
    assert resolved.path == tmp_path.resolve()
    assert resolved.org is None
    assert resolved.name == tmp_path.name


# ---------------------------------------------------------------------------
# file iteration / ignore rules
# ---------------------------------------------------------------------------


def _seed(root: Path, files: list[str]) -> None:
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# generated\n")


def test_iter_source_files_python_only(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            "src/a.py",
            "src/b.py",
            "src/sub/c.py",
            "src/notes.md",
            "src/data.json",
        ],
    )
    paths = sorted(p.relative_to(tmp_path).as_posix() for p in iter_source_files(tmp_path))
    assert paths == ["src/a.py", "src/b.py", "src/sub/c.py"]


def test_iter_source_files_skips_ignored_dirs(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            "src/a.py",
            ".venv/site-packages/junk.py",
            "node_modules/pkg/index.py",
            "__pycache__/cached.py",
            "build/output.py",
        ],
    )
    paths = sorted(p.relative_to(tmp_path).as_posix() for p in iter_source_files(tmp_path))
    assert paths == ["src/a.py"]


def test_iter_source_files_skips_min_js(tmp_path: Path) -> None:
    _seed(tmp_path, ["src/a.js", "src/b.min.js"])
    paths = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in iter_source_files(tmp_path, [SourceLanguage.javascript])
    )
    assert paths == ["src/a.js"]


def test_iter_source_files_skips_symlinks(tmp_path: Path) -> None:
    _seed(tmp_path, ["src/a.py"])
    link = tmp_path / "link_to_src"
    link.symlink_to(tmp_path / "src", target_is_directory=True)
    paths = sorted(p.relative_to(tmp_path).as_posix() for p in iter_source_files(tmp_path))
    assert paths == ["src/a.py"]  # symlinked dir not followed


def test_iter_source_files_multi_language(tmp_path: Path) -> None:
    _seed(tmp_path, ["src/a.py", "src/b.ts", "src/c.tsx", "src/d.go"])
    paths = sorted(
        p.relative_to(tmp_path).as_posix()
        for p in iter_source_files(
            tmp_path,
            [
                SourceLanguage.python,
                SourceLanguage.typescript,
                SourceLanguage.tsx,
                SourceLanguage.go,
            ],
        )
    )
    assert paths == ["src/a.py", "src/b.ts", "src/c.tsx", "src/d.go"]


# ---------------------------------------------------------------------------
# language_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("foo.py", SourceLanguage.python),
        ("foo.ts", SourceLanguage.typescript),
        ("foo.tsx", SourceLanguage.tsx),
        ("foo.go", SourceLanguage.go),
        ("foo.js", SourceLanguage.javascript),
        ("foo.txt", None),
        ("README", None),
    ],
)
def test_language_of(name: str, expected: SourceLanguage | None) -> None:
    assert language_of(Path(name)) == expected
