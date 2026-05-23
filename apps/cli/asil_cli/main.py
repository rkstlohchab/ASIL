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
    Embedder,
    GraphBuilder,
    SourceLanguage,
    TreeSitterParser,
    iter_source_files,
    language_of,
    resolve_repo,
)
from asil_memory import GraphStore, GraphStoreError, VectorStore, VectorStoreError
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    add_completion=False,
    help="ASIL — Engineering Intelligence Infrastructure.",
    no_args_is_help=True,
)
llm_app = typer.Typer(help="LLM router commands.", no_args_is_help=True)
graph_app = typer.Typer(help="Neo4j graph commands.", no_args_is_help=True)
vector_app = typer.Typer(help="Qdrant vector commands.", no_args_is_help=True)
app.add_typer(llm_app, name="llm")
app.add_typer(graph_app, name="graph")
app.add_typer(vector_app, name="vector")

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
    no_graph: Annotated[
        bool,
        typer.Option("--no-graph", help="Parse only; skip writing to Neo4j."),
    ] = False,
    embed: Annotated[
        bool,
        typer.Option(
            "--embed",
            help="Also embed each function/class via the LLM router and upsert into Qdrant. "
            "Opt-in because embeddings cost API calls; ~$0.001 for a small repo on text-embedding-3-small.",
        ),
    ] = False,
) -> None:
    """Resolve (or clone) a repo, walk its source files, parse them, write to Neo4j, print stats.

    Use --no-graph for a parse-only smoke test.
    Use --embed to also embed and store vectors in Qdrant (opt-in; costs API calls).
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

    builder: GraphBuilder | None = None
    repo_key: str | None = None
    if not no_graph:
        try:
            store = GraphStore()
            store.verify_connectivity()
        except GraphStoreError as e:
            console.print(f"[red]neo4j unreachable: {e}[/red]")
            console.print(
                "[yellow]hint: `make up` to start docker services, "
                "or pass --no-graph to skip graph writes.[/yellow]"
            )
            raise typer.Exit(code=2) from None
        builder = GraphBuilder(store)
        repo_key = builder.upsert_repo(repo)
        console.print(f"  graph: [green]repo upserted[/green] (key=[bold]{repo_key}[/bold])")
    else:
        console.print("  graph: [yellow]skipped (--no-graph)[/yellow]")

    # If --embed was passed we need a repo_key even when --no-graph; derive one
    # from the resolved repo via the same helper the graph builder uses.
    if repo_key is None and embed:
        from asil_ingest import repo_key_for

        repo_key = repo_key_for(repo)

    embedder: Embedder | None = None
    vector_dim: int | None = None
    if embed:
        try:
            vstore = VectorStore()
            vstore.verify_connectivity()
        except VectorStoreError as e:
            console.print(f"[red]qdrant unreachable: {e}[/red]")
            raise typer.Exit(code=2) from None
        router = ModelRouter.from_env()
        embedder = Embedder(router=router, vector_store=vstore, repo_root=repo.path)
        # Probe the active embedder once so we know what dim to size the
        # collection at. Cheap (1 throwaway embedding) and lets profile
        # switches Just Work without manual collection management.
        vector_dim = asyncio.run(embedder.probe_dim())
        embedder.ensure_collection(vector_dim)
        console.print(
            f"  vectors: [green]collection ready[/green] "
            f"(dim=[bold]{vector_dim}[/bold], provider={router.active_profile_name})"
        )

    console.print(
        f"[bold]walking[/bold] for languages: {', '.join(lang.value for lang in parsers)}"
    )

    files_parsed = 0
    total_loc = 0
    n_functions = 0
    n_classes = 0
    n_imports = 0
    n_calls = 0
    n_graph_writes = 0
    n_vector_writes = 0
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

        if builder is not None and repo_key is not None:
            try:
                builder.write_file(repo_key, parsed)
                n_graph_writes += 1
            except Exception as e:
                console.print(f"[yellow]graph write failed for {rel}: {e}[/yellow]")

        if embedder is not None and repo_key is not None:
            try:
                written = asyncio.run(embedder.embed_file(repo_key, parsed))
                n_vector_writes += written
            except Exception as e:
                console.print(f"[yellow]embed failed for {rel}: {e}[/yellow]")

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
    if not no_graph:
        table.add_row("graph writes", str(n_graph_writes))
    if embed:
        table.add_row("vector writes", str(n_vector_writes))
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


@graph_app.command("stats")
def graph_stats(
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="Scope to a single repo key (e.g. 'tiangolo/fastapi'). Default: all repos.",
        ),
    ] = None,
) -> None:
    """Show node counts across the graph (overall or per-repo)."""
    configure_logging()
    try:
        store = GraphStore()
        store.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    repos = store.list_repos()
    if repos:
        repo_table = Table(title="indexed repos")
        repo_table.add_column("key")
        repo_table.add_column("spec")
        repo_table.add_column("files", justify="right")
        repo_table.add_column("local")
        repo_table.add_column("commit")
        repo_table.add_column("indexed at")
        for r in repos:
            repo_table.add_row(
                r["key"],
                r["spec"],
                str(r["files"]),
                "yes" if r["is_local"] else "no",
                (r["commit_sha"] or "")[:12],
                r["indexed_at"] or "",
            )
        console.print(repo_table)
    else:
        console.print("[yellow]no repos indexed yet — run `asil ingest <spec>`.[/yellow]")
        return

    counts = store.stats(repo_key=repo)
    title = f"node counts — {repo}" if repo else "node counts (all repos)"
    stats_table = Table(title=title)
    stats_table.add_column("label")
    stats_table.add_column("count", justify="right")
    for label, n in counts.items():
        stats_table.add_row(label, f"{n:,}")
    console.print(stats_table)


@graph_app.command("clear")
def graph_clear(
    repo: Annotated[
        str,
        typer.Argument(help="Repo key to remove (e.g. 'tiangolo/fastapi'). Required."),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Detach-delete every node belonging to a repo. Use before re-ingesting."""
    configure_logging()
    try:
        store = GraphStore()
        store.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    if not yes:
        confirm = typer.confirm(f"delete all nodes for repo {repo!r}?", default=False)
        if not confirm:
            console.print("aborted")
            raise typer.Exit(code=1)
    removed = store.clear_repo(repo)
    console.print(f"[green]removed {removed:,} nodes for {repo!r}[/green]")


@graph_app.command("query")
def graph_query(
    cypher: Annotated[str, typer.Argument(help="Raw Cypher to run. Read-only recommended.")],
    limit: Annotated[int, typer.Option(help="Max rows to print.")] = 20,
) -> None:
    """Run an ad-hoc Cypher query. Debugging only — don't script against this."""
    configure_logging()
    try:
        store = GraphStore()
        store.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    rows = store.query(cypher)
    if not rows:
        console.print("[dim]no rows returned.[/dim]")
        return
    columns = list(rows[0].keys())
    table = Table(title="query result")
    for col in columns:
        table.add_column(col)
    for row in rows[:limit]:
        table.add_row(*[_short(row.get(col)) for col in columns])
    if len(rows) > limit:
        console.print(f"[dim]showing {limit}/{len(rows)} rows[/dim]")
    console.print(table)


def _short(v: object) -> str:
    s = repr(v) if not isinstance(v, str) else v
    return s if len(s) <= 80 else s[:77] + "..."


@vector_app.command("stats")
def vector_stats() -> None:
    """Show point count in the asil_code Qdrant collection (overall + per-repo)."""
    configure_logging()
    try:
        vstore = VectorStore()
        vstore.verify_connectivity()
    except VectorStoreError as e:
        console.print(f"[red]qdrant unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    info = vstore.stats()
    if not info["exists"]:
        console.print(
            "[yellow]collection 'asil_code' doesn't exist yet — "
            "run `asil ingest <spec> --embed` first.[/yellow]"
        )
        return

    overall = Table(title="vector store")
    overall.add_column("metric")
    overall.add_column("value", justify="right")
    overall.add_row("collection", str(info["collection"]))
    overall.add_row("total points", f"{info['total']:,}")
    console.print(overall)

    if info["per_repo"]:
        per = Table(title="per repo")
        per.add_column("repo_key")
        per.add_column("points", justify="right")
        for rk, n in sorted(info["per_repo"].items(), key=lambda x: -x[1]):
            per.add_row(rk, f"{n:,}")
        console.print(per)


@vector_app.command("search")
def vector_search(
    query: Annotated[str, typer.Argument(help="Natural-language query to embed and search.")],
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Scope to one repo key."),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option(help="Filter by kind: 'function' or 'class'."),
    ] = None,
    limit: Annotated[int, typer.Option(help="Top-k results to return.")] = 10,
) -> None:
    """Embed `query` via the active LLM profile and return the top-k closest code chunks."""
    configure_logging()
    try:
        vstore = VectorStore()
        vstore.verify_connectivity()
    except VectorStoreError as e:
        console.print(f"[red]qdrant unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    async def _run() -> list:
        router = ModelRouter.from_env()
        vecs = await router.embed([query])
        return vstore.search(vecs[0], limit=limit, repo_key=repo, kind=kind)

    hits = asyncio.run(_run())
    if not hits:
        console.print("[dim]no matches.[/dim]")
        return

    table = Table(title=f"top {len(hits)} for: {query!r}", show_lines=False, expand=True)
    table.add_column("score", justify="right", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("qualified_name", overflow="fold")
    table.add_column("file:line", overflow="fold")
    for h in hits:
        p = h.payload
        loc = f"{p.get('file_path', '?')}:{p.get('start_line', '?')}"
        table.add_row(
            f"{h.score:.3f}",
            p.get("kind", "?"),
            p.get("qualified_name", "?"),
            loc,
        )
    console.print(table)


@vector_app.command("clear")
def vector_clear(
    repo: Annotated[str, typer.Argument(help="Repo key to remove from the vector store.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Detach-delete every vector point belonging to a repo. Use before re-embedding."""
    configure_logging()
    try:
        vstore = VectorStore()
        vstore.verify_connectivity()
    except VectorStoreError as e:
        console.print(f"[red]qdrant unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    if not yes:
        confirm = typer.confirm(f"delete all vectors for repo {repo!r}?", default=False)
        if not confirm:
            console.print("aborted")
            raise typer.Exit(code=1)
    removed = vstore.clear_repo(repo)
    console.print(f"[green]removed {removed:,} vectors for {repo!r}[/green]")


if __name__ == "__main__":  # pragma: no cover
    app()
