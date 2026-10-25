from types import SimpleNamespace

from autostacgis import Record
from autostacgis.plugin_manager import PluginManager
from autostacgis.stac import Stac

from autostacgis_postgresql.engine import persist, plugins


def test_unsaved_items_obey_the_strict_stac_contract_without_catalog_mutation(tmp_path):
    settings = SimpleNamespace(
        data_dir=tmp_path, result_collection="outputs", public_url="https://example.com"
    )
    stac = Stac(
        metadata_structure={
            "inline": {
                "properties": {
                    "properties": {"properties": {"autostacgis:source_item_id": {"type": "string"}}}
                }
            }
        }
    )
    record = Record(
        {
            "type": "Feature",
            "stac_version": "1.1.0",
            "id": "source",
            "geometry": None,
            "properties": {"datetime": "2026-01-01T00:00:00Z"},
            "links": [],
            "assets": {"data": {"href": "https://example.com/image.tif", "type": "image/tiff"}},
        }
    )
    feature = persist([record], settings, "job", stac=stac)["features"][0]
    assert feature == record.item
    assert "collection" not in feature
    assert stac.validate(feature) == feature


def test_installed_plugin_may_import_its_implementation_from_a_shared_library(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTACGIS_PLUGIN_DIR", str(tmp_path))
    PluginManager(tmp_path).install_file(
        "shared_service.py",
        '''"""Expose the executor service through a custom shared-library adapter."""
ID = "shared-service"
VERSION = "0.0.1"
CAPABILITY = "services"
REQUIRES = ["executor"]
from autostacgis_plugins.executor import executor as IMPLEMENTATION
''',
    )
    row = next(row for row in plugins()["plugins"] if row["id"] == "shared-service")
    assert row["version"] == "0.0.1"
    assert row["capability"] == "services"


def test_unsaved_payloads_are_not_automatically_encoded_or_catalogued(tmp_path):
    from autostacgis.artifacts import Artifact

    from autostacgis_postgresql.engine import durable_item

    settings = SimpleNamespace(
        data_dir=tmp_path, result_collection="outputs", public_url="https://example.com"
    )
    record = Record(
        {
            "type": "Feature",
            "stac_version": "1.1.0",
            "id": "transient",
            "geometry": None,
            "properties": {"datetime": "2026-01-01T00:00:00Z"},
            "links": [],
            "assets": {"data": {"href": "autostacgis://runtime/example"}},
        },
        payload=Artifact("bytes", b"unsaved"),
    )
    output = persist([record], settings, "job")["features"][0]
    assert output == record.item
    assert not durable_item(output)
    assert not (tmp_path / "results").exists()


def test_explicitly_saved_results_are_served_in_place_without_copying(tmp_path):
    from autostacgis_postgresql.engine import asset_file

    output = tmp_path / "scene" / "source_statistics.tif"
    output.parent.mkdir()
    output.write_bytes(b"already saved by the result destination")
    settings = SimpleNamespace(
        data_dir=tmp_path / "backend", result_collection="outputs", public_url="https://example.com"
    )
    record = Record(
        {
            "type": "Feature",
            "id": "saved",
            "properties": {},
            "links": [],
            "assets": {"data": {"href": output.as_uri()}},
        },
        runtime={"saved": True},
    )
    feature = persist([record], settings, "job")["features"][0]
    name = feature["assets"]["data"]["href"].rsplit("/", 1)[1]
    assert asset_file(settings, "job", name) == output
    assert list((settings.data_dir / "results/job").iterdir()) == [
        settings.data_dir / "results/job/files.json"
    ]
    assert output.read_bytes() == b"already saved by the result destination"
