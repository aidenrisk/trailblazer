"""Command line entry point. One perceive against one URL, printed as JSON."""

import json
import uuid
from pathlib import Path

import typer

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.scraper.scraper import perceive
from trailblazer.contracts.scraper_result import PerceiveRequest
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

    with BrowserSession(cdp_port=settings.cdp_port, headed=headed or settings.headed) as session:
        page = session.goto(url)
        result = perceive(page, PerceiveRequest(job_id=job, page_index=page_index), settings)

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
