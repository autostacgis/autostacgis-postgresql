"""Build the sibling engine and standard plugins for the independent service image."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
# Never bundle obsolete distribution versions beside the newly built wheels.
(root / "wheels").mkdir(exist_ok=True)
for path in (root / "wheels").glob("autostacgis*.whl"):
    path.unlink()
for name in ("autostacgis-python", "autostacgis-plugins"):
    source = root.parent / name
    if not (source / "pyproject.toml").exists():
        raise SystemExit(f"Checkout sibling {name} before building")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(root / "wheels"), str(source)], check=True
    )
