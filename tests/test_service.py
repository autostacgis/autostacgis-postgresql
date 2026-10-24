import os
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from autostacgis_postgresql.app import create_app
from autostacgis_postgresql.config import Settings
from autostacgis_postgresql.worker import execute_job


@pytest.fixture
def client(tmp_path):
    dsn = os.environ.get("AUTOSTACGIS_TEST_DSN")
    if not dsn:
        pytest.skip("Set AUTOSTACGIS_TEST_DSN to a disposable migrated pgSTAC database")
    settings = Settings(
        database_url=dsn, api_token="test-token", data_dir=tmp_path, public_url="http://testserver"
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer test-token"
        yield client


def item():
    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "id": "scene-" + uuid4().hex,
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        "bbox": [0, 0, 1, 1],
        "properties": {"datetime": "2026-01-01T00:00:00Z", "ready": True},
        "links": [],
        "assets": {
            "data": {
                "href": "https://example.test/data.tif",
                "roles": ["data"],
                "type": "image/tiff; application=geotiff",
            }
        },
    }


def invocation():
    return {
        "configuration": {
            "catalog": {
                "version": "1.0.0",
                "plugins": [{"use": name} for name in ("metadata-value", "executor", "storage", "events")],
            },
            "request": {
                "goal": {
                    "plugin": "metadata-value",
                    "settings": {"jsonata": "item.properties.ready", "value": True},
                }
            },
        },
        "inputs": [item()],
    }


def test_standard_transactions_search_and_cql2(client):
    identifier = "test-" + uuid4().hex
    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": identifier,
        "description": "test",
        "license": "proprietary",
        "links": [],
        "extent": {"spatial": {"bbox": [[-180, -90, 180, 90]]}, "temporal": {"interval": [[None, None]]}},
    }
    response = client.post("/collections", json=collection)
    assert response.status_code in (200, 201), response.text
    value = item()
    value["collection"] = identifier
    response = client.post(f"/collections/{identifier}/items", json=value)
    assert response.status_code in (200, 201), response.text
    response = client.post(
        "/search",
        json={"collections": [identifier], "bbox": [0, 0, 2, 2], "limit": 1},
        headers={"Authorization": ""},
    )
    assert response.status_code == 200, response.text
    assert response.json()["features"][0]["id"] == value["id"]
    response = client.get(
        "/search",
        params={"collections": identifier, "filter-lang": "cql2-text", "filter": f"id = '{value['id']}'"},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["features"]) == 1
    assert client.get("/queryables").status_code == 200
    assert client.get("/conformance").status_code == 200
    client.delete(f"/collections/{identifier}/items/{value['id']}")
    client.delete(f"/collections/{identifier}")


def test_authentication_and_plugin_parameter_contracts(client):
    response = client.get("/autostacgis/plugins", headers={"Authorization": ""})
    assert response.status_code == 401
    descriptor = client.get("/autostacgis/plugins").json()
    assert len(descriptor["capabilities"]) == 15
    plugins = {plugin["id"]: plugin for plugin in descriptor["plugins"]}
    assert plugins["metadata-value"]["contract"]["solver"] == "backward_chain"
    assert "scheduler" not in plugins
    assert plugins["apscheduler"]["capability"] == plugins["watchdog"]["capability"] == "trigger"
    assert "observer" in plugins["watchdog"]["settings_schema"]["properties"]
    assert client.post("/collections", json={}, headers={"Authorization": ""}).status_code == 401


def test_configuration_validation_planning_and_name_safety(client):
    value = invocation()
    response = client.post("/autostacgis/validate", json=value)
    assert response.status_code == 200, response.text
    response = client.post("/autostacgis/plan", json=value)
    assert response.status_code == 200, response.text
    assert response.json()["steps"] == []
    identifier = uuid4().hex
    response = client.put("/autostacgis/configurations/" + identifier, json=value["configuration"])
    assert response.status_code == 200, response.text
    assert any(row["id"] == identifier for row in client.get("/autostacgis/configurations").json())
    invalid = deepcopy(value)
    invalid["configuration"]["catalog"]["plugins"] = [{"use": "os.system"}]
    assert client.post("/autostacgis/validate", json=invalid).status_code == 422
    invalid = deepcopy(value)
    invalid["configuration"]["request"]["goal"]["settings"]["typo"] = 1
    assert client.post("/autostacgis/validate", json=invalid).status_code == 422


def test_durable_queue_claim_execution_and_result_ingest(client, tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    path = tmp_path / "input.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=1,
        dtype="uint16",
        crs="EPSG:4326",
        transform=from_bounds(0, 0, 1, 1, 32, 32),
    ) as raster:
        raster.write(np.ones((1, 32, 32), dtype="uint16"))
    value = invocation()
    value["inputs"][0]["assets"]["data"]["href"] = str(path)
    response = client.post("/autostacgis/jobs", json=value)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    store = client.app.state.store
    # Other test/development jobs may exist: claim until this newly queued job is reached.
    while True:
        job = store.claim()
        assert job is not None
        if str(job["id"]) == identifier:
            break
        store.finish(job["id"], "failed", error="Discarded by disposable database integration test")
    assert store.job(job["id"])["state"] == "running"
    execute_job(identifier, client.app.state.autostacgis_settings)
    response = client.get("/autostacgis/jobs/" + identifier)
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["state"] == "succeeded", completed
    output = completed["result"]["features"][0]
    response = client.post("/search", json={"ids": [output["id"]], "collections": [output["collection"]]})
    assert response.status_code == 200, response.text
    assert response.json()["features"][0]["id"] == output["id"]
    from urllib.parse import urlsplit

    href = output["assets"]["data"]["href"]
    response = client.get(urlsplit(href).path, headers={"Range": "bytes=0-15"})
    assert response.status_code == 206, response.text
    assert len(response.content) == 16
    artifact = (
        client.app.state.autostacgis_settings.data_dir
        / "results"
        / identifier
        / urlsplit(href).path.rsplit("/", 1)[1]
    )
    import json

    with rasterio.open(artifact) as raster:
        assert json.loads(raster.tags()["STAC"]) == output
    assert client.get("/cog/info", params={"url": str(path)}).status_code == 422


def test_cancelled_jobs_are_not_claimed_or_completed(client):
    value = client.post("/autostacgis/jobs", json=invocation()).json()
    assert client.delete("/autostacgis/jobs/" + value["id"]).json()["state"] == "cancelled"
    execute_job(value["id"], client.app.state.autostacgis_settings)
    assert client.get("/autostacgis/jobs/" + value["id"]).json()["state"] == "cancelled"


def test_trigger_jobs_expose_raw_options_and_require_fresh_discovery(client):
    value = invocation()
    value["configuration"]["catalog"]["plugins"].append(
        {
            "use": "apscheduler",
            "settings": {
                "scheduler": {"timezone": "UTC"},
                "trigger": "interval",
                "trigger_args": {"seconds": 30},
                "job": {"misfire_grace_time": 10},
            },
        }
    )
    value["trigger"] = "apscheduler"
    assert client.post("/autostacgis/jobs", json=value).status_code == 422
    del value["inputs"]
    response = client.post("/autostacgis/jobs", json=value)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert client.get("/autostacgis/jobs/" + identifier).json()["trigger_name"] == "apscheduler"
    assert client.delete("/autostacgis/jobs/" + identifier).status_code == 200


def test_timed_trigger_runs_in_a_supervised_process_and_can_be_cancelled(client, tmp_path):
    import json
    import time
    from threading import Thread
    from uuid import UUID

    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    from autostacgis_postgresql.worker import supervise

    path = tmp_path / "scheduled.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=1,
        dtype="uint16",
        crs="EPSG:4326",
        transform=from_bounds(0, 0, 1, 1, 32, 32),
    ) as raster:
        raster.write(np.ones((1, 32, 32), dtype="uint16"))
    source = item()
    source["assets"]["data"]["href"] = str(path)
    metadata = tmp_path / "scheduled.json"
    metadata.write_text(json.dumps(source))
    value = invocation()
    del value["inputs"]
    value["trigger"] = "apscheduler"
    value["configuration"]["catalog"]["plugins"] += [
        {"use": "stac-source", "settings": {"items": [str(metadata)]}},
        {
            "use": "apscheduler",
            "settings": {
                "scheduler": {"timezone": "UTC"},
                "trigger": "interval",
                "trigger_args": {"seconds": 0.2},
            },
        },
    ]
    value["configuration"]["request"]["sources"] = ["stac-source"]
    response = client.post("/autostacgis/jobs", json=value)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    store = client.app.state.store
    with store.connection() as conn:
        conn.execute("UPDATE autostacgis.jobs SET state='running' WHERE id=%s", (identifier,))
    job = store.job(UUID(identifier))
    thread = Thread(target=supervise, args=(job, client.app.state.autostacgis_settings), daemon=True)
    thread.start()
    completed = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            completed = store.job(UUID(identifier))
            if completed["result"] or completed["state"] == "failed":
                break
            time.sleep(0.1)
        log = client.app.state.autostacgis_settings.data_dir / "logs" / (identifier + ".log")
        assert completed and completed["result"], log.read_text() if log.exists() else completed
    finally:
        client.delete("/autostacgis/jobs/" + identifier)
        thread.join(12)
    assert not thread.is_alive()
    assert store.job(UUID(identifier))["state"] == "cancelled"


def test_publication_cannot_silently_invalidate_metadata_goals(client, tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    path = tmp_path / "identity.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=1,
        dtype="uint16",
        crs="EPSG:4326",
        transform=from_bounds(0, 0, 1, 1, 32, 32),
    ) as raster:
        raster.write(np.ones((1, 32, 32), dtype="uint16"))
    value = invocation()
    value["inputs"][0]["assets"]["data"]["href"] = str(path)
    value["configuration"]["request"]["goal"]["settings"] = {
        "jsonata": "item.id",
        "value": value["inputs"][0]["id"],
    }
    response = client.post("/autostacgis/jobs", json=value)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    store = client.app.state.store
    with store.connection() as conn:
        conn.execute("UPDATE autostacgis.jobs SET state='running' WHERE id=%s", (identifier,))
    with pytest.raises(ValueError, match="Published outputs"):
        execute_job(identifier, client.app.state.autostacgis_settings)
    assert client.get("/autostacgis/jobs/" + identifier).json()["state"] == "failed"


def test_grouped_workflow_crud_and_configured_trigger(client):
    from autostacgis.workflow_config import default_workflow

    document = default_workflow()
    document["goal"]["metadata-value"] = {"jsonata": "item.properties.ready", "value": True}
    value = {"configuration": document, "inputs": [item()]}
    response = client.post("/autostacgis/validate", json=value)
    assert response.status_code == 200, response.text
    descriptor = client.get("/autostacgis/plugins").json()
    assert set(descriptor["default_configuration"]) == {"solver", "goal"}
    assert all(plugin["description"] for plugin in descriptor["plugins"])
    name = "workflow-" + uuid4().hex
    response = client.post("/autostacgis/workflows", json={"name": name, "document": document})
    assert response.status_code == 201, response.text
    assert client.post("/autostacgis/workflows", json={"name": name, "document": document}).status_code == 409
    assert client.get("/autostacgis/workflows/" + name).json()["document"] == document
    assert client.put("/autostacgis/workflows/" + name, json=document).status_code == 200
    renamed = name + " renamed"
    assert client.patch("/autostacgis/workflows/" + name, json={"name": renamed}).status_code == 200
    assert client.get("/autostacgis/workflows/" + name).status_code == 404
    assert client.delete("/autostacgis/workflows/" + renamed).status_code == 200
    assert client.delete("/autostacgis/workflows/" + renamed).status_code == 404
    assert client.post("/autostacgis/workflows", json={"name": "", "document": document}).status_code == 422
    scheduled = {
        **document,
        "trigger": {"apscheduler": {"trigger": "interval", "trigger_args": {"hours": 1}}},
    }
    response = client.post("/autostacgis/jobs", json={"configuration": scheduled})
    assert response.status_code == 202, response.text
    job = client.get("/autostacgis/jobs/" + response.json()["id"]).json()
    assert job["trigger_name"] == "apscheduler"
    client.delete("/autostacgis/jobs/" + response.json()["id"])
