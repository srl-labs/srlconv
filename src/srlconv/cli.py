from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from logging import LogRecord
from pathlib import Path
from typing import Optional

import typer
from deepdiff import DeepDiff
from rich import box
from rich import print as rich_print
from rich.console import Console, ConsoleRenderable
from rich.logging import RichHandler
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
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


def _syntax_theme_for_deepdiff() -> str:
    """Pick Rich Syntax theme for the host terminal.

    Rich cannot query the real background color portably. We use built-in
    ``ansi_light`` / ``ansi_dark`` (terminal-native 16-color pairs), plus:

    - ``SRLCONV_SYNTAX_THEME=ansi_light|ansi_dark|monokai`` to force a style.
    - If unset, ``COLORFGBG`` (``fg;bg``, 0--15) when present: background
      ``>= 8`` is treated as a light screen → ``ansi_light``, else ``ansi_dark``.
    - Otherwise ``ansi_dark`` (closest to the previous fixed ``monokai`` look).
    """
    override = os.environ.get("SRLCONV_SYNTAX_THEME", "").strip().lower()
    if override in ("ansi_light", "light"):
        return "ansi_light"
    if override in ("ansi_dark", "dark"):
        return "ansi_dark"
    if override == "monokai":
        return "monokai"
    fgbg = os.environ.get("COLORFGBG", "")
    parts = fgbg.split(";")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        bg = int(parts[1])
        if bg >= 8:
            return "ansi_light"
        return "ansi_dark"
    return "ansi_dark"


def _multiline_git_diff(path1: Path, path2: Path) -> str:
    q1 = shlex.quote(str(path1))
    q2 = shlex.quote(str(path2))
    return f"git diff --patience --color-moved=dimmed-zebra \\\n{q1} \\\n{q2}"


def _json_without_preamble(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k != "_preamble"}
    return obj


def _deepdiff_show_pair(
    console: Console,
    left: Path,
    right: Path,
    *,
    as_json: bool,
) -> None:
    try:
        if as_json:
            with left.open(encoding="utf-8") as f:
                a = _json_without_preamble(json.load(f))
            with right.open(encoding="utf-8") as f:
                b = _json_without_preamble(json.load(f))
        else:
            a = left.read_text(encoding="utf-8").splitlines()
            b = right.read_text(encoding="utf-8").splitlines()
    except json.JSONDecodeError as e:
        rich_print(f"[red]Invalid JSON: {e}[/red]")
        raise typer.Exit(1) from e
    except OSError as e:
        rich_print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    diff = DeepDiff(a, b, ignore_order=True)
    if not diff:
        console.print(
            "[green]No differences (ignoring list/order where applicable).[/green]"
        )
        return
    console.print(
        Syntax(
            diff.to_json(indent=2),
            "json",
            theme=_syntax_theme_for_deepdiff(),
            word_wrap=True,
        )
    )


def _prompt_deepdiff_after_diffs(
    console: Console,
    *,
    orig_cfg_path: Path,
    converted_path: Path,
    orig_cli_path: Path,
    cli_path: Path,
    orig_cli_flat_path: Path,
    cli_flat_path: Path,
) -> None:
    if not sys.stdin.isatty():
        return
    rich_print()
    while True:
        rich_print(
            "[bold]Show config diff in this format:[/bold]\n"
            "  1. json config\n"
            "  2. cli config\n"
            "  3. cli flat config\n"
            "  0. exit program"
        )
        try:
            choice = Prompt.ask(
                "Select option",
                choices=["0", "1", "2", "3"],
                default="0",
            )
        except KeyboardInterrupt:
            console.print()
            return
        if choice == "0":
            return
        if choice == "1":
            _deepdiff_show_pair(console, orig_cfg_path, converted_path, as_json=True)
        elif choice == "2":
            _deepdiff_show_pair(console, orig_cli_path, cli_path, as_json=False)
        else:
            _deepdiff_show_pair(
                console, orig_cli_flat_path, cli_flat_path, as_json=False
            )
        rich_print()


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
    show_git_diff_commands: bool = typer.Option(
        False,
        "--show-git-diff-commands",
        help="Print suggested git diff commands for CLI and CLI-flat configs.",
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
    table.add_row(
        rich_escape(f"[ JSON ] Current {cv}"),
        str(orig_cfg_path),
    )
    table.add_row(
        rich_escape(f"[ JSON ] Target {tv}"),
        str(converted_path),
    )
    table.add_row(
        rich_escape(f"[ CLI ] Current {cv}"),
        str(orig_cli_path),
    )
    table.add_row(
        rich_escape(f"[ CLI ] Target {tv}"),
        str(cli_path),
    )
    table.add_row(
        rich_escape(f"[ CLI Flat ] Current {cv}"),
        str(orig_cli_flat_path),
    )
    table.add_row(
        rich_escape(f"[ CLI Flat ] Target {tv}"),
        str(cli_flat_path),
    )
    rich_print(table)

    rich_print(
        Panel(
            "Check out README.md to understand the nuances of the diff outputs",
            border_style="dim",
            expand=False,
        )
    )
    if show_git_diff_commands:
        rich_print()
        rich_print("[bold]CLI-Flat config diff:[/bold]")
        rich_print(_multiline_git_diff(orig_cli_flat_path, cli_flat_path))
        rich_print()
        rich_print("[bold]CLI config diff:[/bold]")
        rich_print(_multiline_git_diff(orig_cli_path, cli_path))

    _prompt_deepdiff_after_diffs(
        console,
        orig_cfg_path=orig_cfg_path,
        converted_path=converted_path,
        orig_cli_path=orig_cli_path,
        cli_path=cli_path,
        orig_cli_flat_path=orig_cli_flat_path,
        cli_flat_path=cli_flat_path,
    )
