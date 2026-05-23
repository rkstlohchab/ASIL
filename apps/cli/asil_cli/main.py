"""ASIL CLI entry point.

Phase 0 commands:
  asil status              — show backing service health
  asil llm ping [--tier T] — round-trip a small completion through the router
  asil llm profile         — show the active LLM profile + provider mapping

Phase 1 commands (incremental):
  asil ingest <spec>       — clone/resolve repo, parse with Tree-sitter, emit stats

Future phases add: ask, replay, drift report, events.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer
from asil_core import configure_logging, get_settings
from asil_core.llm import ModelRouter
from asil_core.llm.profiles import CHAT_TIERS
from asil_ingest import (
    SourceLanguage,
    TreeSitterParser,
    iter_source_files,
    language_of,
    resolve_repo,
)
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    add_completion=False,
    help="ASIL — Engineering Intelligence Infrastructure.",
    no_args_is_help=True,
)
llm_app = typer.Typer(help="LLM router commands.", no_args_is_help=True)
app.add_typer(llm_app, name="llm")

console = Console()


@app.command()
def status() -> None:
    """Show health of every backing service (uses defaults from .env)."""
    configure_logging()
    settings = get_settings()

    async def _probe(url: str) -> tuple[str, str]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(url)
            return ("ok", f"HTTP {r.status_code}")
        except Exception as e:
            return ("down", type(e).__name__)

    async def _run() -> list[tuple[str, str, str, str]]:
        targets = [
            ("neo4j", f"http://{settings.neo4j_uri.split('://', 1)[-1].split(':', 1)[0]}:7474"),
            ("qdrant", settings.qdrant_url),
            ("prometheus", settings.prometheus_url),
            ("loki", f"{settings.loki_url}/ready"),
            ("grafana", "http://localhost:3000/api/health"),
        ]
        results = await asyncio.gather(*[_probe(u) for _, u in targets])
        return [
            (name, url, st, detail)
            for (name, url), (st, detail) in zip(targets, results, strict=True)
        ]

    rows = asyncio.run(_run())
    table = Table(title="ASIL service status")
    table.add_column("service")
    table.add_column("url")
    table.add_column("status")
    table.add_column("detail")
    for name, url, st, detail in rows:
        color = "green" if st == "ok" else "red"
        table.add_row(name, url, f"[{color}]{st}[/{color}]", detail)
    console.print(table)
    console.print(f"\nactive LLM profile: [bold]{settings.asil_llm_profile}[/bold]")


@llm_app.command("ping")
def llm_ping(
    tier: Annotated[
        str, typer.Option(help="Tier to test (reasoning|classify|summarize|verify)")
    ] = "reasoning",
    prompt: Annotated[str, typer.Option(help="Prompt to send")] = "Say hi in 5 words.",
    max_tokens: Annotated[int, typer.Option()] = 64,
) -> None:
    """Round-trip a small completion through the active profile's provider."""
    configure_logging()
    if tier not in CHAT_TIERS:
        console.print(f"[red]invalid tier {tier!r}; choose from {list(CHAT_TIERS)}[/red]")
        raise typer.Exit(code=2)

    async def _run() -> None:
        router = ModelRouter.from_env()
        resp = await router.call(
            tier=tier,  # type: ignore[arg-type]
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        table = Table(title=f"llm ping — tier={tier}")
        table.add_column("field")
        table.add_column("value")
        table.add_row("profile", router.active_profile_name)
        table.add_row("provider", resp.provider)
        table.add_row("model", resp.model)
        table.add_row("input_tokens", str(resp.input_tokens))
        table.add_row("output_tokens", str(resp.output_tokens))
        table.add_row("cost_usd", f"{resp.cost_usd:.6f}")
        console.print(table)
        console.print("\n[bold]response:[/bold]")
        console.print(resp.text or "[dim](empty)[/dim]")

    asyncio.run(_run())


@llm_app.command("profile")
def llm_profile() -> None:
    """Show the active profile's tier → provider/model mapping."""
    from asil_core.llm.profiles import load_profile

    profile = load_profile()
    table = Table(title=f"profile: {profile.name}")
    table.add_column("tier")
    table.add_column("provider")
    table.add_column("model")
    for tier, provider in profile.chat.items():
        table.add_row(tier, provider.name, provider.model)
    table.add_row(
        "embed",
        profile.embedding.name,
        f"{profile.embedding.model} (dim={profile.embedding.dim})",
    )
    console.print(table)


@app.command()
def ingest(
    spec: Annotated[
        str,
        typer.Argument(
            help="Repo spec: local path, https URL, or 'org/name' for a GitHub repo.",
        ),
    ],
    languages: Annotated[
        list[str] | None,
        typer.Option(
            "--language",
            "-l",
            help="Restrict to specific languages. Repeatable. Default: python.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(help="Cap files parsed (useful for smoke tests on huge repos)."),
    ] = None,
    show_errors: Annotated[
        bool,
        typer.Option(help="Print every file that produced a parse error."),
    ] = False,
) -> None:
    """Resolve (or clone) a repo, walk its source files, parse them, print stats.

    Phase 1 step 1 — parse-only. Writes nothing to Neo4j / Qdrant yet.
    """
    configure_logging()
    settings = get_settings()

    chosen: list[SourceLanguage] = []
    if languages:
        for lang in languages:
            try:
                chosen.append(SourceLanguage(lang.lower()))
            except ValueError:
                console.print(
                    f"[red]unknown language: {lang!r}; "
                    f"choose from {[lng.value for lng in SourceLanguage]}[/red]"
                )
                raise typer.Exit(code=2) from None
    else:
        chosen = [SourceLanguage.python]

    cache_dir = Path(settings.asil_cache_dir)

    console.print(f"[bold]resolving[/bold] {spec}...")
    repo = resolve_repo(spec, cache_dir)
    where = f"[dim]({repo.path})[/dim]"
    if repo.is_local:
        console.print(f"  local path {where}")
    else:
        console.print(f"  cloned [green]{repo.org}/{repo.name}[/green] {where}")
        if repo.commit_sha:
            console.print(f"  HEAD = [dim]{repo.commit_sha[:12]}[/dim]")

    parsers: dict[SourceLanguage, TreeSitterParser] = {}
    for lang in chosen:
        try:
            parsers[lang] = TreeSitterParser(lang)
        except NotImplementedError as e:
            console.print(f"[yellow]skipping {lang.value}: {e}[/yellow]")

    if not parsers:
        console.print("[red]no parsers available for the requested languages[/red]")
        raise typer.Exit(code=2)

    console.print(
        f"[bold]walking[/bold] for languages: {', '.join(lang.value for lang in parsers)}"
    )

    files_parsed = 0
    total_loc = 0
    n_functions = 0
    n_classes = 0
    n_imports = 0
    n_calls = 0
    error_files: list[tuple[Path, list[str]]] = []
    per_language_count: dict[SourceLanguage, int] = dict.fromkeys(parsers, 0)

    started = time.monotonic()

    for path in iter_source_files(repo.path, list(parsers.keys())):
        lang = language_of(path)
        if lang is None or lang not in parsers:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = path.relative_to(repo.path).as_posix()
        module = (
            rel.removesuffix(".py").replace("/", ".") if lang is SourceLanguage.python else None
        )
        parsed = parsers[lang].parse(text, path=rel, module_name=module)
        files_parsed += 1
        per_language_count[lang] += 1
        total_loc += parsed.loc
        n_functions += len(parsed.all_functions_including_methods())
        n_classes += len(parsed.classes)
        n_imports += len(parsed.imports)
        n_calls += sum(len(fn.calls) for fn in parsed.all_functions_including_methods())
        if parsed.parse_errors:
            error_files.append((path, parsed.parse_errors))

        if limit is not None and files_parsed >= limit:
            console.print(f"[yellow]--limit {limit} reached, stopping[/yellow]")
            break

    elapsed = time.monotonic() - started

    table = Table(title=f"ingest stats — {spec}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("files parsed", str(files_parsed))
    for lang, count in per_language_count.items():
        table.add_row(f"  {lang.value}", str(count))
    table.add_row("total LOC", f"{total_loc:,}")
    table.add_row("functions (incl. methods)", str(n_functions))
    table.add_row("classes", str(n_classes))
    table.add_row("imports", str(n_imports))
    table.add_row("call sites", str(n_calls))
    table.add_row("files with parse errors", str(len(error_files)))
    table.add_row("elapsed (s)", f"{elapsed:.2f}")
    if files_parsed > 0:
        table.add_row("files / sec", f"{files_parsed / elapsed:.1f}")
    console.print(table)

    if show_errors and error_files:
        console.print("\n[bold]files with parse errors:[/bold]")
        for path, errs in error_files[:50]:
            rel = path.relative_to(repo.path).as_posix()
            console.print(f"  [yellow]{rel}[/yellow] — {'; '.join(errs)}")
        if len(error_files) > 50:
            console.print(f"  [dim]... and {len(error_files) - 50} more[/dim]")


if __name__ == "__main__":  # pragma: no cover
    app()
