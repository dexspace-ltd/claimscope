"""Build the Lambda bundle: claimscope package + its dependencies as Linux ARM64 wheels. No Docker needed."""

import hashlib
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "services" / "api"
BUILD = ROOT / "infra" / "build"


def build_lambda(extra: str | None = None) -> Path:
    """Bundle for the Lambdas; `extra` adds an optional-dependency group (e.g. "video") in its own bundle."""
    project = tomllib.loads((SERVICE / "pyproject.toml").read_text())["project"]
    deps = project["dependencies"] + (project["optional-dependencies"][extra] if extra else [])
    suffix = f"-{extra}" if extra else ""
    out = BUILD / f"lambda{suffix}"
    stamp = hashlib.sha256("\n".join(sorted(deps)).encode()).hexdigest()
    deps_dir = BUILD / f"deps{suffix}"
    stamp_file = deps_dir / ".stamp"

    if not stamp_file.exists() or stamp_file.read_text() != stamp:
        shutil.rmtree(deps_dir, ignore_errors=True)
        subprocess.run(
            ["uv", "pip", "install", "--quiet", "--target", str(deps_dir),
             "--python-platform", "aarch64-manylinux_2_28", "--python-version", "3.12",
             "--only-binary", ":all:", *deps],
            check=True,
        )
        stamp_file.write_text(stamp)

    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(deps_dir, out, ignore=shutil.ignore_patterns(".stamp", "__pycache__"))
    shutil.copytree(SERVICE / "claimscope", out / "claimscope", ignore=shutil.ignore_patterns("__pycache__"))
    return out
