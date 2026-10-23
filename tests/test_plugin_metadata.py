from types import SimpleNamespace

from autostacgis import Record
from autostacgis.plugin_manager import PluginManager
from autostacgis.stac import Stac

from autostacgis_postgresql.engine import persist, plugins


def test_published_items_obey_the_strict_stac_contract(tmp_path):
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
    assert feature["collection"] == "outputs"
    assert feature["links"][0]["rel"] == "collection"
    assert stac.validate(feature) == feature


def test_installed_plugin_may_import_its_implementation_from_a_shared_library(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTACGIS_PLUGIN_DIR", str(tmp_path))
    PluginManager(tmp_path).install_file(
        "shared_service.py",
        """ID = "shared-service"
VERSION = "1.0.0"
CAPABILITY = "services"
REQUIRES = ["executor"]
from autostacgis_plugins.executor import executor as IMPLEMENTATION
""",
    )
    row = next(row for row in plugins()["plugins"] if row["id"] == "shared-service")
    assert row["version"] == "1.0.0"
    assert row["capability"] == "services"
