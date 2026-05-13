"""Command-line entry point.

    workestrator run --config workestrator.yaml [--log-level INFO]
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from workestrator import __version__
from workestrator.config import load
from workestrator.orchestrator import Workestrator


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """workestrator — pearscarf-driven Claude orchestrator."""


@cli.command()
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    default="workestrator.yaml",
    show_default=True,
    help="Path to YAML config file.",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    default="INFO",
    show_default=True,
)
def run(config: str, log_level: str) -> None:
    """Run the orchestrator loop."""
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load(Path(config))
    logger = logging.getLogger("workestrator.cli")
    logger.info(
        f"starting workestrator v{__version__}: "
        f"pearscarf={cfg.pearscarf.mcp_url} roles={cfg.roles.dir} "
        f"poll={cfg.orchestrator.poll_interval_seconds}s "
        f"concurrency={cfg.orchestrator.max_concurrent_agents}"
    )
    try:
        asyncio.run(Workestrator(cfg).run())
    except KeyboardInterrupt:
        click.echo("\nbye.")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
