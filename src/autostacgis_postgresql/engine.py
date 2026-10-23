from copy import deepcopy
from urllib.parse import urlsplit
from uuid import uuid4

from autostacgis import Application, Record, Registry
from autostacgis.config import validate
from autostacgis.registry import CAPABILITIES
from autostacgis_plugins import modules


def describe(value):
    if callable(value):
        return {"python": value.__module__ + "." + value.__qualname__}
    if isinstance(value, dict):
        return {key: describe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [describe(child) for child in value]
    return value


def plugins():
    rows = []
    for module in modules():
        implementation = module.IMPLEMENTATION
        contract = deepcopy(getattr(implementation, "contract", {}))
        rows.append(
            {
                "id": module.ID,
                "capability": module.CAPABILITY,
                "requires": list(module.REQUIRES),
                "defaults": deepcopy(getattr(module, "DEFAULTS", contract.get("settings", {}))),
                "settings_schema": deepcopy(
                    getattr(
                        implementation,
                        "settings_schema",
                        contract.get("settings_schema", {"type": "object", "additionalProperties": True}),
                    )
                ),
                "contract": describe(contract),
            }
        )
    return {
        "capabilities": list(CAPABILITIES),
        "plugins": rows,
        "default_configuration": {
            "catalog": {
                "version": "1.0.0",
                "plugins": [
                    {"use": name}
                    for name in (
                        "stac-source",
                        "metadata-value",
                        "executor",
                        "storage",
                        "events",
                        "geotiff",
                        "parquet",
                        "filesystem",
                        "stac-json",
                    )
                ],
            },
            "request": {"sources": ["stac-source"], "goal": {"output": {}}},
        },
    }


def application(configuration, settings, directory=None):
    # Remote callers select installed names; importing arbitrary modules is a local Python API only.
    validate(configuration, "config")
    known = Registry(builtins=True).plugins
    catalog = configuration.get("catalog")
    if catalog:
        for entry in catalog["plugins"]:
            if "use" not in entry or entry["use"] not in known:
                raise ValueError(
                    "API catalogs must select installed plugins using {use: name, settings: {...}}"
                )
    return Application(configuration, base_dir=directory or settings.data_dir)


def prepare(configuration, request, inputs, settings, directory=None):
    app = application(configuration, settings, directory)
    request = app.request(request or configuration["request"])
    records = None
    if inputs is not None:
        if len(inputs) > settings.max_inputs:
            raise ValueError(f"At most {settings.max_inputs} inputs are allowed")
        records = []
        for item in inputs:
            app.stac.validate(item)
            from autostacgis.data.kinds import describe_kinds

            records.append(describe_kinds(Record(deepcopy(item))))
    return app, request, records


def persist(records, settings, identifier, validate=None):
    from autostacgis.data.writers import write_parquet, write_raster

    directory = settings.data_dir / "results" / str(identifier)
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    published = []
    for record in records:
        item = deepcopy(record.item)
        asset = record.runtime.get("selected_asset", "data")
        item["properties"]["autostacgis:source_item_id"] = item["id"]
        item["id"] = str(identifier) + "-" + record.key
        item["collection"] = settings.result_collection
        item["links"] = []
        if record.payload is not None:
            stem = uuid4().hex
            if record.kind == "raster":
                name = stem + ".tif"
                media = "image/tiff; application=geotiff; profile=cloud-optimized"
            else:
                name = stem + ".parquet"
                media = "application/vnd.apache.parquet"
            item.setdefault("assets", {}).setdefault(asset, {}).update(
                href=f"{settings.public_url.rstrip('/')}/assets/{identifier}/{name}", type=media
            )
            if record.kind == "raster":
                write_raster(record.payload, directory / name, driver="COG", stac=item, asset_key=asset)
            else:
                write_parquet(record.payload, directory / name)
        elif any(
            urlsplit(a.get("href", "")).scheme not in ("http", "https", "s3", "gs", "az")
            for a in item.get("assets", {}).values()
        ):
            raise ValueError(
                "Result has local or transient assets without a payload; configure durable publication"
            )
        value = record.clone()
        value.item = item
        published.append(value)
        items.append(item)
    if validate is not None and not validate(published):
        raise ValueError(
            "Published outputs no longer satisfy the requested goal; publication changes IDs, collection, and asset URLs"
        )
    return {"type": "FeatureCollection", "features": items}
