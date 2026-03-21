from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, PackageLoader

_LOG = logging.getLogger(__name__)

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
    _LOG.info(
        "Running: %s exec -f json -t %s --label clab-node-name=%s --cmd %r",
        cli,
        topology_file,
        node_name,
        cmd,
    )
    proc = subprocess.run(
        [
            cli,
            "exec",
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


def _clab_exec_target(
    *,
    cli: str,
    topology_file: str,
    workdir: Path,
    cmd: str,
) -> None:
    _LOG.info(
        "Running: %s exec -t %s --label clab-node-name=%s --cmd %r",
        cli,
        topology_file,
        TARGET_TOPOLOGY_NODE,
        cmd,
    )
    subprocess.run(
        [
            cli,
            "exec",
            "-t",
            topology_file,
            "--label",
            f"clab-node-name={TARGET_TOPOLOGY_NODE}",
            "--cmd",
            cmd,
        ],
        cwd=workdir,
        check=True,
    )


def prepare_and_deploy(
    *,
    current_version: str,
    current_config: Path,
    current_type: str,
    target_version: str,
    target_type: str,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    """Deploy lab; store original + converted JSON and CLI dumps under ``converted/``."""
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

    _LOG.info("Using lab directory: %s", workdir)
    _LOG.info("Running: %s deploy -t %s (cwd=%s)", cli, topology_file, workdir)
    subprocess.run(
        [cli, "deploy", "-t", topology_file],
        cwd=workdir,
        check=True,
    )

    _LOG.info(
        "Running: %s save --copy %s --node-filter %s -t %s",
        cli,
        conversion_files,
        CURRENT_TOPOLOGY_NODE,
        topology_file,
    )
    subprocess.run(
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
        check=True,
    )

    _LOG.info("Running: chmod -R 777 %s", conversion_files)
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
    _LOG.info("Wrote original JSON: %s", out_current_cfg)

    cur_cli_txt = _clab_exec_node_capture_json(
        cli=cli,
        topology_file=topology_file,
        workdir=workdir,
        node_name=CURRENT_TOPOLOGY_NODE,
        cmd=_SR_CLI_INFO_CANDIDATE,
    )
    out_current_cli = converted_dir / f"{cv}.cli.txt"
    out_current_cli.write_text(cur_cli_txt, encoding="utf-8")
    _LOG.info("Wrote original CLI config: %s", out_current_cli)

    cur_cli_flat_txt = _clab_exec_node_capture_json(
        cli=cli,
        topology_file=topology_file,
        workdir=workdir,
        node_name=CURRENT_TOPOLOGY_NODE,
        cmd=_SR_CLI_INFO_FLAT,
    )
    out_current_cli_flat = converted_dir / f"{cv}.cli-flat.txt"
    out_current_cli_flat.write_text(cur_cli_flat_txt, encoding="utf-8")
    _LOG.info("Wrote original CLI-Flat config: %s", out_current_cli_flat)

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
    _LOG.info("Wrote converted configuration: %s", out_target_cfg)

    tgt_cli_txt = _clab_exec_node_capture_json(
        cli=cli,
        topology_file=topology_file,
        workdir=workdir,
        node_name=TARGET_TOPOLOGY_NODE,
        cmd=_SR_CLI_INFO_CANDIDATE,
    )
    out_target_cli = converted_dir / f"{tv}.cli.txt"
    out_target_cli.write_text(tgt_cli_txt, encoding="utf-8")
    _LOG.info("Wrote converted CLI config: %s", out_target_cli)

    tgt_cli_flat_txt = _clab_exec_node_capture_json(
        cli=cli,
        topology_file=topology_file,
        workdir=workdir,
        node_name=TARGET_TOPOLOGY_NODE,
        cmd=_SR_CLI_INFO_FLAT,
    )
    out_target_cli_flat = converted_dir / f"{tv}.cli-flat.txt"
    out_target_cli_flat.write_text(tgt_cli_flat_txt, encoding="utf-8")
    _LOG.info("Wrote converted CLI-Flat config: %s", out_target_cli_flat)

    return (
        workdir,
        out_current_cfg,
        out_current_cli,
        out_current_cli_flat,
        out_target_cfg,
        out_target_cli,
        out_target_cli_flat,
    )
