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
    resolve_repo,
)
from asil_memory import (
    EpisodicStore,
    EpisodicStoreError,
    GraphStore,
    GraphStoreError,
    HybridRetriever,
    Memory,
    RetrievalResult,
    VectorStore,
    VectorStoreError,
)
from asil_reasoning import Verifier, VerifierResult, score_verified_answer
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
app.add_typer(llm_app, name="llm")
app.add_typer(graph_app, name="graph")
app.add_typer(vector_app, name="vector")
app.add_typer(eval_app, name="eval")
app.add_typer(memory_app, name="memory")
app.add_typer(postmortem_app, name="postmortem")
app.add_typer(events_app, name="events")

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
) -> None:
    """Ask ASIL a question about the indexed code.

    Pipeline (Phase 2): hybrid retrieve -> reasoning LLM -> verifier pass ->
    composed Confidence -> persist to episodic memory. Subsequent runs
    surface similar prior conclusions before producing a fresh answer.
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
        RetrievalResult, str, float, VerifierResult | None, list[Memory], str
    ]:
        # Recall first so the LLM has prior context (cheap — one extra vector query).
        prior_memories: list[Memory] = []
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

        result = await retriever.retrieve(question, repo_key=repo)
        if not result.candidates:
            return (
                result,
                "(no candidates retrieved — index may be empty for this repo)",
                0.0,
                None,
                prior_memories,
                "tight",
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
        )

    result, answer_text, cost, verifier_result, prior_memories, _profile_name = asyncio.run(_run())

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
def memory_stats() -> None:
    """Total + per-repo memory counts."""
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


if __name__ == "__main__":  # pragma: no cover
    app()
