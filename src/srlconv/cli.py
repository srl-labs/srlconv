from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

import typer
from rich import print as rich_print
from rich.logging import RichHandler


def _get_version() -> str:
    try:
        return version("srlconv")
    except PackageNotFoundError:
        return "0.0.0"


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


def _version_callback(value: bool) -> None:
    if value:
        rich_print(_get_version())
        raise typer.Exit(0)


app = typer.Typer(
    invoke_without_command=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def main(
    ctx: typer.Context,
    show_version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        help="Show version and exit.",
        callback=_version_callback,
        is_flag=True,
        is_eager=True,
    ),
) -> None:
    _configure_logging()
    if ctx.invoked_subcommand is None:
        rich_print(ctx.get_help())


@app.command("version")
def version_cmd() -> None:
    """Print the installed srlconv version."""
    _configure_logging()
    rich_print(_get_version())
