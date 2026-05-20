"""ASIL CLI entry point.

Phase 0 commands:
  asil status              — show backing service health
  asil llm ping [--tier T] — round-trip a small completion through the router
  asil llm profile         — show the active LLM profile + provider mapping

Future phases add: ingest, ask, replay, drift report, events.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from asil_core import configure_logging, get_settings
from asil_core.llm import ModelRouter
from asil_core.llm.profiles import CHAT_TIERS

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
        except Exception as e:  # noqa: BLE001
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
        return [(name, url, st, detail) for (name, url), (st, detail) in zip(targets, results)]

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
    tier: Annotated[str, typer.Option(help="Tier to test (reasoning|classify|summarize|verify)")] = "reasoning",
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


if __name__ == "__main__":  # pragma: no cover
    app()
