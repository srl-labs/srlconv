from __future__ import annotations

import logging
import shlex
import subprocess
from importlib.metadata import PackageNotFoundError, version
from logging import LogRecord
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich import print as rich_print
from rich.console import Console, ConsoleRenderable
from rich.logging import RichHandler
from rich.markup import escape as rich_escape
from rich.table import Table

from srlconv import lab


class SrlconvRichHandler(RichHandler):
    """RichHandler that allows lab subprocess lines to supply a pre-built renderable (ANSI or markup)."""

    def render_message(self, record: LogRecord, message: str) -> ConsoleRenderable:
        rich_msg = getattr(record, "srlconv_rich", None)
        if rich_msg is not None:
            return rich_msg
        return super().render_message(record, message)


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
        handlers=[
            SrlconvRichHandler(
                rich_tracebacks=True,
                show_path=False,
                markup=True,
            )
        ],
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
    effective_target_type = target_type if target_type is not None else current_type
    cv = lab.normalize_srlinux_version(current_version)
    tv = lab.normalize_srlinux_version(target_version)
    console = Console()
    try:
        (
            workdir,
            orig_cfg_path,
            orig_cli_path,
            orig_cli_flat_path,
            converted_path,
            cli_path,
            cli_flat_path,
        ) = lab.prepare_and_deploy(
            current_version=current_version,
            current_config=current_config,
            current_type=current_type,
            target_version=target_version,
            target_type=effective_target_type,
            post_deploy_context=lambda: console.status(
                "collecting configuration files, please wait",
                spinner="dots",
            ),
        )
    except FileNotFoundError as e:
        rich_print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except subprocess.CalledProcessError as e:
        rich_print(f"[red]Containerlab exited with status {e.returncode}[/red]")
        err_text = (e.stderr or "").strip()
        out_text = (e.stdout or "").strip()
        detail = "\n".join(x for x in (err_text, out_text) if x)
        if detail:
            rich_print(f"[red]{rich_escape(detail)}[/red]")
        raise typer.Exit(e.returncode or 1) from e
    except RuntimeError as e:
        rich_print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    rich_print()
    rich_print(f"Conversion workspace: {workdir}")
    table = Table(show_header=True, box=box.SIMPLE_HEAD)
    table.add_column("Description", style="bold")
    table.add_column("Path", overflow="fold")
    table.add_row(f"Current config {cv} [JSON]", str(orig_cfg_path))
    table.add_row(f"Target config {tv} [JSON]", str(converted_path))
    table.add_row(f"Current config {cv} [CLI]", str(orig_cli_path))
    table.add_row(f"Target config {tv} [CLI]", str(cli_path))
    table.add_row(f"Current config {cv} [CLI Flat]", str(orig_cli_flat_path))
    table.add_row(f"Target config {tv} [CLI Flat]", str(cli_flat_path))
    rich_print(table)

    diff_cmd = (
        "git diff --patience --color-moved=dimmed-zebra "
        f"{shlex.quote(str(orig_cli_flat_path))} {shlex.quote(str(cli_flat_path))}"
    )
    rich_print()
    rich_print("[bold]Show diff between configs:[/bold]")
    rich_print(diff_cmd)
