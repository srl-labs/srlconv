from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, PackageLoader

_LOG = logging.getLogger(__name__)


def normalize_srlinux_version(value: str) -> str:
    """Normalize SR Linux version string so that it is always in the format of '25.10.1'."""
    v = value.strip()
    if not v:
        return v
    if v.startswith(("v", "V")):
        return v[1:]
    return v.lower()
    return "v" + v


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
) -> Path:
    """Write topology and conversion_files under a temp directory and run containerlab deploy."""
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
    return workdir
