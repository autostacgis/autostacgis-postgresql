import io
import os
import tarfile
import zipfile
from urllib.request import urlopen

import pytest

from autostacgis_postgresql.embedded import EmbeddedBackend, exclusive_cluster
from autostacgis_postgresql.install_extensions import install_extensions


def test_cluster_has_only_one_owner(tmp_path):
    with exclusive_cluster(tmp_path):
        with pytest.raises(RuntimeError, match="already owned"):
            with exclusive_cluster(tmp_path):
                pass
    with exclusive_cluster(tmp_path):
        pass


def test_extension_installation_cannot_escape_the_runtime(tmp_path):
    archive = tmp_path / "extensions.jar"
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as bundle:
        entry = tarfile.TarInfo("../unaccent.control")
        payload = b"untrusted"
        entry.size = len(payload)
        bundle.addfile(entry, io.BytesIO(payload))
    with zipfile.ZipFile(archive, "w") as jar:
        jar.writestr("postgres.txz", data.getvalue())
    with pytest.raises(ValueError, match="escapes"):
        install_extensions(archive, tmp_path / "runtime")
    assert not (tmp_path / "unaccent.control").exists()


def test_native_backend_preserves_catalog_across_stop_and_restart(tmp_path):
    root = os.environ.get("AUTOSTACGIS_TEST_POSTGRES_ROOT")
    if not root:
        pytest.skip("Set AUTOSTACGIS_TEST_POSTGRES_ROOT to a native PostgreSQL/PostGIS runtime")
    import json
    from urllib.request import Request

    backend = EmbeddedBackend(root, tmp_path, "test-password", "test-token")
    with exclusive_cluster(tmp_path):
        try:
            backend.start()
            collection = {
                "type": "Collection",
                "stac_version": "1.1.0",
                "id": "retained",
                "description": "Retained embedded catalog",
                "license": "proprietary",
                "links": [],
                "extent": {
                    "spatial": {"bbox": [[-180, -90, 180, 90]]},
                    "temporal": {"interval": [[None, None]]},
                },
            }
            with urlopen(
                Request(
                    backend.url + "/collections",
                    data=json.dumps(collection).encode(),
                    headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
                )
            ) as response:
                assert response.status == 201
        finally:
            backend.stop()
        assert not backend.children
        assert not (backend.cluster / "postmaster.pid").exists()
        again = EmbeddedBackend(root, tmp_path, "test-password", "test-token")
        try:
            again.start()
            assert again.url == backend.url
            with urlopen(again.url + "/collections/retained") as response:
                assert json.load(response)["id"] == "retained"
        finally:
            again.stop()
        assert not (backend.cluster / "postmaster.pid").exists()
