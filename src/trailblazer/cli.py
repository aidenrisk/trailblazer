"""Command line entry point. One perceive against one URL, printed as JSON."""

import json
import os
import uuid
from pathlib import Path

import typer

from trailblazer.agents.browser import shared_session
from trailblazer.agents.browser.launch import launch_persistent
from trailblazer.agents.browser.session import AttachedSession
from trailblazer.loop.orchestrator import perceive_once
from trailblazer.observability.logging import configure_logging
from trailblazer.shared.config import get_settings

app = typer.Typer(help="Trailblazer crawl pipeline.", no_args_is_help=True)


@app.command()
def scrape(
    url: str = typer.Option(..., "--url", help="Page to describe."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window."),
    page_index: int = typer.Option(1, "--page-index", help="Loop's page counter."),
    out: Path | None = typer.Option(None, "--out", help="Directory to write the result into."),
    job_id: str | None = typer.Option(None, "--job-id", help="Defaults to a fresh uuid."),
) -> None:
    """Perceive one page and print its `ScraperResult` as JSON.

    With `--out`, also writes `<out>/<job_id>/page_description.json` -- named for
    the contract it holds.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    job = job_id or uuid.uuid4().hex[:12]

    result = perceive_once(
        url=url, page_index=page_index, job_id=job, headed=headed, settings=settings
    )

    payload = result.model_dump(mode="json")
    typer.echo(json.dumps(payload, indent=2))

    if out is not None:
        target = out / job
        target.mkdir(parents=True, exist_ok=True)
        (target / "page_description.json").write_text(
            json.dumps(result.page.model_dump(mode="json"), indent=2)
        )
        typer.echo(f"wrote {target / 'page_description.json'}", err=True)


@app.command()
def launch(
    url: str | None = typer.Option(None, "--url", help="Page to open after launch."),
) -> None:
    """Start the shared headed browser, log in by hand, leave it running.

    The browser is detached, so it outlives this command. Its profile persists,
    and its endpoint is recorded in `SESSION_FILE`, so every later run --
    scraper, form filler, any agent -- attaches to it and inherits the login.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    profile = Path(os.path.expanduser(settings.browser_profile_dir))

    try:
        endpoint = launch_persistent(cdp_port=settings.cdp_port, profile_dir=profile)
    except RuntimeError as e:
        # The port being held by a foreign process is the one failure a human
        # must act on, so it is reported here rather than as a traceback.
        shared_session.clear_record(settings.session_file)
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e

    port = int(endpoint.rsplit(":", 1)[1])
    if url:
        with AttachedSession(cdp_port=port) as session:
            session.goto(url)

    record = shared_session.write_record(settings.session_file, port, str(profile))
    typer.echo(f"browser serving {endpoint}, profile {profile}")
    typer.echo(f"endpoint recorded at {record}; agents attach to it automatically")
    typer.echo("log in in that window -- it stays open, and the login persists.")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    port: int = typer.Option(8000, "--port", help="Port to bind."),
) -> None:
    """Serve the HTTP API. `POST /v0/carriers/{carrier_id}/crawl` runs one crawl."""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run("trailblazer.api:app", host=host, port=port, log_level=settings.log_level.lower())


def main() -> None:
    """Console-script entry point."""
    app()
