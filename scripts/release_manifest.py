"""Generate the independent desktop backend update feed from built wheels."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("version")
parser.add_argument("--minimum-app", default="0.2.0")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
wheels = sorted((root / "wheels").glob("autostacgis*.whl"))
assert len(wheels) == 3, "Build exactly one wheel each for the engine, plugins and service"
assert any(path.name.startswith(f"autostacgis_postgresql-{args.version}-") for path in wheels)
manifest = {
    "format_version": 1,
    "version": args.version,
    "min_app_version": args.minimum_app,
    "python": "3.12",
    "wheels": [
        {
            "url": f"https://github.com/autostacgis/autostacgis-postgresql/releases/download/v{args.version}/{path.name}",
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for path in wheels
    ],
}
(root / "wheels/backend-release.json").write_text(json.dumps(manifest, indent=2) + "\n")
