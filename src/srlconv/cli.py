from __future__ import annotations

import logging
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

import typer
from rich import print as rich_print
from rich.logging import RichHandler

from srlconv import lab


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
    rich_print(_get_version())


@app.command("convert")
def convert_cmd(
    current_version: str = typer.Option(
        ...,
        "--current-version",
        help="Current SR Linux version (e.g. 25.10.1 or v25.10.1).",
    ),
    current_config: Path = typer.Option(
        ...,
        "--current-config",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to the current configuration file.",
    ),
    current_type: str = typer.Option(
        "ixr-d2l",
        "--current-type",
        help="SR Linux device type for the source node.",
    ),
    target_version: str = typer.Option(
        ...,
        "--target-version",
        help="Target SR Linux version (e.g. 25.10.1 or v25.10.1).",
    ),
    target_type: Optional[str] = typer.Option(
        None,
        "--target-type",
        help="SR Linux device type for the target node (defaults to --current-type).",
    ),
) -> None:
    """Template a Containerlab topology and deploy it for configuration conversion."""
    log = logging.getLogger("srlconv")
    effective_target_type = target_type if target_type is not None else current_type
    try:
        workdir = lab.prepare_and_deploy(
            current_version=current_version,
            current_config=current_config,
            current_type=current_type,
            target_version=target_version,
            target_type=effective_target_type,
        )
    except FileNotFoundError as e:
        rich_print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except subprocess.CalledProcessError as e:
        rich_print(f"[red]Containerlab exited with status {e.returncode}[/red]")
        raise typer.Exit(e.returncode or 1) from e
    except RuntimeError as e:
        rich_print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    log.info("Topology deployed; lab files kept under %s", workdir)
    rich_print(f"Lab workspace: {workdir}")
