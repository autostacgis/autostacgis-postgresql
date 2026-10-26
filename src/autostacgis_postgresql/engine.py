import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, urlsplit

from autostacgis import Application, Record, Registry
from autostacgis.config import validate
from autostacgis.registry import CAPABILITIES


def describe(value):
    if callable(value):
        return {"python": value.__module__ + "." + value.__qualname__}
    if isinstance(value, dict):
        return {key: describe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [describe(child) for child in value]
    return value


def plugins():
    from autostacgis.plugin_metadata import inventory
    from autostacgis.workflow_config import default_workflow

    return {
        "capabilities": list(CAPABILITIES),
        "plugins": describe(inventory(Registry())),
        "default_configuration": default_workflow(),
    }


def application(configuration, settings, directory=None):
    # Remote callers select installed names; importing arbitrary modules is a local Python API only.
    validate(configuration, "config")
    known = Registry(builtins=True).plugins
    for capability in CAPABILITIES:
        for name in configuration.get(capability, {}):
            if name not in known or capability not in known[name]:
                raise ValueError(f"Unavailable installed {capability} plugin: {name}")
    return Application(configuration, base_dir=directory or settings.data_dir)


def prepare(configuration, inputs, settings, directory=None):
    app = application(configuration, settings, directory)
    execution = app._execution()
    records = None
    if inputs is not None:
        if len(inputs) > settings.max_inputs:
            raise ValueError(f"At most {settings.max_inputs} inputs are allowed")
        records = []
        for item in inputs:
            app.stac.validate(item)
            from autostacgis.data.kinds import describe_kinds

            records.append(describe_kinds(Record(deepcopy(item))))
    return app, execution, records


def durable_item(item):
    """Transient payload handles must never become persistent catalog assets."""
    return bool(item.get("assets")) and all(
        urlsplit(asset.get("href", "")).scheme not in ("autostacgis", "memory")
        for asset in item["assets"].values()
    )


def asset_file(settings, identifier, name):
    """Resolve an explicitly saved file without duplicating its data."""
    root = (settings.data_dir / "results").resolve()
    directory = (root / str(identifier)).resolve()
    if not directory.is_relative_to(root):
        raise FileNotFoundError(name)
    manifest = directory / "files.json"
    if manifest.is_file():
        files = json.loads(manifest.read_text())
        if name in files:
            target = Path(files[name])
            if target.is_file():
                return target
    raise FileNotFoundError(name)


def persist(records, settings, identifier, validate=None, stac=None):
    """Return result metadata; only expose files explicitly saved by a result policy.

    No encoding, copying or materialization takes place here. The process result's
    configured encoder/data_out pipeline owns all durable output bytes.
    """
    from autostacgis.io import is_local, local

    items, published, files, saved = [], [], {}, []
    for record in records:
        item = deepcopy(record.item)
        if record.runtime.get("saved"):
            for key, asset in item.get("assets", {}).items():
                href = asset.get("href", "")
                if is_local(href):
                    target = local(href).resolve()
                    if not target.is_file():
                        continue  # Directory outputs keep their destination reference.
                    name = record.key + "-" + str(len(files)) + target.suffix
                    files[name] = str(target)
                    asset["href"] = (
                        f"{settings.public_url.rstrip('/')}/assets/{identifier}/{quote(name, safe='')}"
                    )
        if record.runtime.get("saved") and durable_item(item):
            item["properties"]["autostacgis:source_item_id"] = item["id"]
            item["id"] = str(identifier) + "-" + record.key
            item["collection"] = settings.result_collection
            saved.append(item["id"])
            item["links"] = [
                {
                    "rel": "collection",
                    "href": f"{settings.public_url.rstrip('/')}/collections/{quote(settings.result_collection, safe='')}",
                    "type": "application/json",
                }
            ]
        value = record.clone()
        value.item = item
        published.append(value)
        if stac is not None:
            if record.runtime.get("saved"):
                stac.extensions(item)
            stac.validate(item)
        items.append(item)
    if validate is not None and not validate(published):
        raise ValueError(
            "Saved output metadata no longer satisfies the requested goal; catalog registration changes IDs, collection, and asset URLs"
        )
    if files:
        directory = settings.data_dir / "results" / str(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ".files.json"
        temporary.write_text(json.dumps(files))
        temporary.replace(directory / "files.json")
    return {"type": "FeatureCollection", "features": items, "saved": saved}
