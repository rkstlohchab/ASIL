"""Clone a remote repo (or accept a local path) and iterate its source files.

Two surfaces:

  resolve_repo(spec, cache_dir) -> ResolvedRepo
      Accepts a GitHub URL ("https://github.com/org/name"), an "org/name" short
      form, or a local filesystem path. For remote specs, shallow-clones to
      `<cache_dir>/repos/<org>/<name>`. For local specs, returns the path as-is.

  iter_source_files(root, languages) -> Iterator[Path]
      Walks the tree honoring a curated ignore list (venvs, node_modules,
      caches, vendored dirs).

Intentional non-features (deferred):
  - `git fetch` + diff-aware re-index — Phase 1.8.
  - SCIP invocation — Phase 1.6.
  - Auth via GITHUB_TOKEN for private repos — wire via env when needed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from asil_ingest.models import SourceLanguage

# Directories we never recurse into. Conservative — better to miss a vendored
# tree than to index megabytes of build output.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        "target",  # Rust / Java
        "out",
        "site-packages",
        ".idea",
        ".vscode",
        ".asil_cache",
    }
)

# Globs we never index. Vendored or generated.
IGNORED_FILE_GLOBS: frozenset[str] = frozenset(
    {
        "*.min.js",
        "*.min.css",
        "*.map",
        "*.pyc",
        "*.pyo",
        "*.so",
        "*.dylib",
        "*.dll",
    }
)

LANGUAGE_EXTENSIONS: dict[SourceLanguage, tuple[str, ...]] = {
    SourceLanguage.python: (".py",),
    SourceLanguage.typescript: (".ts",),
    SourceLanguage.tsx: (".tsx",),
    SourceLanguage.javascript: (".js", ".mjs", ".cjs"),
    SourceLanguage.go: (".go",),
}


@dataclass(slots=True)
class ResolvedRepo:
    """A repo on local disk, ready to walk."""

    spec: str  # what the user passed
    path: Path  # absolute path on disk
    is_local: bool  # True if `spec` was already a local path
    org: str | None  # for remote repos
    name: str | None  # for remote repos
    commit_sha: str | None = None  # populated on demand


def resolve_repo(spec: str, cache_dir: str | Path) -> ResolvedRepo:
    """Make `spec` available on local disk.

    Local paths are returned as-is. Remote specs ("https://github.com/x/y",
    "git@github.com:x/y.git", "x/y") are shallow-cloned into
    `<cache_dir>/repos/<org>/<name>`. Re-running on an existing clone is a no-op
    — we trust that the user runs `make reset-dbs` / clears the cache when they
    want a fresh checkout.
    """
    cache_root = Path(cache_dir).expanduser().resolve()

    local = Path(spec).expanduser()
    if local.exists() and local.is_dir():
        return ResolvedRepo(
            spec=spec,
            path=local.resolve(),
            is_local=True,
            org=None,
            name=local.name,
        )

    org, name, clone_url = _parse_remote_spec(spec)
    target = cache_root / "repos" / org / name

    if target.exists():
        commit_sha = _git_head_sha(target)
        return ResolvedRepo(
            spec=spec,
            path=target,
            is_local=False,
            org=org,
            name=name,
            commit_sha=commit_sha,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("git") is None:
        raise RuntimeError("`git` is not on PATH — install git to clone remote repos.")

    # Shallow clone keeps Phase 1 fast. Phase 1.8 will switch to incremental fetch.
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(target)],
        check=True,
    )
    return ResolvedRepo(
        spec=spec,
        path=target,
        is_local=False,
        org=org,
        name=name,
        commit_sha=_git_head_sha(target),
    )


def iter_source_files(
    root: Path,
    languages: list[SourceLanguage] | None = None,
) -> Iterator[Path]:
    """Walk `root`, yielding source files for the requested languages.

    Paths are returned absolute. Ordering is depth-first, sorted within each
    directory for reproducibility (so the same repo always indexes in the same
    order — easier diffs, easier debugging).
    """
    languages = languages or [SourceLanguage.python]
    extensions: set[str] = set()
    for lang in languages:
        extensions.update(LANGUAGE_EXTENSIONS.get(lang, ()))

    yield from _walk(root.resolve(), extensions)


def _walk(directory: Path, extensions: set[str]) -> Iterator[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
    except (PermissionError, FileNotFoundError):
        return

    for entry in entries:
        if entry.is_symlink():
            # Skip symlinks — easy way to loop forever otherwise.
            continue
        if entry.is_dir():
            if entry.name in IGNORED_DIRS:
                continue
            yield from _walk(entry, extensions)
        elif entry.is_file():
            if entry.suffix in extensions and not _matches_ignored_glob(entry.name):
                yield entry


def language_of(path: Path) -> SourceLanguage | None:
    """Map a file path to a `SourceLanguage` based on its extension."""
    suffix = path.suffix
    for lang, exts in LANGUAGE_EXTENSIONS.items():
        if suffix in exts:
            return lang
    return None


def module_name_for(rel: str, language: SourceLanguage) -> str:
    """Repo-relative path → dotted module name.

    Python convention is `pkg.subpkg.module`. JS / TS have no module system
    in the language proper, so we adopt the same dotted-path shape: e.g.
    `src/components/Button.tsx` → `src.components.Button`. The convention
    keeps qualified-name lookups uniform across languages.
    """
    stem = rel
    for ext in LANGUAGE_EXTENSIONS.get(language, ()):
        if rel.endswith(ext):
            stem = rel[: -len(ext)]
            break
    return stem.replace("/", ".")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


def _parse_remote_spec(spec: str) -> tuple[str, str, str]:
    """Return (org, name, clone_url) from a remote spec."""
    s = spec.strip()

    # SSH form: git@github.com:org/name(.git)?
    if s.startswith("git@"):
        _, _, rest = s.partition(":")
        org, name = rest.rstrip("/").removesuffix(".git").split("/", 1)
        return org, name, s

    # HTTPS form: https://github.com/org/name(.git)?
    parsed = urlparse(s)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        parts = parsed.path.strip("/").removesuffix(".git").split("/")
        if len(parts) < 2:
            raise ValueError(f"can't parse repo from URL: {spec!r}")
        org, name = parts[0], parts[1]
        return org, name, s

    # Short form: org/name
    if _OWNER_REPO_RE.match(s):
        org, name = s.split("/", 1)
        return org, name, f"https://github.com/{org}/{name}.git"

    raise ValueError(
        f"can't resolve {spec!r} — pass a local directory, an https URL, "
        "or 'org/name' for a GitHub repo."
    )


def _git_head_sha(repo_path: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode("utf-8").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _matches_ignored_glob(filename: str) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(filename, pat) for pat in IGNORED_FILE_GLOBS)
