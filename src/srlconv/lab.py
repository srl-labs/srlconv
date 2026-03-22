from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, ContextManager
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, PackageLoader
from rich.markup import escape as _rich_escape
from rich.text import Text

_LOG = logging.getLogger(__name__)

_CLAB_LOG_PREFIX = "[dim]containerlab[/dim] │ "

# Must match ``lab_name`` in srlconv.clab.yaml.j2 (used in paths under conversion_files).
LAB_NAME = "conversion"
# Topology node that carries the source (current) configuration; used for `clab save --node-filter`.
CURRENT_TOPOLOGY_NODE = "srl-current"
# Node that receives the converted config upgrade.
TARGET_TOPOLOGY_NODE = "srl-target"

# srl-target bind destinations (must match srlconv.clab.yaml.j2).
CONVERSION_FILES_MOUNT = "/home/admin/conversion_files"
LOAD_CONFIG_MOUNT = "/home/admin/load_config"
# Host path relative to lab workdir; must match the bind source in the topology template.
LOAD_CONFIG_HOST_RELPATH = "load_config"

# ``clab exec -f json`` wraps results; ``stdout`` holds sr_cli output (string or embedded JSON).
_SR_CLI_INFO_CANDIDATE = "sr_cli -e -d -- info /"
_SR_CLI_INFO_FLAT = "sr_cli -e -d -- info flat /"
# Command to list YANG containers and lists
_SR_CLI_CONTAINER_LIST_DISCOVER_CMD = (
    'sr_cli -e -d -- "info detail depth 1 / | as json"'
)


@dataclass
class YangTopLevelStructure:
    """Top-level YANG container vs list keys from ``info detail depth 1 / | as json``."""

    containers: list[str]
    lists: list[str]


def normalize_srlinux_version(value: str) -> str:
    """Strip whitespace and optional leading v/V for container image tags (e.g. v25.10.1 -> 25.10.1)."""
    v = value.strip()
    if not v:
        return v
    if v.startswith(("v", "V")):
        return v[1:]
    return v


def _find_containerlab_cli() -> str | None:
    for name in ("containerlab", "clab"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _log_clab_line(line: str) -> None:
    text = line.rstrip("\n\r")
    if not text:
        return
    prefix = Text.from_markup(_CLAB_LOG_PREFIX)
    if "\x1b" in text:
        body = Text.from_ansi(text)
    else:
        body = Text.from_markup(_rich_escape(text))
    _LOG.info("", extra={"srlconv_rich": prefix + body})


def _log_clab_captured(stdout: str | None, stderr: str | None) -> None:
    for block in (stdout or "", stderr or ""):
        for line in block.splitlines():
            _log_clab_line(line)


def _run_clab_deploy_streaming(*, cli: str, topology_file: str, workdir: Path) -> None:
    cmd = [cli, "deploy", "-t", topology_file]
    captured: list[str] = []
    with subprocess.Popen(
        cmd,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            captured.append(line)
            _log_clab_line(line)
        proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd,
            output="".join(captured),
            stderr=None,
        )


def _stdout_from_clab_exec_json(collection_json: str) -> str:
    """Extract sr_cli ``stdout`` from ``clab exec -f json`` output (per containerlab/exec)."""
    data = json.loads(collection_json)
    if not isinstance(data, dict):
        return collection_json
    for _cid, results in data.items():
        if not results or not isinstance(results, list):
            continue
        first = results[0]
        if not isinstance(first, dict):
            continue
        out = first.get("stdout")
        if out is None:
            err = first.get("stderr")
            if isinstance(err, str) and err.strip():
                return err
            continue
        if isinstance(out, str):
            return out
        return json.dumps(out, indent=2)
    return collection_json


def _clab_exec_node_capture_json(
    *,
    cli: str,
    topology_file: str,
    workdir: Path,
    node_name: str,
    cmd: str,
) -> str:
    """Run ``clab exec -f json`` on a node and return decoded command stdout (not the wrapper tree)."""
    proc = subprocess.run(
        [
            cli,
            "exec",
            "--log-level",
            "error",
            "-t",
            topology_file,
            "-f",
            "json",
            "--label",
            f"clab-node-name={node_name}",
            "--cmd",
            cmd,
        ],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        return ""
    try:
        return _stdout_from_clab_exec_json(raw)
    except json.JSONDecodeError:
        _LOG.warning("clab exec -f json stdout was not valid JSON; saving raw")
        return raw


def _topology_file(topology_path: Path) -> str:
    """Absolute path for ``clab -t`` (required for reliable exec/capture)."""
    return str(topology_path.expanduser().resolve())


def _parse_top_level_yang_structure(detail_json_text: str) -> YangTopLevelStructure:
    """Split top-level keys into YANG containers (object) vs lists (array); skip scalars and ``_*`` keys."""
    text = detail_json_text.strip()
    if not text:
        msg = "Empty stdout from info detail depth 1 / | as json"
        raise RuntimeError(msg)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        msg = f"Discovery output is not valid JSON: {e}"
        raise RuntimeError(msg) from e
    if not isinstance(obj, dict):
        msg = f"Expected JSON object from discovery, got {type(obj).__name__}"
        raise RuntimeError(msg)

    containers: list[str] = []
    lists: list[str] = []
    for key, val in obj.items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict):
            containers.append(key)
        elif isinstance(val, list):
            lists.append(key)
    containers.sort()
    lists.sort()
    return YangTopLevelStructure(containers=containers, lists=lists)


def _safe_yang_filename_segment(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_")


def _export_per_yang_cli_files(
    *,
    cli: str,
    topology_file: str,
    workdir: Path,
    node_name: str,
    yang: YangTopLevelStructure,
    converted_dir: Path,
    version_label: str,
) -> None:
    """Write ``<name>_<ver>.cli.txt`` / ``.cli-flat.txt`` per top-level container or list."""
    jobs: list[tuple[str, bool]] = [(n, False) for n in yang.containers] + [
        (n, True) for n in yang.lists
    ]
    for name, is_list in jobs:
        safe = _safe_yang_filename_segment(name)
        wild = " *" if is_list else ""
        info_cmd = f"sr_cli -e -d -- info / {name}{wild}"
        flat_cmd = f"sr_cli -e -d -- info flat / {name}{wild}"
        cli_body = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=node_name,
            cmd=info_cmd,
        )
        flat_body = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=node_name,
            cmd=flat_cmd,
        )
        out_cli = converted_dir / f"{safe}_{version_label}.cli.txt"
        out_flat = converted_dir / f"{safe}_{version_label}.cli-flat.txt"
        out_cli.write_text(cli_body, encoding="utf-8")
        out_flat.write_text(flat_body, encoding="utf-8")


def _clab_exec_target(
    *,
    cli: str,
    topology_file: str,
    workdir: Path,
    cmd: str,
) -> None:
    proc = subprocess.run(
        [
            cli,
            "exec",
            "--log-level",
            "error",
            "-t",
            topology_file,
            "--label",
            f"clab-node-name={TARGET_TOPOLOGY_NODE}",
            "--cmd",
            cmd,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            proc.args,
            output=proc.stdout,
            stderr=proc.stderr,
        )
    _log_clab_captured(proc.stdout, proc.stderr)


def prepare_and_deploy(
    *,
    current_version: str,
    current_config: Path,
    current_type: str,
    target_version: str,
    target_type: str,
    post_deploy_context: Callable[[], ContextManager[Any]] | None = None,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    """Deploy lab; store original + converted JSON, monolithic CLI dumps, and per-top-level-YANG CLI files."""
    config_path = current_config.resolve()
    if not config_path.is_file():
        msg = f"Configuration file not found: {config_path}"
        raise FileNotFoundError(msg)

    cv = normalize_srlinux_version(current_version)
    tv = normalize_srlinux_version(target_version)

    workdir = Path(tempfile.mkdtemp(prefix="srlconv-"))
    conversion_files = workdir / "conversion_files"
    conversion_files.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=PackageLoader("srlconv", "templates"),
        autoescape=False,
    )

    load_config_path = workdir / LOAD_CONFIG_HOST_RELPATH
    load_body = env.get_template("load_config.j2").render(
        lab_name=LAB_NAME,
        conversion_files_mount=CONVERSION_FILES_MOUNT,
        current_topology_node=CURRENT_TOPOLOGY_NODE,
    )
    load_config_path.write_text(load_body, encoding="utf-8")
    subprocess.run(
        ["chmod", "666", str(load_config_path.resolve())],
        check=True,
    )

    template = env.get_template("srlconv.clab.yaml.j2")
    body = template.render(
        lab_name=LAB_NAME,
        conversion_files_mount=CONVERSION_FILES_MOUNT,
        load_config_mount=LOAD_CONFIG_MOUNT,
        current_version=cv,
        current_config=str(config_path),
        current_type=current_type,
        target_version=tv,
        target_type=target_type,
    )
    topology_path = workdir / "srlconv.clab.yaml"
    topology_path.write_text(body, encoding="utf-8")
    topology_file = _topology_file(topology_path)

    cli_raw = _find_containerlab_cli()
    if cli_raw is None:
        raise RuntimeError(
            "Neither 'containerlab' nor 'clab' was found on PATH. "
            "Install Containerlab and ensure it is available in your environment."
        )
    cli = str(Path(cli_raw).resolve())

    _LOG.info("Lab workspace created at %s", workdir)
    _run_clab_deploy_streaming(
        cli=cli,
        topology_file=topology_file,
        workdir=workdir,
    )

    ctx = (
        nullcontext()
        if post_deploy_context is None
        else post_deploy_context()
    )
    with ctx:
        save_proc = subprocess.run(
            [
                cli,
                "save",
                "--copy",
                str(conversion_files.resolve()),
                "--node-filter",
                CURRENT_TOPOLOGY_NODE,
                "-t",
                topology_file,
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        if save_proc.returncode != 0:
            raise subprocess.CalledProcessError(
                save_proc.returncode,
                save_proc.args,
                output=save_proc.stdout,
                stderr=save_proc.stderr,
            )

        subprocess.run(
            ["chmod", "-R", "777", str(conversion_files.resolve())],
            check=True,
        )

        converted_dir = workdir / "converted"
        converted_dir.mkdir(parents=True, exist_ok=True)

        saved_json = (
            conversion_files / f"clab-{LAB_NAME}" / CURRENT_TOPOLOGY_NODE / "config.json"
        )
        if not saved_json.is_file():
            msg = f"Expected saved config not found: {saved_json}"
            raise FileNotFoundError(msg)
        out_current_cfg = converted_dir / f"{cv}.cfg.json"
        shutil.copy2(saved_json, out_current_cfg)

        yang_structure: dict[str, YangTopLevelStructure] = {}

        detail_current_raw = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=CURRENT_TOPOLOGY_NODE,
            cmd=_SR_CLI_CONTAINER_LIST_DISCOVER_CMD,
        )
        yang_structure["current"] = _parse_top_level_yang_structure(detail_current_raw)
        _export_per_yang_cli_files(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=CURRENT_TOPOLOGY_NODE,
            yang=yang_structure["current"],
            converted_dir=converted_dir,
            version_label=cv,
        )

        cur_cli_txt = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=CURRENT_TOPOLOGY_NODE,
            cmd=_SR_CLI_INFO_CANDIDATE,
        )
        out_current_cli = converted_dir / f"{cv}.cli.txt"
        out_current_cli.write_text(cur_cli_txt, encoding="utf-8")

        cur_cli_flat_txt = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=CURRENT_TOPOLOGY_NODE,
            cmd=_SR_CLI_INFO_FLAT,
        )
        out_current_cli_flat = converted_dir / f"{cv}.cli-flat.txt"
        out_current_cli_flat.write_text(cur_cli_flat_txt, encoding="utf-8")

        upgrade_file_in_target = (
            f"{CONVERSION_FILES_MOUNT}/clab-{LAB_NAME}/{CURRENT_TOPOLOGY_NODE}/config.json"
        )
        tools_line = f"tools system configuration upgrade file {upgrade_file_in_target}"
        # Same line as in an interactive SR Linux session; wrapped for non-interactive use.
        exec_cmd = f'sr_cli "{tools_line}"'
        _clab_exec_target(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            cmd=exec_cmd,
        )

        load_exec = f"sh -c 'sr_cli < {LOAD_CONFIG_MOUNT}'"
        _clab_exec_target(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            cmd=load_exec,
        )

        out_target_cfg = converted_dir / f"{tv}.cfg.json"
        shutil.copy2(saved_json, out_target_cfg)

        detail_target_raw = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=TARGET_TOPOLOGY_NODE,
            cmd=_SR_CLI_CONTAINER_LIST_DISCOVER_CMD,
        )
        yang_structure["target"] = _parse_top_level_yang_structure(detail_target_raw)
        _export_per_yang_cli_files(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=TARGET_TOPOLOGY_NODE,
            yang=yang_structure["target"],
            converted_dir=converted_dir,
            version_label=tv,
        )

        tgt_cli_txt = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=TARGET_TOPOLOGY_NODE,
            cmd=_SR_CLI_INFO_CANDIDATE,
        )
        out_target_cli = converted_dir / f"{tv}.cli.txt"
        out_target_cli.write_text(tgt_cli_txt, encoding="utf-8")

        tgt_cli_flat_txt = _clab_exec_node_capture_json(
            cli=cli,
            topology_file=topology_file,
            workdir=workdir,
            node_name=TARGET_TOPOLOGY_NODE,
            cmd=_SR_CLI_INFO_FLAT,
        )
        out_target_cli_flat = converted_dir / f"{tv}.cli-flat.txt"
        out_target_cli_flat.write_text(tgt_cli_flat_txt, encoding="utf-8")

        return (
            workdir,
            out_current_cfg,
            out_current_cli,
            out_current_cli_flat,
            out_target_cfg,
            out_target_cli,
            out_target_cli_flat,
        )
