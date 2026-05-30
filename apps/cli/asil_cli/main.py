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
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from asil_core import Confidence, configure_logging, get_settings
from asil_core.llm import ModelRouter
from asil_core.llm.profiles import CHAT_TIERS
from asil_infra import (
    PostmortemIngestStats,
    ingest_postmortem,
    load_postmortem,
)
from asil_ingest import (
    CallResolver,
    Embedder,
    GraphBuilder,
    SourceLanguage,
    TreeSitterParser,
    iter_source_files,
    language_of,
    module_name_for,
    resolve_repo,
)
from asil_memory import (
    EpisodicStore,
    EpisodicStoreError,
    GraphStore,
    GraphStoreError,
    HybridRetriever,
    Memory,
    MemoryHit,
    RetrievalResult,
    VectorStore,
    VectorStoreError,
)
from asil_reasoning import Verifier, VerifierResult, score_verified_answer
from asil_temporal import TemporalLinker, find_causes
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    add_completion=False,
    help="ASIL — Engineering Intelligence Infrastructure.",
    no_args_is_help=True,
)
llm_app = typer.Typer(help="LLM router commands.", no_args_is_help=True)
graph_app = typer.Typer(help="Neo4j graph commands.", no_args_is_help=True)
vector_app = typer.Typer(help="Qdrant vector commands.", no_args_is_help=True)
eval_app = typer.Typer(help="Retrieval / reasoning eval harness.", no_args_is_help=True)
memory_app = typer.Typer(help="Episodic memory (past conclusions).", no_args_is_help=True)
postmortem_app = typer.Typer(help="Postmortem ingestion (Phase 3).", no_args_is_help=True)
events_app = typer.Typer(help="Runtime events on the graph (Phase 3).", no_args_is_help=True)
temporal_app = typer.Typer(
    help="Temporal causality engine (Phase 4 — THE MOAT).", no_args_is_help=True
)
app.add_typer(llm_app, name="llm")
app.add_typer(graph_app, name="graph")
app.add_typer(vector_app, name="vector")
app.add_typer(eval_app, name="eval")
app.add_typer(memory_app, name="memory")
app.add_typer(postmortem_app, name="postmortem")
app.add_typer(events_app, name="events")
app.add_typer(temporal_app, name="temporal")

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
    resolve_calls: Annotated[
        bool,
        typer.Option(
            "--resolve-calls/--no-resolve-calls",
            help="After graph writes, promote calls_json text refs to :CALLS edges via heuristics.",
        ),
    ] = True,
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
        module = module_name_for(rel, lang)
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

    # Run call-edge resolution after all files have landed so the function
    # index is complete. Skipped when --no-graph (nothing to resolve against)
    # or when the user explicitly opts out via --no-resolve-calls.
    call_stats = None
    if builder is not None and repo_key is not None and resolve_calls:
        resolver = CallResolver(graph_store=builder._store)  # type: ignore[attr-defined]
        # Clear existing CALLS edges first so re-ingesting doesn't compound
        # heuristic drift across runs.
        resolver.clear_repo_edges(repo_key)
        call_stats = resolver.resolve_repo(repo_key)

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
    if call_stats is not None:
        table.add_row(
            "call edges resolved",
            f"{call_stats.resolved} / {call_stats.total_call_sites}",
        )
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


@app.command()
def ask(
    question: Annotated[
        str, typer.Argument(help="Natural-language question about the indexed code.")
    ],
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Scope retrieval to one repo key."),
    ] = None,
    limit: Annotated[int, typer.Option(help="Top-K snippets to feed the reasoner.")] = 8,
    show_candidates: Annotated[
        bool,
        typer.Option("--show-candidates", help="Also print the raw retrieval table."),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help="Run the second-pass verifier; downgrades Confidence on unsupported claims. "
            "Adds one LLM call (~$0.0003 on tight).",
        ),
    ] = True,
    remember: Annotated[
        bool,
        typer.Option(
            "--remember/--no-remember",
            help="After answering, persist the conclusion to episodic memory (Postgres + Qdrant).",
        ),
    ] = True,
    recall_prior: Annotated[
        bool,
        typer.Option(
            "--recall/--no-recall",
            help="Before answering, check episodic memory for similar prior questions and surface them.",
        ),
    ] = True,
    cache_threshold: Annotated[
        float,
        typer.Option(
            "--cache-threshold",
            help=(
                "If a recalled memory's cosine similarity is >= this threshold, return "
                "the cached answer directly and skip reasoning + verifier. Set 1.01 to "
                "disable the short-circuit (keep memories as prompt context only)."
            ),
        ),
    ] = 0.92,
) -> None:
    """Ask ASIL a question about the indexed code.

    Pipeline (Phase 2): hybrid retrieve -> reasoning LLM -> verifier pass ->
    composed Confidence -> persist to episodic memory. Subsequent runs
    surface similar prior conclusions before producing a fresh answer.

    Cache short-circuit: when --recall is on and the top memory hit's
    similarity is >= --cache-threshold, the cached answer is returned and
    the reasoning + verifier steps are skipped. The episodic store's
    `recall_hits` counter on that memory is incremented so the savings
    calculator can count real cache hits off the ledger.
    """
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None
    try:
        vstore = VectorStore()
        vstore.verify_connectivity()
    except VectorStoreError as e:
        console.print(f"[red]qdrant unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    router = ModelRouter.from_env()
    retriever = HybridRetriever(
        graph_store=gstore,
        vector_store=vstore,
        embedder=router,
        final_limit=limit,
    )

    # Episodic memory is best-effort — if Postgres is unreachable we still
    # answer; the user just doesn't get prior-conclusion recall or persistence.
    estore: EpisodicStore | None = None
    if remember or recall_prior:
        try:
            estore = EpisodicStore(vector_store=vstore)
            estore.verify_connectivity()
            estore.apply_schema()
        except EpisodicStoreError as e:
            console.print(f"[yellow]episodic memory unavailable: {e}[/yellow]")
            estore = None

    verifier = Verifier(router=router) if verify else None

    async def _run() -> tuple[
        RetrievalResult,
        str,
        float,
        VerifierResult | None,
        list[Memory],
        str,
        MemoryHit | None,
    ]:
        # Recall first so the LLM has prior context (cheap — one extra vector query).
        prior_memories: list[Memory] = []
        hits: list[MemoryHit] = []
        question_vec_for_memory: list[float] | None = None
        if estore is not None and recall_prior:
            try:
                vec_batch = await router.embed([question])
                question_vec_for_memory = vec_batch[0]
                hits = estore.recall_similar(
                    query_vector=question_vec_for_memory,
                    repo_key=repo,
                    limit=3,
                    min_similarity=0.85,
                )
                prior_memories = [h.memory for h in hits]
            except Exception as e:
                console.print(f"[yellow]memory recall failed: {e}[/yellow]")

        # Cache short-circuit: if the top recall is similar enough, return its
        # answer immediately and skip the reasoning + verifier LLM calls. The
        # embed call we just paid for is already in the ledger; nothing
        # additional is billed for this ask.
        if hits and hits[0].similarity >= cache_threshold and estore is not None:
            top = hits[0]
            try:
                estore.bump_recall_hit(top.memory.id)
            except Exception as e:
                console.print(f"[yellow]bump_recall_hit failed: {e}[/yellow]")
            return (
                RetrievalResult(
                    query=question,
                    candidates=[],
                    confidence=top.memory.confidence,
                ),
                top.memory.answer,
                0.0,
                None,
                prior_memories,
                router.active_profile_name,
                top,
            )

        result = await retriever.retrieve(question, repo_key=repo)
        if not result.candidates:
            return (
                result,
                "(no candidates retrieved — index may be empty for this repo)",
                0.0,
                None,
                prior_memories,
                "tight",
                None,
            )
        prompt = _build_ask_prompt(question, result, prior_memories=prior_memories)
        resp = await router.call(
            tier="reasoning",
            messages=[{"role": "user", "content": prompt}],
            system=_ASK_SYSTEM_PROMPT,
            max_tokens=900,
            temperature=0.1,
        )
        verifier_result = None
        if verifier is not None:
            verifier_result = await verifier.verify(question, resp.text, result.candidates)
        total_cost = resp.cost_usd + (verifier_result.cost_usd if verifier_result else 0.0)

        # Persist after we have the final composed confidence (verifier may
        # downgrade). Best-effort: if Postgres is down the answer still ships.
        if estore is not None and remember:
            final_conf = result.confidence
            if verifier_result is not None and not verifier_result.skipped:
                final_conf = score_verified_answer(result.confidence, verifier_result)
            try:
                if question_vec_for_memory is None:
                    vec_batch = await router.embed([question])
                    question_vec_for_memory = vec_batch[0]
                estore.remember(
                    repo_key=repo or "(unscoped)",
                    question=question,
                    answer=resp.text,
                    confidence=final_conf,
                    citations=[
                        {
                            "qualified_name": c.qualified_name,
                            "file_path": c.file_path,
                            "start_line": c.start_line,
                            "kind": c.kind,
                            "score": round(c.score, 4),
                        }
                        for c in result.candidates
                    ],
                    model=resp.model,
                    provider=resp.provider,
                    cost_usd=total_cost,
                    profile=router.active_profile_name,
                    verifier_unsupported=(
                        verifier_result.unsupported_count if verifier_result else 0
                    ),
                    question_vector=question_vec_for_memory,
                    origin_agent="cli",
                )
            except Exception as e:
                console.print(f"[yellow]memory write failed: {e}[/yellow]")

        return (
            result,
            resp.text,
            total_cost,
            verifier_result,
            prior_memories,
            router.active_profile_name,
            None,
        )

    (
        result,
        answer_text,
        cost,
        verifier_result,
        prior_memories,
        _profile_name,
        cache_hit_memory_hit,
    ) = asyncio.run(_run())

    if cache_hit_memory_hit is not None:
        top = cache_hit_memory_hit
        when = top.memory.created_at.strftime("%Y-%m-%d %H:%M") if top.memory.created_at else "?"
        console.print(
            Panel(
                (
                    f"[bold]Answer recalled from cache[/bold]\n"
                    f"similarity={top.similarity:.3f} (>= threshold {cache_threshold:.2f})\n"
                    f"original question: {top.memory.question!r}\n"
                    f"originally answered at {when} with confidence "
                    f"{top.memory.confidence.score:.2f}\n"
                    f"reasoning + verifier LLM calls were skipped"
                ),
                title="cache hit",
                border_style="green",
            )
        )

    if verifier_result is not None and not verifier_result.skipped:
        # Replace the retriever's confidence with the verifier-aware composed one.
        result = RetrievalResult(
            query=result.query,
            candidates=result.candidates,
            confidence=score_verified_answer(result.confidence, verifier_result),
        )

    if show_candidates and result.candidates:
        cand_table = Table(title="retrieval candidates", expand=True)
        cand_table.add_column("score", justify="right", no_wrap=True)
        cand_table.add_column("src", no_wrap=True)
        cand_table.add_column("kind", no_wrap=True)
        cand_table.add_column("qualified_name", overflow="fold")
        cand_table.add_column("file:line", overflow="fold")
        for c in result.candidates:
            cand_table.add_row(
                f"{c.score:.3f}",
                "vec" if c.source == "vector" else "graph",
                c.kind,
                c.qualified_name,
                f"{c.file_path}:{c.start_line}",
            )
        console.print(cand_table)

    if prior_memories:
        mem_table = Table(
            title=f"recalled {len(prior_memories)} similar prior conclusion(s)",
            expand=True,
            border_style="magenta",
        )
        mem_table.add_column("when", no_wrap=True)
        mem_table.add_column("score", justify="right", no_wrap=True)
        mem_table.add_column("question", overflow="fold")
        for m in prior_memories:
            when = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "?"
            mem_table.add_row(when, f"{m.confidence.score:.2f}", m.question)
        console.print(mem_table)

    console.print(
        Panel(
            Markdown(answer_text),
            title=f"answer ({router.active_profile_name})",
            border_style="cyan",
        )
    )
    if verifier_result is not None:
        _print_verifier(verifier_result)
    _print_confidence(result.confidence, cost_usd=cost)


def _print_verifier(vr: VerifierResult) -> None:
    if vr.skipped:
        console.print(f"[dim]verifier skipped: {vr.skip_reason}[/dim]")
        return
    title = f"verifier — {len(vr.claims)} claim(s), {vr.unsupported_count} unsupported"
    border = "yellow" if vr.unsupported_count > 0 else "green"
    table = Table(title=title, show_header=True, expand=True, border_style=border)
    table.add_column("ok", no_wrap=True)
    table.add_column("claim", overflow="fold")
    table.add_column("citation", no_wrap=True, overflow="fold")
    for claim in vr.claims:
        flag = "[green]✓[/green]" if claim.supported else "[red]✗[/red]"
        table.add_row(flag, claim.claim, claim.citation or "[dim]none[/dim]")
    console.print(table)


_ASK_SYSTEM_PROMPT = (
    "You are ASIL, the engineering intelligence layer for this codebase. "
    "Answer the user's question using ONLY the code snippets provided. "
    "Rules:\n"
    "  1. Cite every concrete claim with the file:line of the supporting snippet, like (graph_store.py:116). "
    "Multiple cites are fine.\n"
    "  2. If the snippets don't actually answer the question, say so plainly — do not invent.\n"
    "  3. Prefer short, direct prose. Use a fenced ```py``` block only when quoting code is the clearest answer.\n"
    "  4. Never reference 'the snippets' or 'the context' in your response — speak as if you simply know the code.\n"
    "  5. If the answer requires details not present, end with one sentence on what additional evidence would resolve it."
)


def _build_ask_prompt(
    question: str,
    result: RetrievalResult,
    *,
    prior_memories: list[Memory] | None = None,
) -> str:
    lines = [f"Question: {question}", ""]
    if prior_memories:
        lines.append(
            "Prior conclusions on similar questions (use these as background, but answer the current question on its own merits):"
        )
        for i, m in enumerate(prior_memories, 1):
            ts = m.created_at.isoformat(timespec="minutes") if m.created_at else "?"
            lines.append("")
            lines.append(f"[prior {i}] {m.question!r} ({ts}, confidence {m.confidence.score:.2f})")
            lines.append(f"  {m.answer.strip()[:600]}")
        lines.append("")
    lines.append("Code snippets retrieved (most relevant first):")
    for i, c in enumerate(result.candidates, 1):
        header = f"[{i}] {c.qualified_name}  —  {c.file_path}:{c.start_line}"
        if c.signature:
            header += f"  signature: {c.signature}"
        lines.append("")
        lines.append(header)
        if c.docstring:
            lines.append(f"  doc: {c.docstring.strip()[:300]}")
        if c.text:
            # Trim long bodies to keep input tokens bounded; the retriever
            # already chose AST-aligned slices so this rarely cuts mid-thought.
            snippet = c.text if len(c.text) <= 1200 else c.text[:1200] + "\n  …"
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
    lines.extend(
        [
            "",
            "Answer the question now. Cite with file:line as specified.",
        ]
    )
    return "\n".join(lines)


def _print_confidence(conf: Confidence, *, cost_usd: float) -> None:
    badge_color = "green" if conf.score >= 0.6 else ("yellow" if conf.score >= 0.4 else "red")
    table = Table(title="confidence", show_header=False)
    table.add_column("k", style="dim")
    table.add_column("v")
    table.add_row("score", f"[{badge_color}]{conf.score:.3f}[/{badge_color}]")
    table.add_row("evidence_count", str(conf.evidence_count))
    table.add_row("retrieval_strength", f"{conf.retrieval_strength:.3f}")
    if conf.causal_confidence > 0:
        table.add_row("causal_confidence", f"{conf.causal_confidence:.3f}")
    for d in conf.derivation:
        table.add_row("derivation", d)
    table.add_row("llm cost ($)", f"{cost_usd:.6f}")
    console.print(table)


@graph_app.command("resolve-calls")
def graph_resolve_calls(
    repo: Annotated[
        str,
        typer.Argument(help="Repo key (e.g. 'local:/path' or 'org/name')."),
    ],
) -> None:
    """Re-resolve calls_json text refs into :CALLS edges. Idempotent: clears
    existing edges first so heuristic drift across runs doesn't compound."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    resolver = CallResolver(graph_store=gstore)
    removed = resolver.clear_repo_edges(repo)
    if removed:
        console.print(f"  cleared [yellow]{removed:,}[/yellow] existing CALLS edges")
    stats = resolver.resolve_repo(repo)

    table = Table(title=f"call resolution — {repo}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("callers inspected", f"{stats.callers_inspected:,}")
    table.add_row("call sites", f"{stats.total_call_sites:,}")
    table.add_row("resolved", f"{stats.resolved:,}")
    table.add_row("unresolved", f"{stats.unresolved:,}")
    if stats.total_call_sites > 0:
        pct = 100 * stats.resolved / stats.total_call_sites
        table.add_row("resolution rate", f"{pct:.1f}%")
    console.print(table)

    if stats.by_strategy:
        breakdown = Table(title="by strategy")
        breakdown.add_column("strategy")
        breakdown.add_column("count", justify="right")
        for strat, n in sorted(stats.by_strategy.items(), key=lambda x: -x[1]):
            breakdown.add_row(strat, f"{n:,}")
        console.print(breakdown)


@graph_app.command("neighbors")
def graph_neighbors(
    qualified_name: Annotated[
        str,
        typer.Argument(
            help="qualified_name of a Function or Class to inspect (e.g. 'asil_memory.graph_store.GraphStore')."
        ),
    ],
    repo: Annotated[str | None, typer.Option("--repo", help="Scope to one repo key.")] = None,
) -> None:
    """Show the immediate graph neighborhood of a symbol (debug aid for the retriever)."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    cypher = """
    MATCH (n {qualified_name: $qname})
    WHERE (n:Function OR n:Class)
    OPTIONAL MATCH (parent)-[:CONTAINS]->(n)
    OPTIONAL MATCH (n)-[:CONTAINS]->(child)
    RETURN labels(n)[0] AS self_label,
           labels(parent)[0] AS parent_label,
           parent.qualified_name AS parent_qname,
           collect(DISTINCT child.qualified_name) AS children
    """
    rows = gstore.query(cypher, qname=qualified_name)
    if not rows:
        console.print(f"[yellow]no node found with qualified_name={qualified_name!r}[/yellow]")
        return
    row = rows[0]
    console.print(f"[bold]{row['self_label']}[/bold] {qualified_name}")
    if row.get("parent_qname"):
        console.print(f"  contained by [bold]{row['parent_label']}[/bold] {row['parent_qname']}")
    children = [c for c in (row.get("children") or []) if c]
    if children:
        console.print(f"  contains {len(children)}:")
        for c in children:
            console.print(f"    • {c}")


@eval_app.command("recall")
def eval_recall(
    corpus: Annotated[
        str,
        typer.Argument(help="Built-in corpus name ('asil_self') or path to a YAML file."),
    ] = "asil_self",
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="Repo key to evaluate against. Required if the corpus doesn't pin one.",
        ),
    ] = None,
    top_k: Annotated[int, typer.Option(help="Retriever final_limit.")] = 10,
    show_details: Annotated[
        bool,
        typer.Option("--show-details", help="Print per-case top-K and hit rank."),
    ] = False,
) -> None:
    """Run top-K recall over a Q&A corpus. Use this to catch retrieval regressions."""
    from asil_eval import load_corpus, run_recall

    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None
    try:
        vstore = VectorStore()
        vstore.verify_connectivity()
    except VectorStoreError as e:
        console.print(f"[red]qdrant unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    corpus_obj = load_corpus(corpus)
    repo_key = repo or corpus_obj.repo_key
    if repo_key is None:
        console.print("[red]no repo_key — pass --repo or set it inside the corpus YAML[/red]")
        raise typer.Exit(code=2)

    router = ModelRouter.from_env()
    retriever = HybridRetriever(
        graph_store=gstore,
        vector_store=vstore,
        embedder=router,
        final_limit=top_k,
    )

    result = asyncio.run(
        run_recall(corpus_obj, retriever=retriever, top_k=top_k, repo_key_override=repo_key)
    )
    s = result.summary()

    table = Table(title=f"recall — {s['corpus']} ({s['n_cases']} cases)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("repo_key", str(s["repo_key"]))
    table.add_row("recall@1", _color_recall(s["recall@1"]))
    table.add_row("recall@3", _color_recall(s["recall@3"]))
    table.add_row("recall@5", _color_recall(s["recall@5"]))
    table.add_row("recall@10", _color_recall(s["recall@10"]))
    console.print(table)

    if show_details:
        detail = Table(title="per-case detail", expand=True)
        detail.add_column("rank", justify="right", no_wrap=True)
        detail.add_column("question", overflow="fold")
        detail.add_column("top hit", overflow="fold")
        for cr in result.cases:
            rank = str(cr.hit_rank) if cr.hit_rank is not None else "[red]miss[/red]"
            top = cr.top_qnames[0] if cr.top_qnames else "(empty)"
            detail.add_row(rank, cr.case.question, top)
        console.print(detail)

    # PLAN.md eval bar for Phase 1 is top-3 recall >= 80%.
    if s["recall@3"] < 0.80:
        console.print(
            f"[yellow]warning: recall@3 = {s['recall@3']:.0%} is below the "
            "Phase 1 bar of 80%[/yellow]"
        )


def _color_recall(v: float) -> str:
    color = "green" if v >= 0.8 else ("yellow" if v >= 0.5 else "red")
    return f"[{color}]{v:.0%}[/{color}]"


@memory_app.command("stats")
def memory_stats(
    days: Annotated[int, typer.Option(help="Window for dedupe/agent/source aggregations.")] = 30,
    by_source: Annotated[
        bool,
        typer.Option("--by-source", help="Group write events by metadata.source."),
    ] = False,
    by_agent: Annotated[
        bool,
        typer.Option("--by-agent", help="Group write events by origin_agent."),
    ] = False,
    dedupe_rate: Annotated[
        bool,
        typer.Option(
            "--dedupe-rate", help="Show write-time dedupe ratios from asil_memory_writes."
        ),
    ] = False,
    top_recalled: Annotated[
        int,
        typer.Option("--top-recalled", help="Show top-N memories by recall_hits (0 = off)."),
    ] = 0,
) -> None:
    """Total + per-repo counts, plus optional dedupe / sources / top-recalled views."""
    configure_logging()
    estore = _open_episodic_or_exit()
    info = estore.stats()
    overall = Table(title="episodic memory")
    overall.add_column("metric")
    overall.add_column("value", justify="right")
    overall.add_row("total memories", f"{info['total']:,}")
    console.print(overall)
    if info["per_repo"]:
        per = Table(title="per repo")
        per.add_column("repo_key")
        per.add_column("count", justify="right")
        for rk, n in sorted(info["per_repo"].items(), key=lambda x: -x[1]):
            per.add_row(rk, f"{n:,}")
        console.print(per)

    if dedupe_rate or by_agent or by_source:
        try:
            stats = estore.write_log_stats(days=days)
        except Exception as e:
            console.print(f"[yellow]write_log unavailable: {e}[/yellow]")
            stats = None
        if stats:
            if dedupe_rate:
                t = Table(title=f"write outcomes (last {days} days)")
                t.add_column("metric")
                t.add_column("value", justify="right")
                t.add_row("total writes", f"{stats['total_writes']:,}")
                t.add_row("inserted", f"{stats['inserted']:,}")
                t.add_row("folded", f"{stats['folded']:,}")
                t.add_row("dedupe rate", f"{stats['dedupe_rate_pct']:.2f}%")
                console.print(t)
            if by_agent and stats["by_agent"]:
                t = Table(title=f"writes by agent (last {days} days)")
                t.add_column("origin_agent")
                t.add_column("count", justify="right")
                for k, v in stats["by_agent"].items():
                    t.add_row(k, f"{v:,}")
                console.print(t)
            if by_source and stats["by_source"]:
                t = Table(title=f"writes by source (last {days} days)")
                t.add_column("source")
                t.add_column("count", justify="right")
                for k, v in stats["by_source"].items():
                    t.add_row(k, f"{v:,}")
                console.print(t)

    if top_recalled > 0:
        try:
            tops = estore.top_recalled(limit=top_recalled)
        except Exception as e:
            console.print(f"[yellow]top_recalled failed: {e}[/yellow]")
            tops = []
        if tops:
            t = Table(title=f"top {len(tops)} recalled memories", expand=True)
            t.add_column("hits", justify="right", no_wrap=True)
            t.add_column("agent", no_wrap=True)
            t.add_column("question", overflow="fold")
            t.add_column("id", no_wrap=True)
            for m in tops:
                t.add_row(str(m.recall_hits), m.origin_agent, m.question, m.id[:8])
            console.print(t)
    estore.close()


@memory_app.command("list")
def memory_list(
    repo: Annotated[str | None, typer.Option("--repo", help="Scope to one repo.")] = None,
    limit: Annotated[int, typer.Option(help="Max rows to print.")] = 10,
) -> None:
    """Most-recent-first list of past conclusions."""
    configure_logging()
    estore = _open_episodic_or_exit()
    mems = estore.recall_recent(repo_key=repo, limit=limit)
    if not mems:
        console.print("[dim]no memories yet — run `asil ask` first.[/dim]")
        estore.close()
        return
    table = Table(title=f"recent memories ({len(mems)})", expand=True)
    table.add_column("when", no_wrap=True)
    table.add_column("conf", justify="right", no_wrap=True)
    table.add_column("repo", overflow="fold")
    table.add_column("question", overflow="fold")
    table.add_column("id", no_wrap=True)
    for m in mems:
        when = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "?"
        table.add_row(when, f"{m.confidence.score:.2f}", m.repo_key, m.question, m.id[:8])
    console.print(table)
    estore.close()


@memory_app.command("recall")
def memory_recall(
    query: Annotated[str, typer.Argument(help="Natural-language query.")],
    repo: Annotated[str | None, typer.Option("--repo", help="Scope to one repo.")] = None,
    limit: Annotated[int, typer.Option(help="Top-K similar memories.")] = 5,
    min_similarity: Annotated[float, typer.Option(help="Hide hits below this cosine score.")] = 0.5,
) -> None:
    """Find past conclusions whose question is semantically similar to `query`."""
    configure_logging()
    estore = _open_episodic_or_exit()

    async def _run() -> list:
        router = ModelRouter.from_env()
        vec = (await router.embed([query]))[0]
        return estore.recall_similar(
            query_vector=vec,
            repo_key=repo,
            limit=limit,
            min_similarity=min_similarity,
        )

    hits = asyncio.run(_run())
    if not hits:
        console.print("[dim]no similar memories above the threshold.[/dim]")
        estore.close()
        return
    table = Table(title=f"top {len(hits)} memories for: {query!r}", expand=True)
    table.add_column("sim", justify="right", no_wrap=True)
    table.add_column("conf", justify="right", no_wrap=True)
    table.add_column("when", no_wrap=True)
    table.add_column("question", overflow="fold")
    table.add_column("id", no_wrap=True)
    for h in hits:
        when = h.memory.created_at.strftime("%Y-%m-%d %H:%M") if h.memory.created_at else "?"
        table.add_row(
            f"{h.similarity:.3f}",
            f"{h.memory.confidence.score:.2f}",
            when,
            h.memory.question,
            h.memory.id[:8],
        )
    console.print(table)
    estore.close()


@memory_app.command("show")
def memory_show(
    memory_id: Annotated[str, typer.Argument(help="Memory id (full or first 8 chars).")],
) -> None:
    """Print one memory's full answer + citations."""
    configure_logging()
    estore = _open_episodic_or_exit()
    mem = _resolve_memory(estore, memory_id)
    if mem is None:
        console.print(f"[red]no memory found for {memory_id!r}[/red]")
        estore.close()
        raise typer.Exit(code=1)
    console.print(f"[bold]id:[/bold] {mem.id}")
    console.print(f"[bold]when:[/bold] {mem.created_at}")
    console.print(f"[bold]repo:[/bold] {mem.repo_key}")
    console.print(f"[bold]question:[/bold] {mem.question}")
    console.print(
        f"[bold]confidence:[/bold] {mem.confidence.score:.3f}  "
        f"(evidence={mem.confidence.evidence_count}, "
        f"unsupported={mem.verifier_unsupported})"
    )
    console.print()
    console.print(Panel(Markdown(mem.answer), title="answer", border_style="cyan"))
    if mem.citations:
        cit_table = Table(title="citations", expand=True)
        cit_table.add_column("qualified_name", overflow="fold")
        cit_table.add_column("file:line", overflow="fold")
        for c in mem.citations:
            loc = f"{c.get('file_path', '?')}:{c.get('start_line', '?')}"
            cit_table.add_row(c.get("qualified_name", "?"), loc)
        console.print(cit_table)
    estore.close()


@memory_app.command("forget")
def memory_forget(
    memory_id: Annotated[str, typer.Argument(help="Memory id (full or first 8 chars).")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Delete one memory (Postgres row + Qdrant point)."""
    configure_logging()
    estore = _open_episodic_or_exit()
    mem = _resolve_memory(estore, memory_id)
    if mem is None:
        console.print(f"[red]no memory found for {memory_id!r}[/red]")
        estore.close()
        raise typer.Exit(code=1)
    if not yes and not typer.confirm(
        f"delete memory {mem.id[:8]} ({mem.question!r})?", default=False
    ):
        console.print("aborted")
        estore.close()
        raise typer.Exit(code=1)
    ok = estore.forget(mem.id)
    console.print(
        f"[green]forgot {mem.id[:8]}[/green]" if ok else f"[red]could not forget {mem.id[:8]}[/red]"
    )
    estore.close()


@memory_app.command("clear")
def memory_clear(
    repo: Annotated[str, typer.Argument(help="Repo key to clear all memories for.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Wipe every memory for one repo (Postgres rows + Qdrant points)."""
    configure_logging()
    estore = _open_episodic_or_exit()
    if not yes and not typer.confirm(f"delete ALL memories for repo {repo!r}?", default=False):
        console.print("aborted")
        estore.close()
        raise typer.Exit(code=1)
    n = estore.clear_repo(repo)
    console.print(f"[green]cleared {n} memories for {repo!r}[/green]")
    estore.close()


@memory_app.command("forget-session")
def memory_forget_session(
    session_id: Annotated[
        str,
        typer.Argument(help="Session id (e.g. Claude Code .jsonl filename without extension)."),
    ],
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Delete every memory that came from one ingested session.

    Matches both `origin_session_id` (memories written *via* that session)
    and `metadata.original_session_id` (memories ingested *from* that
    session's transcript). Use this to undo an `asil context export` or
    `ingest-transcripts` run that included something sensitive."""
    configure_logging()
    estore = _open_episodic_or_exit()
    if not yes and not typer.confirm(
        f"delete ALL memories from session {session_id!r}?", default=False
    ):
        console.print("aborted")
        estore.close()
        raise typer.Exit(code=1)
    n = estore.forget_session(session_id)
    console.print(
        f"[green]forgot {n} memories from session {session_id[:12]}[/green]"
        if n
        else f"[yellow]no memories matched session {session_id[:12]}[/yellow]"
    )
    estore.close()


@memory_app.command("clear-all")
def memory_clear_all(
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Nuke EVERY memory across every repo and every session. No undo.

    Use this for a clean reset before benchmarking, or to scrub the store
    after testing. Postgres `asil_memories` + Qdrant `asil_memories`
    collection both get wiped."""
    configure_logging()
    estore = _open_episodic_or_exit()
    info = estore.stats()
    total = info["total"]
    if total == 0:
        console.print("[dim]Nothing to clear.[/dim]")
        estore.close()
        return
    if not yes and not typer.confirm(
        f"delete ALL {total} memories across ALL repos? this cannot be undone.",
        default=False,
    ):
        console.print("aborted")
        estore.close()
        raise typer.Exit(code=1)
    n = estore.clear_all()
    console.print(f"[green]nuked {n} memories[/green]")
    estore.close()


def _open_episodic_or_exit() -> EpisodicStore:
    try:
        vstore = VectorStore()
        vstore.verify_connectivity()
    except VectorStoreError:
        vstore = None  # type: ignore[assignment]
    try:
        estore = EpisodicStore(vector_store=vstore)
        estore.verify_connectivity()
        estore.apply_schema()
        return estore
    except EpisodicStoreError as e:
        console.print(f"[red]postgres unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None


def _resolve_memory(estore: EpisodicStore, memory_id: str) -> Memory | None:
    """Accept either a full UUID or its first 8 characters."""
    if len(memory_id) == 36:
        return estore.get(memory_id)
    # Prefix match against recent rows. Phase 2 only — fine for hundreds of memories.
    for m in estore.recall_recent(limit=200):
        if m.id.startswith(memory_id):
            return m
    return None


@postmortem_app.command("ingest")
def postmortem_ingest(
    path: Annotated[
        str,
        typer.Argument(help="Path to a postmortem YAML file (see research/postmortems/)."),
    ],
) -> None:
    """Parse a postmortem YAML and write its timeline into the graph as runtime nodes."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    try:
        pm = load_postmortem(path)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from None

    stats: PostmortemIngestStats = ingest_postmortem(pm, gstore)
    gstore.close()

    table = Table(title=f"postmortem ingested — {stats.incident_id}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("incident", pm.incident.title)
    table.add_row("env", pm.incident.env_key)
    table.add_row("severity", pm.incident.severity)
    table.add_row("services materialized", str(stats.services))
    table.add_row("deployments", str(stats.deployments))
    table.add_row("metric shifts", str(stats.metric_shifts))
    table.add_row("log signatures", str(stats.log_signatures))
    if stats.extra_incidents:
        table.add_row("extra incidents", str(stats.extra_incidents))
    console.print(table)


@events_app.command("list")
def events_list(
    service: Annotated[str, typer.Option("--service", "-s", help="Service name (required).")],
    env: Annotated[
        str, typer.Option("--env", "-e", help="Environment scope, e.g. 'prod'.")
    ] = "prod",
    since: Annotated[
        str | None,
        typer.Option("--since", help="ISO timestamp lower bound, e.g. '2025-08-14T00:00:00Z'."),
    ] = None,
    until: Annotated[str | None, typer.Option("--until", help="ISO timestamp upper bound.")] = None,
    limit: Annotated[int, typer.Option(help="Max events to show.")] = 100,
) -> None:
    """Time-ordered runtime events linked to a service — deployments, metric
    shifts, log signatures, incidents. Phase 4 will add causal-edge overlays."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    events = gstore.events_for_service(
        env_key=env, service_name=service, since=since, until=until, limit=limit
    )
    gstore.close()

    if not events:
        console.print(
            f"[yellow]no events for service={service!r} in env={env!r}"
            + (f" since {since}" if since else "")
            + (f" until {until}" if until else "")
            + ".[/yellow]"
        )
        return

    table = Table(title=f"events — {env}/{service} ({len(events)})", expand=True)
    table.add_column("at", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("detail", overflow="fold")
    table.add_column("source", overflow="fold")
    for ev in events:
        when = _format_event_time(ev.get("at"))
        kind = ev.get("kind", "?")
        table.add_row(when, kind, _format_event_detail(ev), str(ev.get("source") or ""))
    console.print(table)


@events_app.command("stats")
def events_stats(
    env: Annotated[str | None, typer.Option("--env", help="Scope counts to one env.")] = None,
) -> None:
    """Show runtime node counts per label."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None
    counts = gstore.runtime_stats(env_key=env)
    gstore.close()

    title = f"runtime node counts — {env}" if env else "runtime node counts (all envs)"
    table = Table(title=title)
    table.add_column("label")
    table.add_column("count", justify="right")
    for label, n in counts.items():
        table.add_row(label, f"{n:,}")
    console.print(table)


@events_app.command("clear")
def events_clear(
    env: Annotated[str, typer.Argument(help="Env key to wipe runtime nodes for.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Detach-delete every Service/Deployment/MetricShift/LogSignature/Incident
    for one env. Code nodes (Repo/File/Function/Class/Symbol) are untouched."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None
    if not yes and not typer.confirm(f"delete ALL runtime nodes for env {env!r}?", default=False):
        console.print("aborted")
        gstore.close()
        raise typer.Exit(code=1)
    removed = gstore.clear_env(env)
    gstore.close()
    console.print(f"[green]removed {removed} runtime nodes for env {env!r}[/green]")


def _format_event_time(at: object) -> str:
    """Neo4j returns DateTime objects; the events query uses datetime($since) so
    `at` may be a Neo4j DateTime or an ISO string depending on how it was written."""
    if at is None:
        return "?"
    s = str(at)
    # Trim subseconds for terminal display; keep timezone if present.
    if "." in s:
        head, _, tail = s.partition(".")
        # If the tail has a timezone suffix, preserve it.
        tz_chars = "+-Z"
        tz_idx = next((i for i, ch in enumerate(tail) if ch in tz_chars and i > 0), None)
        s = head + tail[tz_idx:] if tz_idx is not None else head
    return s


def _format_event_detail(ev: dict) -> str:
    kind = ev.get("kind")
    if kind == "deployment":
        bits = [f"deploy_id={ev.get('id')}"]
        if ev.get("commit_sha"):
            bits.append(f"commit={str(ev['commit_sha'])[:12]}")
        if ev.get("description"):
            bits.append(str(ev["description"]))
        return " · ".join(bits)
    if kind == "metric_shift":
        before, after, unit = ev.get("before"), ev.get("after"), ev.get("unit") or ""
        bits = [str(ev.get("metric") or "?")]
        if before is not None and after is not None:
            bits.append(f"{before}{unit} → {after}{unit}")
        if ev.get("description"):
            bits.append(str(ev["description"]))
        return " · ".join(bits)
    if kind == "log_signature":
        sig = str(ev.get("signature") or "")
        sig = sig if len(sig) < 80 else sig[:77] + "…"
        bits = [sig]
        if ev.get("count"):
            bits.append(f"x{ev['count']}")
        if ev.get("level"):
            bits.append(f"[{ev['level']}]")
        return " · ".join(bits)
    if kind == "incident":
        bits = [f"id={ev.get('id')}", str(ev.get("title") or "")]
        if ev.get("severity"):
            bits.append(f"sev={ev['severity']}")
        return " · ".join(bits)
    return str(ev)


@temporal_app.command("link")
def temporal_link(
    env: Annotated[str, typer.Argument(help="Env scope to link, e.g. 'prod'.")],
    half_life: Annotated[
        float,
        typer.Option(
            help="Decay half-life in seconds. confidence(Δt) = exp(-ln2·Δt/half_life). "
            "Default 300s = 5min."
        ),
    ] = 300.0,
    lookback_minutes: Annotated[
        float, typer.Option(help="How far back to look for candidate causes.")
    ] = 360.0,
    min_confidence: Annotated[
        float, typer.Option(help="Drop candidates whose proximity score is below this.")
    ] = 0.05,
) -> None:
    """Walk every Incident in `env` and resolve causal edges to it.

    Idempotent: clears existing :PRECEDED edges per incident first so re-running
    with different decay parameters gives a clean slate. Output is a per-incident
    table showing how many candidates were scored vs how many edges were written.
    """
    from datetime import timedelta

    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    linker = TemporalLinker(
        graph_store=gstore,
        lookback=timedelta(minutes=lookback_minutes),
        half_life_seconds=half_life,
        min_confidence=min_confidence,
    )
    all_stats = linker.link_env(env)
    gstore.close()

    if not all_stats:
        console.print(f"[yellow]no incidents in env {env!r}.[/yellow]")
        return

    table = Table(title=f"temporal link — env={env}", expand=True)
    table.add_column("incident", overflow="fold")
    table.add_column("inspected", justify="right", no_wrap=True)
    table.add_column("edges", justify="right", no_wrap=True)
    table.add_column("after-incident", justify="right", no_wrap=True)
    table.add_column("low-conf", justify="right", no_wrap=True)
    table.add_column("by kind", overflow="fold")
    for s in all_stats:
        by_kind = ", ".join(f"{k}={n}" for k, n in s.by_kind.items()) or "-"
        table.add_row(
            s.incident_id,
            str(s.candidates_inspected),
            str(s.edges_written),
            str(s.edges_skipped_after_incident),
            str(s.edges_skipped_low_confidence),
            by_kind,
        )
    console.print(table)
    console.print(
        f"[dim]decay half-life: {half_life}s · lookback: {lookback_minutes}min "
        f"· min confidence: {min_confidence}[/dim]"
    )


@temporal_app.command("causes")
def temporal_causes(
    incident_id: Annotated[str, typer.Argument(help="Incident id to inspect.")],
    min_confidence: Annotated[
        float, typer.Option(help="Hide candidates below this confidence.")
    ] = 0.05,
    limit: Annotated[int, typer.Option(help="Max candidates to print.")] = 20,
    score: Annotated[
        bool,
        typer.Option(
            "--score/--read",
            help="--score recomputes scores live without writing edges; "
            "--read returns whatever edges are already in the graph.",
        ),
    ] = False,
) -> None:
    """Show the ranked causal candidates for one incident.

    Two modes:
      --read  (default): query existing :PRECEDED edges. Fast, deterministic.
      --score: recompute live via the linker without writing. Useful when you
               want to try different half-lives without re-linking.
    """
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    if score:
        candidates = find_causes(gstore, incident_id, min_confidence=min_confidence, limit=limit)
        if not candidates:
            console.print(f"[yellow]no candidates for {incident_id!r}.[/yellow]")
            gstore.close()
            return
        table = Table(title=f"live-scored causes — {incident_id}", expand=True)
        table.add_column("conf", justify="right", no_wrap=True)
        table.add_column("Δt", justify="right", no_wrap=True)
        table.add_column("kind", no_wrap=True)
        table.add_column("cause", overflow="fold")
        for c in candidates:
            table.add_row(
                _color_conf(c.confidence),
                _fmt_delta(c.delta_seconds),
                c.cause_kind,
                c.cause_label,
            )
        console.print(table)
    else:
        rows = gstore.causes_for_incident(incident_id, min_confidence=min_confidence, limit=limit)
        if not rows:
            console.print(
                f"[yellow]no :PRECEDED edges on {incident_id!r}. "
                "Run `asil temporal link <env>` first, or use --score to compute live.[/yellow]"
            )
            gstore.close()
            return
        table = Table(title=f"persisted causes — {incident_id}", expand=True)
        table.add_column("conf", justify="right", no_wrap=True)
        table.add_column("Δt", justify="right", no_wrap=True)
        table.add_column("kind", no_wrap=True)
        table.add_column("identity", overflow="fold")
        table.add_column("derivation", overflow="fold")
        for r in rows:
            ident = _identity_label(r["cause_kind"], r["cause_props"])
            table.add_row(
                _color_conf(float(r["confidence"])),
                _fmt_delta(float(r["delta_seconds"])),
                str(r["cause_kind"]),
                ident,
                str(r.get("derivation") or ""),
            )
        console.print(table)
    gstore.close()


def _color_conf(c: float) -> str:
    color = "green" if c >= 0.6 else ("yellow" if c >= 0.3 else "red")
    return f"[{color}]{c:.3f}[/{color}]"


def _fmt_delta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.2f}h"


def _identity_label(kind: str, props: dict) -> str:
    if kind == "Deployment":
        return f"{props.get('deployment_id', '?')} on {props.get('service_name', '?')}"
    if kind == "MetricShift":
        before, after, unit = props.get("before"), props.get("after"), props.get("unit") or ""
        delta = (
            f" ({before}{unit} → {after}{unit})" if before is not None and after is not None else ""
        )
        return f"{props.get('service_name', '?')}.{props.get('metric', '?')}{delta}"
    if kind == "LogSignature":
        sig = str(props.get("signature") or "")
        sig = sig if len(sig) < 70 else sig[:67] + "…"
        return f'"{sig}" on {props.get("service_name", "?")}'
    return str(props)


# ---------------------------------------------------------------------------
# replay command (Phase 5 step 1 — the hero demo)
# ---------------------------------------------------------------------------


@app.command()
def replay(
    incident_id: Annotated[str, typer.Argument(help="Incident id to replay.")],
    causes_limit: Annotated[int, typer.Option(help="Max top causes to show.")] = 5,
) -> None:
    """Replay an incident: timeline + top causes + service cascade + confidence card.

    Reads from the causal graph — does NOT invent causes. If :PRECEDED edges
    haven't been written yet, run `asil temporal link <env>` first.
    """
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    from asil_replay import ReplayEngine

    engine = ReplayEngine(graph_store=gstore)
    result = engine.replay(incident_id, causes_limit=causes_limit)
    if result is None:
        console.print(f"[yellow]incident {incident_id!r} not found in graph.[/yellow]")
        gstore.close()
        raise typer.Exit(code=1)

    # 1. Header panel
    header_text = "\n".join(result.summary_lines)
    console.print(Panel(header_text, title="incident", border_style="bold cyan"))

    # 2. Timeline table
    if result.timeline:
        tl = Table(title="timeline", expand=True, show_lines=False)
        tl.add_column("at", no_wrap=True)
        tl.add_column("marker", no_wrap=True)
        tl.add_column("kind", no_wrap=True)
        tl.add_column("service", no_wrap=True)
        tl.add_column("description", overflow="fold")
        for entry in result.timeline:
            marker_color = {
                "↗ cause": "green",
                "▶ INCIDENT": "bold red",
                "↓ response": "dim",
            }.get(entry.marker, "")
            marker_str = f"[{marker_color}]{entry.marker}[/{marker_color}]" if marker_color else ""
            tl.add_row(entry.at, marker_str, entry.kind, entry.service, entry.description)
        console.print(tl)
    else:
        console.print("[dim]no timeline events found.[/dim]")

    # 3. Top causes table
    if result.top_causes:
        ct = Table(title="top causes", expand=True)
        ct.add_column("conf", justify="right", no_wrap=True)
        ct.add_column("Δt", justify="right", no_wrap=True)
        ct.add_column("kind", no_wrap=True)
        ct.add_column("identity", overflow="fold")
        ct.add_column("strategy", overflow="fold")
        for c in result.top_causes:
            ident = _identity_label(c.get("cause_kind", ""), c.get("cause_props", {}))
            ct.add_row(
                _color_conf(float(c.get("confidence", 0))),
                _fmt_delta(float(c.get("delta_seconds", 0))),
                str(c.get("cause_kind", "")),
                ident,
                str(c.get("strategy", "")),
            )
        console.print(ct)
    else:
        console.print(
            "[yellow]no causal edges found. Run `asil temporal link <env>` first.[/yellow]"
        )

    # 4. Service cascade
    if result.service_cascade:
        cascade_lines: list[str] = []
        for i, sc in enumerate(result.service_cascade):
            at_short = (
                sc.first_event_at.split("T")[-1][:8]
                if "T" in sc.first_event_at
                else sc.first_event_at
            )
            cascade_lines.append(
                f"[bold]{sc.service}[/bold]  (first event {at_short} — {sc.first_event_kind})"
            )
            if i < len(result.service_cascade) - 1:
                cascade_lines.append(" ↓")
        console.print(Panel("\n".join(cascade_lines), title="service cascade", border_style="cyan"))

    # 5. State diff (before/after)
    if result.state_diff is not None:
        sd = result.state_diff
        diff_parts: list[str] = []
        if sd.deployments_during:
            diff_parts.append("[bold]Deployments during incident window:[/bold]")
            for d in sd.deployments_during:
                at_short = d.at.split("T")[-1][:8] if "T" in d.at else d.at
                sha_part = f" (sha: {d.commit_sha})" if d.commit_sha else ""
                diff_parts.append(f"  • {d.deployment_id} on {d.service}{sha_part} at {at_short}")
                if d.description:
                    diff_parts.append(f"    {d.description}")
        if sd.metric_deltas:
            if diff_parts:
                diff_parts.append("")
            diff_parts.append("[bold]Metric changes:[/bold]")
            for m in sd.metric_deltas:
                before = f"{m.before}" if m.before is not None else "?"
                after = f"{m.after}" if m.after is not None else "?"
                diff_parts.append(f"  • {m.service}.{m.metric}: {before}{m.unit} → {after}{m.unit}")
        if diff_parts:
            console.print(Panel("\n".join(diff_parts), title="state diff", border_style="yellow"))

    # 6. Confidence card
    conf = result.confidence
    conf_color = "green" if conf.score >= 0.6 else ("yellow" if conf.score >= 0.3 else "red")
    conf_text = (
        f"[{conf_color}]Score: {conf.score:.3f}[/{conf_color}]\n"
        f"Evidence: {conf.evidence_count} causal edges\n"
        f"Derivation: {conf.derivation}"
    )
    console.print(Panel(conf_text, title="confidence", border_style="cyan"))

    gstore.close()


# ---------------------------------------------------------------------------
# drift commands (Phase 6 — architecture drift detection)
# ---------------------------------------------------------------------------

drift_app = typer.Typer(help="Architecture drift detection.")
app.add_typer(drift_app, name="drift")


@drift_app.command("baseline")
def drift_baseline(
    repo_key: Annotated[str, typer.Argument(help="Repo key to snapshot.")],
    output: Annotated[str, typer.Option(help="Path to save the baseline JSON.")] = "",
) -> None:
    """Capture the current dependency structure as a baseline snapshot."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    from asil_drift import BaselineLearner

    learner = BaselineLearner(graph_store=gstore)
    snapshot = learner.capture(repo_key)

    console.print(
        Panel(
            f"Repo: {snapshot.repo_key}\n"
            f"Captured: {snapshot.captured_at.isoformat()}\n"
            f"Edges: {len(snapshot.edges)}\n"
            f"Modules: {snapshot.module_count}\n"
            f"Functions: {snapshot.function_count}",
            title="baseline snapshot",
            border_style="cyan",
        )
    )

    if output:
        import json as _json
        from dataclasses import asdict

        data = asdict(snapshot)
        data["captured_at"] = snapshot.captured_at.isoformat()
        Path(output).write_text(_json.dumps(data, indent=2, default=str))
        console.print(f"[green]Saved to {output}[/green]")

    gstore.close()


@drift_app.command("report")
def drift_report(
    repo_key: Annotated[str, typer.Argument(help="Repo key to check.")],
    baseline_path: Annotated[str, typer.Option("--baseline", help="Path to baseline JSON.")] = "",
) -> None:
    """Compare current graph against a baseline and show drift events."""
    configure_logging()
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        console.print(f"[red]neo4j unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None

    from asil_drift import DriftDetector

    if baseline_path:
        import json as _json
        from datetime import datetime as _dt

        from asil_drift.models import BaselineSnapshot, DependencyEdge

        raw = _json.loads(Path(baseline_path).read_text())
        baseline = BaselineSnapshot(
            repo_key=raw["repo_key"],
            captured_at=_dt.fromisoformat(raw["captured_at"]),
            edges=[
                DependencyEdge(
                    caller=e["caller"],
                    callee=e["callee"],
                    file_path=e.get("file_path", ""),
                    line=e.get("line", 0),
                )
                for e in raw.get("edges", [])
            ],
            module_count=raw.get("module_count", 0),
            function_count=raw.get("function_count", 0),
        )
    else:
        # No saved baseline — use empty baseline (everything is "new")
        console.print("[yellow]No baseline provided — treating empty graph as baseline.[/yellow]")
        from asil_drift.models import BaselineSnapshot

        baseline = BaselineSnapshot(repo_key=repo_key)

    detector = DriftDetector(graph_store=gstore)
    events = detector.detect(repo_key, baseline)

    if not events:
        console.print("[green]No drift detected.[/green]")
    else:
        dt = Table(title=f"drift report ({len(events)} events)", expand=True)
        dt.add_column("severity", no_wrap=True)
        dt.add_column("kind", no_wrap=True)
        dt.add_column("caller", overflow="fold")
        dt.add_column("callee", overflow="fold")
        dt.add_column("description", overflow="fold")

        severity_colors = {"critical": "bold red", "warning": "yellow", "info": "dim"}
        for ev in sorted(
            events, key=lambda e: {"critical": 0, "warning": 1, "info": 2}.get(e.severity, 3)
        ):
            color = severity_colors.get(ev.severity, "")
            dt.add_row(
                f"[{color}]{ev.severity}[/{color}]",
                ev.kind,
                ev.caller,
                ev.callee,
                ev.description,
            )
        console.print(dt)

    gstore.close()


# ---------------------------------------------------------------------------
# adapters commands (Phase 3 step 3+ — live ingestion from K8s / Prom / Loki)
# ---------------------------------------------------------------------------

adapters_app = typer.Typer(
    help="Poll live infrastructure adapters and merge events into the graph.",
    no_args_is_help=True,
)
app.add_typer(adapters_app, name="adapters")


@adapters_app.command("prometheus")
def adapters_prometheus(
    env: Annotated[str, typer.Option(help="env_key the events land under.")] = "prod",
    endpoint: Annotated[str, typer.Option(help="Prometheus base URL.")] = "",
    probe: Annotated[
        list[str] | None,
        typer.Option(
            "--probe",
            help=(
                "Repeatable. Format: 'service:metric:promql'. "
                "Example: --probe 'payments:p99_latency:histogram_quantile(...)'"
            ),
        ),
    ] = None,
    threshold: Annotated[float, typer.Option(help="Emit a MetricShift when ratio >= this.")] = 1.5,
    write: Annotated[
        bool, typer.Option(help="If true, MERGE results into Neo4j; else dry-run.")
    ] = False,
) -> None:
    """Poll Prometheus for the configured probes. Optionally writes the
    resulting MetricShift events into the runtime namespace.

    Defaults endpoint to `settings.prometheus_url` when --endpoint is empty.
    """
    import asyncio as _asyncio

    from asil_core import get_settings
    from asil_infra.adapters import NotConfiguredError, PrometheusAdapter

    configure_logging()
    settings = get_settings()
    url = endpoint or settings.prometheus_url
    probes: list[tuple[str, str, str]] = []
    for entry in probe or []:
        try:
            service, metric, promql = entry.split(":", 2)
        except ValueError:
            console.print(f"[red]bad --probe {entry!r}, expected svc:metric:promql[/red]")
            raise typer.Exit(code=1) from None
        probes.append((service, metric, promql))

    prom = PrometheusAdapter(url, probes=probes, shift_threshold=threshold)
    try:
        events = _asyncio.run(prom.poll(env))
    except NotConfiguredError as exc:
        console.print(f"[red]Prometheus unreachable: {exc}[/red]")
        raise typer.Exit(code=1) from None

    if not events:
        console.print(f"[dim]no shifts detected over {len(probes)} probe(s)[/dim]")
    else:
        t = Table(title=f"prometheus -> {len(events)} MetricShift(s)")
        t.add_column("service")
        t.add_column("metric")
        t.add_column("before", justify="right")
        t.add_column("after", justify="right")
        t.add_column("ratio", justify="right")
        for e in events:
            ratio = (e.after or 0) / (e.before or 1)
            t.add_row(
                e.service_name, e.metric, f"{e.before:.3f}", f"{e.after:.3f}", f"{ratio:.2f}x"
            )
        console.print(t)

    if write and events:
        _write_runtime_events(events)
        console.print(f"[green]wrote {len(events)} event(s) to graph[/green]")


@adapters_app.command("loki")
def adapters_loki(
    env: Annotated[str, typer.Option(help="env_key the events land under.")] = "prod",
    endpoint: Annotated[str, typer.Option(help="Loki base URL.")] = "",
    service: Annotated[
        list[str] | None,
        typer.Option("--service", help="Service to filter on (repeatable)."),
    ] = None,
    lookback: Annotated[int, typer.Option(help="Lookback window in seconds.")] = 300,
    level: Annotated[str, typer.Option(help="Log level regex to filter on.")] = "error",
    write: Annotated[bool, typer.Option(help="MERGE results into Neo4j.")] = False,
) -> None:
    """Poll Loki for recent error logs and emit one LogSignature per pattern."""
    import asyncio as _asyncio

    from asil_core import get_settings
    from asil_infra.adapters import LokiAdapter, NotConfiguredError

    configure_logging()
    settings = get_settings()
    url = endpoint or settings.loki_url

    loki = LokiAdapter(url, services=service or [], lookback_seconds=lookback, level_filter=level)
    try:
        events = _asyncio.run(loki.poll(env))
    except NotConfiguredError as exc:
        console.print(f"[red]Loki unreachable: {exc}[/red]")
        raise typer.Exit(code=1) from None

    if not events:
        console.print(f"[dim]no {level} log signatures in last {lookback}s[/dim]")
    else:
        t = Table(title=f"loki -> {len(events)} LogSignature(s)")
        t.add_column("service")
        t.add_column("signature", overflow="fold")
        t.add_column("count", justify="right")
        for e in events:
            t.add_row(e.service_name, e.signature[:80], str(e.count))
        console.print(t)

    if write and events:
        _write_runtime_events(events)
        console.print(f"[green]wrote {len(events)} event(s) to graph[/green]")


@adapters_app.command("k8s")
def adapters_k8s(
    env: Annotated[str, typer.Option(help="env_key the events land under.")] = "prod",
    kubeconfig: Annotated[
        str, typer.Option(help="Path to kubeconfig. Defaults to $KUBECONFIG / ~/.kube/config.")
    ] = "",
    namespace: Annotated[str, typer.Option(help="K8s namespace.")] = "default",
    write: Annotated[bool, typer.Option(help="MERGE results into Neo4j.")] = False,
) -> None:
    """Poll a Kubernetes cluster for Deployments + Services. Requires a
    reachable kubeconfig — no live cluster is provisioned by docker-compose."""
    import asyncio as _asyncio

    from asil_infra.adapters import K8sAdapter, NotConfiguredError

    configure_logging()
    k8s = K8sAdapter(kubeconfig=kubeconfig or None, namespace=namespace)
    try:
        events = _asyncio.run(k8s.poll(env))
    except NotConfiguredError as exc:
        console.print(f"[red]K8s skipped: {exc}[/red]")
        raise typer.Exit(code=1) from None

    if not events:
        console.print(f"[dim]no services / deployments in namespace {namespace}[/dim]")
    else:
        t = Table(title=f"k8s {namespace} -> {len(events)} event(s)")
        t.add_column("kind")
        t.add_column("name")
        for e in events:
            t.add_row(type(e).__name__, getattr(e, "name", getattr(e, "deployment_id", "?")))
        console.print(t)

    if write and events:
        _write_runtime_events(events)
        console.print(f"[green]wrote {len(events)} event(s) to graph[/green]")


def _write_runtime_events(events) -> None:
    """Dispatch a typed RuntimeEvent list to the right GraphStore method."""
    from asil_infra.models import Deployment, Incident, LogSignature, MetricShift, Service

    gstore = GraphStore()
    try:
        gstore.verify_connectivity()
        for e in events:
            props = e.model_dump(mode="json")
            if isinstance(e, Service):
                gstore.merge_service(props)
            elif isinstance(e, Deployment):
                gstore.merge_deployment(props)
            elif isinstance(e, MetricShift):
                gstore.merge_metric_shift(props)
            elif isinstance(e, LogSignature):
                gstore.merge_log_signature(props)
            elif isinstance(e, Incident):
                gstore.merge_incident(props, e.affected_services)
    finally:
        gstore.close()


# ---------------------------------------------------------------------------
# scan command — SonarQube-style CI entry point
# ---------------------------------------------------------------------------


@app.command()
def scan(
    repo: Annotated[str, typer.Option(help="Path to the repo to scan. Defaults to cwd.")] = ".",
    repo_key: Annotated[
        str, typer.Option(help="Graph repo key. Defaults to `local:<abspath>`.")
    ] = "",
    baseline: Annotated[
        str,
        typer.Option(
            help="Path to a drift baseline JSON (use `asil drift baseline` first).",
        ),
    ] = "",
    gate: Annotated[
        str,
        typer.Option(help="Quality gate: strict / normal / lenient / none."),
    ] = "normal",
    sarif: Annotated[
        str,
        typer.Option(help="Write SARIF 2.1.0 results to this path (for GitHub code scanning)."),
    ] = "",
    json_out: Annotated[
        str,
        typer.Option("--json", help="Write machine-readable JSON to this path."),
    ] = "",
    pr_comment_out: Annotated[
        str,
        typer.Option(
            "--pr-comment",
            help="Write a markdown PR comment to this path (or '-' for stdout).",
        ),
    ] = "",
    no_incidents: Annotated[
        bool,
        typer.Option(help="Skip the recent-incident causal-link signal."),
    ] = False,
    incident_lookback_hours: Annotated[
        int, typer.Option(help="How far back to look for recent incidents.")
    ] = 168,
    quiet: Annotated[
        bool, typer.Option(help="Suppress the human-readable terminal table.")
    ] = False,
) -> None:
    """Run every CI-grade check ASIL has on this repo. Emit results in
    JSON / SARIF / PR-comment formats. Exit code reflects the quality gate.

    Designed to be the single command CI runs on every PR — the
    SonarQube-style entry point. Cheap on purpose: only reads from the
    graph + saved baseline, never spins up the LLM.

    Exit codes:
      0  gate passed (or `--gate none`)
      1  gate failed — at least one finding above the gate's threshold
      2  ASIL itself crashed (graph unreachable, bad baseline file, ...)
    """
    import json as _json
    from pathlib import Path as _Path

    from asil_eval import run_scan, to_pr_comment, to_sarif

    configure_logging()
    rk = repo_key or f"local:{_Path(repo).resolve()}"

    try:
        report = run_scan(
            repo_root=repo,
            repo_key=rk,
            baseline_path=baseline or None,
            gate=gate,
            include_recent_incidents=not no_incidents,
            incident_lookback_hours=incident_lookback_hours,
        )
    except Exception as exc:
        console.print(f"[red]scan crashed: {exc}[/red]")
        raise typer.Exit(code=2) from None

    if not quiet:
        _render_scan_report(report)

    if json_out:
        _Path(json_out).write_text(
            _json.dumps(_scan_to_jsonable(report), indent=2),
            encoding="utf-8",
        )
        console.print(f"[dim]wrote JSON to {json_out}[/dim]")

    if sarif:
        _Path(sarif).write_text(
            _json.dumps(to_sarif(report), indent=2),
            encoding="utf-8",
        )
        console.print(f"[dim]wrote SARIF to {sarif}[/dim]")

    if pr_comment_out:
        md = to_pr_comment(report)
        if pr_comment_out == "-":
            console.print(md)
        else:
            _Path(pr_comment_out).write_text(md, encoding="utf-8")
            console.print(f"[dim]wrote PR comment to {pr_comment_out}[/dim]")

    if not report.passed_gate:
        raise typer.Exit(code=1)


def _render_scan_report(report) -> None:
    counts = report.counts
    status = "[green]passed[/green]" if report.passed_gate else "[red]failed[/red]"
    console.print(
        f"\n[bold]asil scan[/bold] · repo [cyan]{report.repo_key}[/cyan] · "
        f"gate [bold]{report.gate}[/bold] · {status}"
    )
    console.print(
        f"  critical={counts['critical']}  error={counts['error']}  "
        f"warning={counts['warning']}  note={counts['note']}  "
        f"({len(report.findings)} total, {report.duration_seconds:.2f}s)"
    )

    if not report.findings:
        console.print("[dim]no findings — clean scan.[/dim]")
        return

    t = Table(title="findings", expand=True)
    t.add_column("sev", no_wrap=True)
    t.add_column("rule", no_wrap=True)
    t.add_column("message", overflow="fold")
    t.add_column("file", no_wrap=True)
    color = {
        "critical": "bold red",
        "error": "red",
        "warning": "yellow",
        "note": "dim",
    }
    for f in report.findings:
        t.add_row(
            f"[{color[f.severity.value]}]{f.severity.value}[/{color[f.severity.value]}]",
            f.rule_id,
            f.message,
            f.file_path or "—",
        )
    console.print(t)


def _scan_to_jsonable(report) -> dict[str, object]:
    return {
        "repo_root": report.repo_root,
        "repo_key": report.repo_key,
        "started_at": report.started_at.isoformat(),
        "duration_seconds": report.duration_seconds,
        "gate": report.gate,
        "passed_gate": report.passed_gate,
        "counts": report.counts,
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "message": f.message,
                "file_path": f.file_path,
                "line": f.line,
                "derivation": f.derivation,
                "extra": f.extra,
            }
            for f in report.findings
        ],
    }


# ---------------------------------------------------------------------------
# fix commands (Phase 8 — constrained autonomous fix pipeline)
# ---------------------------------------------------------------------------

fix_app = typer.Typer(
    help="Phase 8 — propose a patch from a causal chain; optionally run it in a sandbox.",
    no_args_is_help=True,
)
app.add_typer(fix_app, name="fix")


@fix_app.command("propose")
def fix_propose(
    incident_id: Annotated[
        str, typer.Argument(help="Incident ID (e.g. INC-2026-04-12-payments-cascade).")
    ],
    repo: Annotated[str, typer.Option(help="Path to the repo. Defaults to cwd.")] = ".",
    repo_key: Annotated[
        str, typer.Option(help="Repo key for graph scoping. Inferred from path if omitted.")
    ] = "",
    record: Annotated[
        bool,
        typer.Option(help="Persist proposal to the audit log even though sandbox didn't run."),
    ] = False,
) -> None:
    """Generate a fix proposal from an incident's causal chain. Read-only —
    does NOT apply the diff or run any tests. Use `asil fix run` for that.

    Output: the proposed unified diff plus a confidence breakdown."""
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from asil_core.llm import ModelRouter
    from asil_fix import NoOpSandbox, PatchGenerator
    from asil_fix.audit import from_settings_or_none as _audit_or_none

    configure_logging()

    rk = repo_key or f"local:{_Path(repo).resolve()}"
    gstore = GraphStore()
    try:
        gstore.verify_connectivity()
    except Exception as exc:
        console.print(f"[red]graph unreachable: {exc}[/red]")
        raise typer.Exit(code=1) from None

    try:
        router = ModelRouter.from_env()
        generator = PatchGenerator(router=router, graph_store=gstore)
        try:
            proposal = _asyncio.run(
                generator.propose(incident_id=incident_id, repo_root=repo, repo_key=rk)
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

        _render_proposal(proposal)

        if record:
            sandbox = NoOpSandbox()
            sandbox_result = sandbox.run(proposal, repo)
            audit = _audit_or_none()
            if audit is None:
                console.print("[yellow]audit log unavailable; skipping persistence[/yellow]")
            else:
                outcome = audit.record(proposal, sandbox_result)
                console.print(f"[green]audited as {outcome.value}[/green]")
    finally:
        gstore.close()


@fix_app.command("run")
def fix_run(
    incident_id: Annotated[str, typer.Argument()],
    repo: Annotated[str, typer.Option(help="Path to the repo. Defaults to cwd.")] = ".",
    repo_key: Annotated[str, typer.Option(help="Repo key for graph scoping.")] = "",
    test_command: Annotated[
        str, typer.Option(help="Shell command run inside the sandbox after applying the diff.")
    ] = "make test",
    timeout: Annotated[int, typer.Option(help="Sandbox wall-clock timeout (seconds).")] = 300,
    confidence_gate: Annotated[
        float,
        typer.Option(help="Minimum proposal confidence to land outcome=accepted on tests-passed."),
    ] = 0.6,
) -> None:
    """Full pipeline: propose -> sandbox apply -> run tests -> audit.

    Never pushes, never merges. The diff + sandbox stdout/stderr land in
    the audit log; a human (or the future Phase 8 dashboard) decides what
    happens next.
    """
    import asyncio as _asyncio
    from pathlib import Path as _Path

    from asil_core.llm import ModelRouter
    from asil_fix import LocalSandbox, PatchGenerator
    from asil_fix.audit import from_settings_or_none as _audit_or_none

    configure_logging()
    rk = repo_key or f"local:{_Path(repo).resolve()}"

    gstore = GraphStore()
    try:
        gstore.verify_connectivity()
    except Exception as exc:
        console.print(f"[red]graph unreachable: {exc}[/red]")
        raise typer.Exit(code=1) from None

    try:
        router = ModelRouter.from_env()
        generator = PatchGenerator(router=router, graph_store=gstore)
        try:
            proposal = _asyncio.run(
                generator.propose(incident_id=incident_id, repo_root=repo, repo_key=rk)
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

        _render_proposal(proposal)

        console.print()
        console.print(f"[bold]Sandboxing[/bold] `{test_command}` (timeout {timeout}s) ...")
        sandbox = LocalSandbox(test_command=test_command, timeout_seconds=timeout)
        result = sandbox.run(proposal, repo)
        _render_sandbox(result)

        audit = _audit_or_none()
        if audit is None:
            console.print("[yellow]audit log unavailable; outcome not persisted[/yellow]")
        else:
            outcome = audit.record(proposal, result, confidence_gate=confidence_gate)
            console.print(f"\n[bold]outcome:[/bold] {outcome.value}")
    finally:
        gstore.close()


@fix_app.command("list")
def fix_list(
    incident_id: Annotated[str, typer.Option(help="Restrict to one incident.")] = "",
    limit: Annotated[int, typer.Option(help="Row cap.")] = 20,
) -> None:
    """Show recent fix proposals from the audit log."""
    from asil_fix.audit import from_settings_or_none as _audit_or_none

    audit = _audit_or_none()
    if audit is None:
        console.print("[red]audit log unavailable (postgres unreachable)[/red]")
        raise typer.Exit(code=1)

    entries = (
        audit.list_for_incident(incident_id, limit=limit)
        if incident_id
        else audit.recent(limit=limit)
    )
    if not entries:
        console.print("[dim]no fix proposals recorded yet[/dim]")
        return

    t = Table(title=f"fix audit ({len(entries)} row(s))")
    t.add_column("ts", no_wrap=True)
    t.add_column("incident")
    t.add_column("outcome")
    t.add_column("sandbox")
    t.add_column("conf", justify="right")
    t.add_column("files")
    t.add_column("cost", justify="right")
    for e in entries:
        t.add_row(
            e.ts.strftime("%Y-%m-%d %H:%M:%SZ"),
            e.incident_id,
            e.outcome.value,
            e.sandbox_outcome or "—",
            f"{e.confidence_score:.2f}",
            ", ".join(e.affected_files[:2]) + ("..." if len(e.affected_files) > 2 else ""),
            f"${e.cost_usd:.4f}",
        )
    console.print(t)


def _render_proposal(proposal) -> None:
    """Pretty-print a FixProposal to the console (CLI-only helper)."""
    h = Table.grid(padding=(0, 1))
    h.add_column(justify="right", style="bold")
    h.add_column()
    h.add_row("incident", proposal.incident_id)
    h.add_row("summary", proposal.summary)
    h.add_row("confidence", f"{proposal.confidence_score:.3f}")
    h.add_row("affected files", ", ".join(proposal.affected_files) or "—")
    h.add_row("model", proposal.model)
    h.add_row("cost", f"${proposal.cost_usd:.6f}")
    console.print(h)
    console.print()
    console.print("[bold]derivation[/bold]")
    for line in proposal.derivation:
        console.print(f"  • {line}")
    console.print()
    console.print("[bold]proposed diff[/bold]")
    console.print(proposal.diff or "[red](no diff parsed from LLM response)[/red]")


def _render_sandbox(result) -> None:
    """Pretty-print a SandboxResult."""
    color = "green" if result.passed else "red"
    console.print(
        f"[bold {color}]sandbox: {result.outcome.value}[/bold {color}] "
        f"({result.duration_seconds:.2f}s)"
    )
    if result.test_command:
        console.print(f"  cmd: [dim]{result.test_command}[/dim]")
    if result.stdout_tail:
        console.print("\n[bold]stdout tail[/bold]")
        console.print(f"[dim]{result.stdout_tail}[/dim]")
    if result.stderr_tail:
        console.print("\n[bold]stderr tail[/bold]")
        console.print(f"[dim]{result.stderr_tail}[/dim]")
    for note in result.notes:
        console.print(f"  ! {note}")


# ---------------------------------------------------------------------------
# external commands — GitHub PRs, Slack channels, Jira / Linear tickets
# ---------------------------------------------------------------------------

external_app = typer.Typer(
    help="External-system adapters: GitHub PRs, Slack messages, Jira / Linear tickets.",
    no_args_is_help=True,
)
app.add_typer(external_app, name="external")


@external_app.command("github")
def external_github(
    repo: Annotated[str, typer.Argument(help="Path to a local git repo (defaults to cwd).")] = ".",
    limit: Annotated[int, typer.Option(help="Max PRs to fetch.")] = 50,
    since_days: Annotated[int, typer.Option(help="Look back this many days.")] = 30,
    write: Annotated[bool, typer.Option(help="MERGE PRs into Neo4j.")] = False,
) -> None:
    """Ingest GitHub pull requests from a local repo. Uses `gh` CLI when
    available; falls back to parsing merge commits from `git log`."""
    import asyncio as _asyncio

    from asil_infra.adapters import NotConfiguredError
    from asil_infra.external import GitHubAdapter

    configure_logging()
    adapter = GitHubAdapter(repo, limit=limit, since_days=since_days)
    try:
        prs = _asyncio.run(adapter.poll())
    except NotConfiguredError as exc:
        console.print(f"[red]github skipped: {exc}[/red]")
        raise typer.Exit(code=1) from None

    if not prs:
        console.print(f"[dim]no PRs found in last {since_days} days[/dim]")
        return

    t = Table(title=f"github -> {len(prs)} PR(s)", expand=True)
    t.add_column("#", justify="right")
    t.add_column("state", no_wrap=True)
    t.add_column("title", overflow="fold")
    t.add_column("author")
    t.add_column("merged sha")
    for pr in prs:
        t.add_row(
            str(pr.number),
            pr.state,
            pr.title,
            pr.author or "—",
            (pr.merge_commit_sha or "—")[:8],
        )
    console.print(t)

    if write:
        gstore = GraphStore()
        try:
            gstore.verify_connectivity()
            for pr in prs:
                gstore.merge_pull_request(pr.model_dump(mode="json"))
        finally:
            gstore.close()
        console.print(f"[green]wrote {len(prs)} PR(s) to graph[/green]")


@external_app.command("slack")
def external_slack(
    channel: Annotated[list[str], typer.Option("--channel", help="Slack channel ID (repeatable).")],
    lookback_hours: Annotated[int, typer.Option(help="Lookback window in hours.")] = 24,
    service: Annotated[
        list[str] | None,
        typer.Option("--service", help="Known service names to extract mentions of (repeatable)."),
    ] = None,
    write: Annotated[bool, typer.Option(help="MERGE messages into Neo4j.")] = False,
) -> None:
    """Ingest recent Slack messages. Requires SLACK_BOT_TOKEN env var."""
    import asyncio as _asyncio

    from asil_infra.adapters import NotConfiguredError
    from asil_infra.external import SlackAdapter

    configure_logging()
    adapter = SlackAdapter(
        channels=channel,
        lookback_seconds=lookback_hours * 3600,
        known_services=service or [],
    )
    try:
        msgs = _asyncio.run(adapter.poll())
    except NotConfiguredError as exc:
        console.print(f"[red]slack skipped: {exc}[/red]")
        raise typer.Exit(code=1) from None

    if not msgs:
        console.print(f"[dim]no messages in last {lookback_hours}h[/dim]")
        return

    t = Table(title=f"slack -> {len(msgs)} message(s)")
    t.add_column("channel")
    t.add_column("text", overflow="fold")
    t.add_column("incidents")
    t.add_column("services")
    for m in msgs:
        t.add_row(
            m.channel,
            m.text[:80],
            ", ".join(m.incident_ids) or "—",
            ", ".join(m.service_names) or "—",
        )
    console.print(t)

    if write:
        gstore = GraphStore()
        try:
            gstore.verify_connectivity()
            for m in msgs:
                gstore.merge_chat_message(m.model_dump(mode="json"))
        finally:
            gstore.close()
        console.print(f"[green]wrote {len(msgs)} message(s) to graph[/green]")


@external_app.command("jira")
def external_jira(
    project: Annotated[list[str], typer.Option("--project", help="Jira project key (repeatable).")],
    lookback_hours: Annotated[int, typer.Option(help="Lookback window in hours.")] = 24,
    write: Annotated[bool, typer.Option(help="MERGE tickets into Neo4j.")] = False,
) -> None:
    """Ingest recently-updated Jira tickets. Requires JIRA_BASE_URL,
    JIRA_USER_EMAIL, JIRA_API_TOKEN env vars."""
    import asyncio as _asyncio

    from asil_infra.adapters import NotConfiguredError
    from asil_infra.external import JiraAdapter

    configure_logging()
    adapter = JiraAdapter(projects=project, lookback_seconds=lookback_hours * 3600)
    try:
        tickets = _asyncio.run(adapter.poll())
    except NotConfiguredError as exc:
        console.print(f"[red]jira skipped: {exc}[/red]")
        raise typer.Exit(code=1) from None

    _render_tickets(tickets, "jira")

    if write and tickets:
        gstore = GraphStore()
        try:
            gstore.verify_connectivity()
            for t in tickets:
                gstore.merge_ticket(t.model_dump(mode="json"))
        finally:
            gstore.close()
        console.print(f"[green]wrote {len(tickets)} ticket(s) to graph[/green]")


@external_app.command("linear")
def external_linear(
    team: Annotated[list[str], typer.Option("--team", help="Linear team key (repeatable).")],
    limit: Annotated[int, typer.Option(help="Max tickets to fetch.")] = 100,
    write: Annotated[bool, typer.Option(help="MERGE tickets into Neo4j.")] = False,
) -> None:
    """Ingest recently-updated Linear tickets. Requires LINEAR_API_KEY env var."""
    import asyncio as _asyncio

    from asil_infra.adapters import NotConfiguredError
    from asil_infra.external import LinearAdapter

    configure_logging()
    adapter = LinearAdapter(teams=team, limit=limit)
    try:
        tickets = _asyncio.run(adapter.poll())
    except NotConfiguredError as exc:
        console.print(f"[red]linear skipped: {exc}[/red]")
        raise typer.Exit(code=1) from None

    _render_tickets(tickets, "linear")

    if write and tickets:
        gstore = GraphStore()
        try:
            gstore.verify_connectivity()
            for t in tickets:
                gstore.merge_ticket(t.model_dump(mode="json"))
        finally:
            gstore.close()
        console.print(f"[green]wrote {len(tickets)} ticket(s) to graph[/green]")


def _render_tickets(tickets, provider: str) -> None:
    if not tickets:
        console.print(f"[dim]{provider}: no tickets[/dim]")
        return
    t = Table(title=f"{provider} -> {len(tickets)} ticket(s)")
    t.add_column("key", no_wrap=True)
    t.add_column("status")
    t.add_column("title", overflow="fold")
    t.add_column("assignee")
    t.add_column("incidents")
    for ticket in tickets:
        t.add_row(
            ticket.key,
            ticket.status,
            ticket.title[:80],
            ticket.assignee or "—",
            ", ".join(ticket.incident_ids) or "—",
        )
    console.print(t)


# ---------------------------------------------------------------------------
# cost commands (Phase 7 — LLM cost ledger + savings visualisation)
# ---------------------------------------------------------------------------

cost_app = typer.Typer(help="LLM cost ledger + savings calculator.", no_args_is_help=True)
app.add_typer(cost_app, name="cost")


@cost_app.command("summary")
def cost_summary(
    days: Annotated[int, typer.Option(help="Window size in days for aggregations.")] = 30,
) -> None:
    """Show total LLM spend, breakdown by provider/tier, and the savings
    estimate from episodic memory. Falls back gracefully if Postgres isn't
    reachable — explains what's missing instead of crashing."""
    configure_logging()
    from asil_core.llm.postgres_ledger import from_settings_or_none

    ledger = from_settings_or_none()
    if ledger is None:
        console.print(
            "[red]Postgres unreachable — no persistent cost ledger.[/red]\n"
            "[dim]Run `make up` to start the docker stack, then retry.[/dim]"
        )
        raise typer.Exit(code=1)

    agg = ledger.aggregates(days=days)

    header = Table(title=f"LLM spend, last {days} days", expand=False)
    header.add_column("metric", no_wrap=True)
    header.add_column("value", no_wrap=True, justify="right")
    header.add_row("total spent", f"${agg.total_usd:.4f}")
    header.add_row("# of LLM calls", str(agg.calls))
    if agg.calls > 0:
        header.add_row("avg / call", f"${agg.total_usd / agg.calls:.6f}")
    console.print(header)
    console.print()

    if agg.by_provider:
        bp = Table(title="by provider", expand=False)
        bp.add_column("provider", no_wrap=True)
        bp.add_column("cost", justify="right")
        for prov, cost in agg.by_provider.items():
            bp.add_row(prov, f"${cost:.4f}")
        console.print(bp)
        console.print()

    if agg.by_tier:
        bt = Table(title="by tier", expand=False)
        bt.add_column("tier", no_wrap=True)
        bt.add_column("cost", justify="right")
        for tier, cost in agg.by_tier.items():
            bt.add_row(tier, f"${cost:.4f}")
        console.print(bt)
        console.print()

    # Savings: pull memory count from the EpisodicStore.
    estore = _open_episodic_or_exit()
    try:
        memory_count = estore.count()
    finally:
        estore.close()

    savings = ledger.savings_vs_no_memory(memory_count, days=days)
    sv = Table(title=f"episodic memory savings (last {days} days)", expand=False)
    sv.add_column("metric", no_wrap=True)
    sv.add_column("value", no_wrap=True, justify="right")
    sv.add_row("memories stored", str(savings["memory_conclusions"]))
    sv.add_row("cache hits", str(savings["cache_hits"]))
    sv.add_row("avg fresh ask $", f"${savings['avg_fresh_usd']:.6f}")
    sv.add_row("avg cached ask $", f"${savings['avg_cached_usd']:.6f}")
    sv.add_row("saved", f"${savings['saved_usd']:.4f}")
    if savings["savings_pct"] is not None:
        sv.add_row("savings %", f"{savings['savings_pct']:.2f}%")
    else:
        sv.add_row("savings %", "(no hits yet)")
    console.print(sv)
    if not savings["measured"]:
        console.print(f"[dim]{savings['note']}[/dim]")


@cost_app.command("daily")
def cost_daily(
    days: Annotated[int, typer.Option(help="Number of days to show.")] = 14,
) -> None:
    """Daily LLM spend as a sparkline-ish text chart. Useful for blog
    screenshots and trend spotting."""
    configure_logging()
    from asil_core.llm.postgres_ledger import from_settings_or_none

    ledger = from_settings_or_none()
    if ledger is None:
        console.print("[red]Postgres unreachable.[/red]")
        raise typer.Exit(code=1)

    agg = ledger.aggregates(days=days)
    if not agg.by_day:
        console.print(f"[yellow]No LLM activity in the last {days} days.[/yellow]")
        return

    max_cost = max(c for _, c in agg.by_day) or 1.0
    t = Table(title=f"daily spend, last {days} days", expand=False)
    t.add_column("day", no_wrap=True)
    t.add_column("$ cost", justify="right")
    t.add_column("bar", overflow="fold")
    for day, cost in agg.by_day:
        bars = int((cost / max_cost) * 30)
        t.add_row(day, f"${cost:.4f}", "█" * bars)
    console.print(t)


# ---------------------------------------------------------------------------
# context export/import (Phase 9.7 — two-command cross-IDE handoff)
# ---------------------------------------------------------------------------

context_app = typer.Typer(
    help="Export this Claude Code session into ASIL, or print import wiring for the next IDE.",
    no_args_is_help=True,
)
app.add_typer(context_app, name="context")


def _encoded_cwd_dirname(cwd: Path) -> str:
    """Mirror Claude Code's path-encoding so we can find the project
    directory under ~/.claude/projects/ from any cwd."""
    return "-" + str(cwd).replace("/", "-").lstrip("-")


@context_app.command("export")
def context_export(
    since: Annotated[
        str,
        typer.Option(
            "--since",
            help="Time window for transcripts (e.g. '2h', '1d'). Larger = more context, slower.",
        ),
    ] = "2h",
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help="Project directory to look up (default: current working dir).",
        ),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="ALSO write a portable markdown bundle here (optional; for offline handoff).",
        ),
    ] = None,
    user_id: Annotated[str | None, typer.Option("--user-id")] = None,
) -> None:
    """One-command export of the current Claude Code session into ASIL's
    episodic memory. Auto-detects the current cwd, finds the matching
    transcript under `~/.claude/projects/`, ingests every Q/A pair (with
    tool calls + final task lists). Dedupe handles re-runs.

    Optional `--file` also writes a portable markdown bundle you can paste
    into agents that don't speak MCP."""
    configure_logging()
    from asil_ingest_agents import ClaudeCodeIngester
    from asil_ingest_agents.claude_code import CLAUDE_PROJECTS_DIR

    target_cwd = (cwd or Path.cwd()).resolve()
    encoded = _encoded_cwd_dirname(target_cwd)
    proj_dir = CLAUDE_PROJECTS_DIR / encoded
    if not proj_dir.exists():
        console.print(
            f"[red]No Claude Code transcripts found for {target_cwd}.[/red]\n"
            f"[dim]Looked at: {proj_dir}[/dim]"
        )
        raise typer.Exit(code=1)

    since_dt = _parse_relative_window(since)
    if since_dt is None:
        console.print(f"[red]Could not parse --since {since!r}[/red]")
        raise typer.Exit(code=2)

    ingester = ClaudeCodeIngester()
    plan = ingester.plan(since=since_dt, project=str(target_cwd))
    if not plan.qa_chunks:
        console.print(f"[yellow]No new Q/A pairs in the last {since} for {target_cwd}.[/yellow]")
        return

    console.print(
        f"[bold]Exporting {len(plan.qa_chunks)} Q/A pair(s) from "
        f"{len(plan.sessions)} session(s) into ASIL...[/bold]"
    )
    _write_plan_to_memory(
        plan,
        origin_agent="claude-code",
        repo_key_override=f"local:{target_cwd}",
        user_id=user_id,
    )

    if file is not None:
        _write_portable_bundle(plan, file, cwd=target_cwd)
        console.print(f"[green]Portable bundle: {file}[/green]")

    console.print(
        "\n[bold]Next:[/bold] In another IDE/agent, run "
        "[cyan]asil context import <target>[/cyan] to wire it up."
    )


@context_app.command("import")
def context_import(
    target: Annotated[
        str,
        typer.Argument(
            help="Where to import: 'claude-code', 'cursor', 'aider', 'mcp' (any client), or 'prompt'.",
        ),
    ],
    about: Annotated[
        str | None,
        typer.Option(
            "--about",
            help="(prompt only) Topic to scope the recall query. Default: most-recalled memories.",
        ),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", help="(prompt) max prior conclusions to surface.")
    ] = 10,
) -> None:
    """Emit the import wiring for the chosen target.

    For MCP-aware clients (claude-code, cursor, aider, mcp): prints the
    config snippet you paste into their settings.

    For 'prompt': queries ASIL's memory and prints a paste-able markdown
    system prompt summarising prior conclusions — works with any agent
    on any LLM provider, even ones that don't speak MCP."""
    configure_logging()
    target_low = target.lower()
    if target_low in ("mcp", "claude-code", "cursor", "aider", "openhands"):
        _print_mcp_wiring(target_low)
        return
    if target_low == "prompt":
        _print_prompt_bundle(about=about, limit=limit)
        return
    console.print(
        f"[red]Unknown target: {target!r}. Try one of: mcp, claude-code, cursor, aider, openhands, prompt.[/red]"
    )
    raise typer.Exit(code=2)


def _print_mcp_wiring(target: str) -> None:
    mcp_url = "http://localhost:8000/mcp"
    snippets = {
        "claude-code": (
            "Add to [bold]~/.claude/settings.json[/bold]:\n\n"
            "[cyan]"
            "{\n"
            '  "mcpServers": {\n'
            '    "asil": {\n'
            '      "type": "http",\n'
            f'      "url": "{mcp_url}"\n'
            "    }\n"
            "  }\n"
            "}\n"
            "[/cyan]"
            "\nRestart Claude Code. Tools appear as [cyan]mcp__asil__*[/cyan]."
        ),
        "cursor": (
            "Add to [bold]~/.cursor/mcp.json[/bold] (create if missing):\n\n"
            "[cyan]"
            "{\n"
            '  "mcpServers": {\n'
            '    "asil": {\n'
            f'      "url": "{mcp_url}",\n'
            '      "transport": "http"\n'
            "    }\n"
            "  }\n"
            "}\n"
            "[/cyan]"
            "\nReload Cursor. Open the MCP panel to confirm 'asil' shows as connected."
        ),
        "aider": (
            "Aider's MCP support is via the OpenAI-style tool surface. Easiest path:\n"
            "set [bold]ASIL_MCP_URL[/bold] in env and use the asil-mcp-client wrapper "
            "(or, until Aider ships native MCP, use [cyan]asil context import prompt[/cyan] "
            "to get a paste-able context block)."
        ),
        "openhands": (
            "OpenHands reads MCP config from [bold]config.toml[/bold]:\n\n"
            "[cyan][mcp_servers.asil]\n"
            f'type = "http"\nurl = "{mcp_url}"[/cyan]\n\n'
            "Restart OpenHands."
        ),
        "mcp": (
            f"Any MCP HTTP client: point at [cyan]{mcp_url}[/cyan]\n"
            "Tools list: [cyan]GET /mcp/tools[/cyan]\n"
            'Call a tool: [cyan]POST /mcp/call/<tool> {"arguments": {...}}[/cyan]'
        ),
    }
    console.print(
        Panel(snippets[target], title=f"Import context into {target}", border_style="cyan")
    )
    console.print(
        "\n[dim]Authorization: if ASIL_AUTH_DISABLE is unset on the server, "
        "the client also needs:\n  Authorization: Bearer <team-api-key>\n"
        "Create a key with `asil team create <id>`.[/dim]"
    )


def _print_prompt_bundle(*, about: str | None, limit: int) -> None:
    """For non-MCP agents: query ASIL and emit a paste-able context block."""
    estore = _open_episodic_or_exit()
    try:
        if about:
            router = ModelRouter.from_env()

            async def _embed() -> list[float]:
                return (await router.embed([about]))[0]

            vec = asyncio.run(_embed())
            hits = estore.recall_similar(query_vector=vec, limit=limit, min_similarity=0.3)
            memories = [h.memory for h in hits]
            header = f"prior conclusions related to: {about!r}"
        else:
            memories = estore.top_recalled(limit=limit)
            header = "top recalled prior conclusions"
    finally:
        estore.close()

    if not memories:
        console.print("[yellow]No memories to import. Run `asil context export` first.[/yellow]")
        return

    out = ["# Context recalled from ASIL", "", f"_{header}_", ""]
    for i, m in enumerate(memories, 1):
        when = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "?"
        out.append(f"## {i}. {m.question}")
        out.append("")
        out.append(
            f"_Originally answered {when} via {m.origin_agent} (recall_hits={m.recall_hits})_"
        )
        out.append("")
        excerpt = m.answer if len(m.answer) <= 1500 else m.answer[:1500] + "\n…[truncated]"
        out.append(excerpt)
        out.append("")
        out.append("---")
        out.append("")
    blob = "\n".join(out)
    # Print to stdout so the user can pipe to pbcopy / a file.
    print(blob)


def _write_portable_bundle(plan, file: Path, *, cwd: Path) -> None:
    """Write the IngestPlan as a self-contained markdown bundle. Useful
    for offline handoff (email, gist, paste into a non-MCP agent)."""
    lines = [
        f"# ASIL context bundle — {cwd}",
        "",
        f"Exported {datetime.now().isoformat(timespec='seconds')} from Claude Code transcripts.",
        f"Sessions: {len(plan.sessions)}, Q/A pairs: {len(plan.qa_chunks)}",
        "",
    ]
    for i, c in enumerate(plan.qa_chunks, 1):
        when = c.start_ts.strftime("%Y-%m-%d %H:%M") if c.start_ts else "?"
        lines.append(f"## {i}. {c.question}")
        lines.append("")
        lines.append(f"_{when} — session {c.session_id[:8]}_")
        lines.append("")
        lines.append(c.assistant_response)
        lines.append("")
        lines.append("---")
        lines.append("")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# teams (Phase 9.5 — multi-team auth)
# ---------------------------------------------------------------------------

team_app = typer.Typer(
    help="Manage teams + API keys for multi-team memory sharing.",
    no_args_is_help=True,
)
app.add_typer(team_app, name="team")


def _open_teams_store():
    from asil_memory import TeamsStore

    settings = get_settings()
    store = TeamsStore(settings.postgres_dsn)
    try:
        store.apply_schema()
    except Exception as e:
        console.print(f"[red]Postgres unreachable: {e}[/red]")
        raise typer.Exit(code=2) from None
    return store


@team_app.command("create")
def team_create(
    team_id: Annotated[str, typer.Argument(help="Stable alphanumeric ID (e.g. 'startup-dev').")],
    name: Annotated[str, typer.Option("--name", help="Human-readable team name.")] = "",
) -> None:
    """Create a new team + mint its first API key. The raw key is shown
    once — store it in 1Password / your secret manager immediately."""
    configure_logging()
    store = _open_teams_store()
    try:
        result = store.create_team(team_id=team_id, name=name or team_id)
    except Exception as e:
        console.print(f"[red]create failed: {e}[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        Panel(
            f"[bold]Team created:[/bold] {result.team.name} (id={result.team.id})\n\n"
            f"[bold yellow]API key (shown once, store it now):[/bold yellow]\n"
            f"  [cyan]{result.api_key}[/cyan]\n\n"
            f"Configure clients with:\n"
            f"  export ASIL_TEAM_API_KEY={result.api_key}\n"
            f"  curl ... -H 'Authorization: Bearer {result.api_key}'",
            title="store this key",
            border_style="yellow",
        )
    )


@team_app.command("list")
def team_list() -> None:
    """List all teams + their status."""
    configure_logging()
    store = _open_teams_store()
    teams = store.list_teams()
    if not teams:
        console.print("[dim]No teams yet. Create one with `asil team create <id>`.[/dim]")
        return
    t = Table(title="teams", expand=False)
    t.add_column("id", no_wrap=True)
    t.add_column("name")
    t.add_column("status", no_wrap=True)
    t.add_column("created", no_wrap=True)
    for team in teams:
        status = "[red]revoked[/red]" if team.revoked_at else "[green]active[/green]"
        t.add_row(team.id, team.name, status, team.created_at.strftime("%Y-%m-%d %H:%M"))
    console.print(t)


@team_app.command("rotate-key")
def team_rotate_key(
    team_id: Annotated[str, typer.Argument(help="Team ID to rotate.")],
) -> None:
    """Mint a new key; the old one stops working immediately."""
    configure_logging()
    store = _open_teams_store()
    try:
        result = store.rotate_key(team_id=team_id)
    except KeyError:
        console.print(f"[red]no such team: {team_id!r}[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        Panel(
            f"[bold]Key rotated for {result.team.id}[/bold]\n\n"
            f"[yellow]New API key (shown once):[/yellow]\n  [cyan]{result.api_key}[/cyan]",
            title="rotated",
            border_style="yellow",
        )
    )


@team_app.command("revoke")
def team_revoke(
    team_id: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes/--no", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Mark a team revoked. All requests with its key start returning 401."""
    configure_logging()
    if not yes and not typer.confirm(f"revoke team {team_id!r}? this disables its key."):
        raise typer.Exit(code=1)
    store = _open_teams_store()
    removed = store.revoke(team_id=team_id)
    if removed:
        console.print(f"[green]revoked {team_id}[/green]")
    else:
        console.print("[yellow]nothing to revoke (team missing or already revoked)[/yellow]")


# ---------------------------------------------------------------------------
# transcript ingestion (Phase 9.3 — cross-agent memory)
# ---------------------------------------------------------------------------

ingest_transcripts_app = typer.Typer(
    help="Ingest transcripts from local coding-agent sessions into episodic memory.",
    no_args_is_help=True,
)
app.add_typer(ingest_transcripts_app, name="ingest-transcripts")


@ingest_transcripts_app.command("claude-code")
def ingest_claude_code(
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Only include sessions modified within this window (e.g. '1h', '2d', '15m'). Omit to include all.",
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            help="Substring filter against the decoded project path (e.g. '/ASIL').",
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="One specific session UUID."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be ingested without writing."),
    ] = False,
    repo_key: Annotated[
        str | None,
        typer.Option(
            "--repo-key",
            help="Override the repo_key used on the resulting memories (default: the session's cwd).",
        ),
    ] = None,
    user_id: Annotated[
        str | None,
        typer.Option(
            "--user-id",
            help="Override the user_id stamped on the memories (default: asil_core.identity.get_user_id()).",
        ),
    ] = None,
) -> None:
    """Read Claude Code's local JSONL transcripts and ingest each user
    question + assistant answer pair as an episodic memory.

    Once ingested, the same questions asked via MCP from any other agent
    (Cursor, OpenHands, Aider, ...) will short-circuit on the cache hit
    and render an "answered via claude-code on YYYY-MM-DD" preamble."""
    configure_logging()
    from asil_ingest_agents import ClaudeCodeIngester

    since_dt: datetime | None = None
    if since:
        since_dt = _parse_relative_window(since)
        if since_dt is None:
            console.print(
                f"[red]could not parse --since {since!r}; use e.g. '1h', '2d', '15m'.[/red]"
            )
            raise typer.Exit(code=2)

    ingester = ClaudeCodeIngester()
    plan = ingester.plan(since=since_dt, project=project, session=session)

    console.print(
        f"[bold]Claude Code transcripts:[/bold] "
        f"{len(plan.sessions)} session(s) → {len(plan.qa_chunks)} Q/A chunks"
    )
    if not plan.qa_chunks:
        console.print("[dim]Nothing to ingest. Try widening --since or omitting it.[/dim]")
        return

    preview = Table(title="first 5 chunks", expand=True)
    preview.add_column("session", no_wrap=True)
    preview.add_column("when", no_wrap=True)
    preview.add_column("question", overflow="fold")
    for c in plan.qa_chunks[:5]:
        preview.add_row(
            c.session_id[:8],
            c.start_ts.strftime("%Y-%m-%d %H:%M") if c.start_ts else "?",
            c.question[:140],
        )
    console.print(preview)

    if dry_run:
        console.print("[yellow]--dry-run: no memories written. Drop the flag to ingest.[/yellow]")
        return

    _write_plan_to_memory(
        plan,
        origin_agent="claude-code",
        repo_key_override=repo_key,
        user_id=user_id,
    )


def _write_plan_to_memory(
    plan,
    *,
    origin_agent: str,
    repo_key_override: str | None = None,
    user_id: str | None = None,
) -> tuple[int, int, list[str]]:
    """Drain an `IngestPlan` into episodic memory. Returns (inserted,
    folded, errors). Shared by every `asil ingest-transcripts <agent>`
    command so the write semantics stay consistent across parsers."""
    from asil_core import Confidence as _Confidence

    estore = _open_episodic_or_exit()
    estore.apply_schema()
    router = ModelRouter.from_env()

    async def _embed(text: str) -> list[float]:
        vec_batch = await router.embed([text])
        return vec_batch[0]

    inserted = 0
    folded = 0
    errors: list[str] = []
    for c in plan.qa_chunks:
        try:
            vec = asyncio.run(_embed(c.question))
            mem_before = estore.write_log_stats(days=1)["folded"]
            estore.remember(
                repo_key=repo_key_override or f"local:{c.session_id}",
                question=c.question,
                answer=c.assistant_response,
                confidence=_Confidence(
                    score=0.6,
                    evidence_count=0,
                    retrieval_strength=0.0,
                    causal_confidence=0.0,
                    derivation=[f"ingested from {c.source}"],
                ),
                citations=[],
                model="(transcript-ingest)",
                provider="(transcript-ingest)",
                cost_usd=0.0,
                profile=router.active_profile_name,
                metadata={
                    "source": c.source,
                    "original_session_id": c.session_id,
                    "original_turn_ids": c.turn_ids,
                    "ingested_at": datetime.utcnow().isoformat(),
                },
                question_vector=vec,
                origin_agent=origin_agent,
                origin_session_id=c.session_id,
                user_id=user_id,
            )
            mem_after = estore.write_log_stats(days=1)["folded"]
            if mem_after > mem_before:
                folded += 1
            else:
                inserted += 1
        except Exception as e:
            errors.append(f"{c.session_id[:8]}: {e}")

    summary = Table(title=f"{origin_agent} ingestion result", expand=False)
    summary.add_column("metric")
    summary.add_column("value", justify="right")
    summary.add_row("chunks processed", str(len(plan.qa_chunks)))
    summary.add_row("inserted", str(inserted))
    summary.add_row("folded into existing", str(folded))
    summary.add_row("errors", str(len(errors)))
    console.print(summary)
    if errors:
        console.print("[yellow]first few errors:[/yellow]")
        for e in errors[:5]:
            console.print(f"  [yellow]{e}[/yellow]")
    estore.close()
    return inserted, folded, errors


@ingest_transcripts_app.command("cursor")
def ingest_cursor(
    since: Annotated[str | None, typer.Option("--since")] = None,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Substring filter against Cursor's workspace-id."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    repo_key: Annotated[str | None, typer.Option("--repo-key")] = None,
    user_id: Annotated[str | None, typer.Option("--user-id")] = None,
) -> None:
    """Ingest Cursor chat history from `workspaceStorage/*/state.vscdb`."""
    configure_logging()
    from asil_ingest_agents import CursorIngester

    since_dt = _parse_relative_window(since) if since else None
    if since and since_dt is None:
        console.print(f"[red]could not parse --since {since!r}[/red]")
        raise typer.Exit(code=2)

    plan = CursorIngester().plan(since=since_dt, workspace=workspace)
    console.print(
        f"[bold]Cursor workspaces:[/bold] {len(plan.sessions)} → {len(plan.qa_chunks)} Q/A chunks"
    )
    if not plan.qa_chunks:
        console.print(
            "[dim]Nothing to ingest. Cursor may not be installed, "
            "or its chat-data key may have changed across versions.[/dim]"
        )
        return
    preview = Table(title="first 5 chunks", expand=True)
    preview.add_column("workspace", no_wrap=True)
    preview.add_column("question", overflow="fold")
    for c in plan.qa_chunks[:5]:
        preview.add_row(c.session_id[:10], c.question[:140])
    console.print(preview)
    if dry_run:
        console.print("[yellow]--dry-run: no memories written.[/yellow]")
        return
    _write_plan_to_memory(plan, origin_agent="cursor", repo_key_override=repo_key, user_id=user_id)


@ingest_transcripts_app.command("generic-jsonl")
def ingest_generic_jsonl(
    path: Annotated[
        list[Path], typer.Option("--path", help="JSONL file to ingest. Pass multiple times.")
    ],
    role_key: Annotated[str, typer.Option("--role-key", help="Field name for role/type.")] = "role",
    text_key: Annotated[
        str, typer.Option("--text-key", help="Field name for message text.")
    ] = "content",
    ts_key: Annotated[
        str | None, typer.Option("--ts-key", help="Field name for timestamp (optional).")
    ] = "timestamp",
    user_label: Annotated[
        str, typer.Option("--user-label", help="Substring identifying user-role messages.")
    ] = "user",
    assistant_label: Annotated[
        str,
        typer.Option("--assistant-label", help="Substring identifying assistant-role messages."),
    ] = "assistant",
    source: Annotated[
        str, typer.Option("--source", help="metadata.source tag on the resulting memories.")
    ] = "generic-jsonl-transcript",
    origin_agent: Annotated[
        str, typer.Option("--origin-agent", help="origin_agent column value.")
    ] = "generic-jsonl",
    since: Annotated[str | None, typer.Option("--since")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    repo_key: Annotated[str | None, typer.Option("--repo-key")] = None,
    user_id: Annotated[str | None, typer.Option("--user-id")] = None,
) -> None:
    """Ingest any agent's JSONL transcript by mapping role + text columns.

    Aider example:
        asil ingest-transcripts generic-jsonl --path ~/.aider/chat.jsonl \\
            --source aider-transcript --origin-agent aider"""
    configure_logging()
    from asil_ingest_agents import GenericJsonlIngester

    since_dt = _parse_relative_window(since) if since else None
    if since and since_dt is None:
        console.print(f"[red]could not parse --since {since!r}[/red]")
        raise typer.Exit(code=2)

    ingester = GenericJsonlIngester(
        paths=path,
        role_key=role_key,
        text_key=text_key,
        ts_key=ts_key,
        user_label=user_label,
        assistant_label=assistant_label,
        source=source,
    )
    plan = ingester.plan(since=since_dt)
    console.print(
        f"[bold]Generic JSONL:[/bold] {len(plan.sessions)} file(s) → {len(plan.qa_chunks)} Q/A chunks"
    )
    if not plan.qa_chunks:
        console.print(
            "[dim]Nothing to ingest. Check --role-key / --text-key / --user-label / --assistant-label.[/dim]"
        )
        return
    if dry_run:
        console.print("[yellow]--dry-run: no memories written.[/yellow]")
        return
    _write_plan_to_memory(
        plan, origin_agent=origin_agent, repo_key_override=repo_key, user_id=user_id
    )


@app.command()
def watch(
    agents: Annotated[
        str,
        typer.Argument(help="Comma-separated agents to watch (claude-code,cursor)."),
    ],
    interval: Annotated[int, typer.Option("--interval", help="Seconds between polls.")] = 30,
    overlap: Annotated[
        int,
        typer.Option(
            "--overlap", help="Seconds of overlap between windows so stalls don't drop chunks."
        ),
    ] = 60,
    iterations: Annotated[
        int,
        typer.Option("--iterations", help="Stop after N polls (0 = forever)."),
    ] = 0,
    repo_key: Annotated[str | None, typer.Option("--repo-key")] = None,
) -> None:
    """Long-running poller: every --interval seconds, run the per-agent
    ingester with --since matching the window and write any new chunks
    to episodic memory. Dedupe in EpisodicStore.remember() ensures
    re-ingesting the same turns folds rather than duplicates."""
    configure_logging()
    from asil_ingest_agents import (
        ClaudeCodeIngester,
        CursorIngester,
        WatchTick,
        run_watch_loop,
    )

    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    ingesters: dict[str, object] = {}
    for a in agent_list:
        if a == "claude-code":
            ingesters[a] = ClaudeCodeIngester()
        elif a == "cursor":
            ingesters[a] = CursorIngester()
        else:
            console.print(f"[yellow]skipping unknown agent: {a!r}[/yellow]")
    if not ingesters:
        console.print("[red]no recognised agents to watch.[/red]")
        raise typer.Exit(code=2)

    console.print(
        f"[bold]asil watch[/bold] — polling {list(ingesters)} every {interval}s "
        f"(overlap={overlap}s). Ctrl-C to stop."
    )

    def on_tick(tick: WatchTick) -> None:
        for name, ing in ingesters.items():
            try:
                plan = ing.plan(since=tick.since)  # type: ignore[attr-defined]
            except Exception as e:
                console.print(f"[yellow]{name} plan() failed: {e}[/yellow]")
                continue
            if not plan.qa_chunks:
                continue
            console.print(
                f"[dim]{tick.started_at:%H:%M:%S} {name}: {len(plan.qa_chunks)} new chunk(s)[/dim]"
            )
            _write_plan_to_memory(plan, origin_agent=name, repo_key_override=repo_key)

    run_watch_loop(
        interval_seconds=interval,
        overlap_seconds=overlap,
        on_tick=on_tick,
        max_iterations=iterations if iterations > 0 else None,
    )


def _parse_relative_window(s: str) -> datetime | None:
    """'1h' → datetime.utcnow() - 1h. Supports h/d/m/w suffixes."""
    s = s.strip().lower()
    if not s:
        return None
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    unit = s[-1]
    from datetime import timedelta

    delta = {
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }.get(unit)
    if delta is None:
        return None
    return datetime.utcnow() - delta


if __name__ == "__main__":  # pragma: no cover
    app()
