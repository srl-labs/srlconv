from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, PackageLoader

_LOG = logging.getLogger(__name__)

# Must match `name:` in srlconv.clab.yaml.j2 (used in paths under conversion_files).
LAB_NAME = "conversion"
# Topology node that carries the source (current) configuration; used for `clab save --node-filter`.
CURRENT_TOPOLOGY_NODE = "srl-current"
# Node that receives the converted config upgrade.
TARGET_TOPOLOGY_NODE = "srl-target"


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


def prepare_and_deploy(
    *,
    current_version: str,
    current_config: Path,
    current_type: str,
    target_version: str,
    target_type: str,
) -> tuple[Path, Path]:
    """Deploy lab, save current config, run tools upgrade on srl-target (in-place JSON), copy to ``converted/<ver>.cfg.json``."""
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
    template = env.get_template("srlconv.clab.yaml.j2")
    body = template.render(
        current_version=cv,
        current_config=str(config_path),
        current_type=current_type,
        target_version=tv,
        target_type=target_type,
    )
    topology_path = workdir / "srlconv.clab.yaml"
    topology_path.write_text(body, encoding="utf-8")

    cli = _find_containerlab_cli()
    if cli is None:
        raise RuntimeError(
            "Neither 'containerlab' nor 'clab' was found on PATH. "
            "Install Containerlab and ensure it is available in your environment."
        )

    _LOG.info("Using lab directory: %s", workdir)
    _LOG.info("Running: %s deploy -t %s (cwd=%s)", cli, topology_path.name, workdir)
    subprocess.run(
        [cli, "deploy", "-t", topology_path.name],
        cwd=workdir,
        check=True,
    )

    _LOG.info(
        "Running: %s save --copy %s --node-filter %s -t %s",
        cli,
        conversion_files,
        CURRENT_TOPOLOGY_NODE,
        topology_path,
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
            str(topology_path.resolve()),
        ],
        cwd=workdir,
        check=True,
    )

    _LOG.info("Running: chmod -R 777 %s", conversion_files)
    subprocess.run(
        ["chmod", "-R", "777", str(conversion_files.resolve())],
        check=True,
    )

    upgrade_file_in_target = (
        f"/home/admin/conversion_files/clab-{LAB_NAME}/"
        f"{CURRENT_TOPOLOGY_NODE}/config.json"
    )
    tools_line = (
        f"tools system configuration upgrade file {upgrade_file_in_target}"
    )
    # Same line as in an interactive SR Linux session; wrapped for non-interactive use.
    exec_cmd = f'sr_cli "{tools_line}"'
    _LOG.info(
        "Running: %s exec -t %s --label clab-node-name=%s --cmd %r",
        cli,
        topology_path.name,
        TARGET_TOPOLOGY_NODE,
        exec_cmd,
    )
    subprocess.run(
        [
            cli,
            "exec",
            "-t",
            str(topology_path.resolve()),
            "--label",
            f"clab-node-name={TARGET_TOPOLOGY_NODE}",
            "--cmd",
            exec_cmd,
        ],
        cwd=workdir,
        check=True,
    )

    converted_dir = workdir / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)
    upgraded_json = (
        conversion_files / f"clab-{LAB_NAME}" / CURRENT_TOPOLOGY_NODE / "config.json"
    )
    if not upgraded_json.is_file():
        msg = f"Expected converted config not found: {upgraded_json}"
        raise FileNotFoundError(msg)
    out_cfg = converted_dir / f"{tv}.cfg.json"
    shutil.copy2(upgraded_json, out_cfg)
    _LOG.info("Wrote converted configuration: %s", out_cfg)

    return workdir, out_cfg
