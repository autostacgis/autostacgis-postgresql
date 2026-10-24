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


def test_startup_failure_includes_recent_logs_without_credentials(tmp_path):
    backend = EmbeddedBackend(tmp_path, tmp_path, "password with spaces", "private-token")
    (tmp_path / "postgres.log").write_text(
        "old message\n" * 1000 + "startup failed password%20with%20spaces private-token\n"
    )
    error = str(backend.startup_error("postgres", "Embedded PostgreSQL exited"))
    assert "startup failed [redacted] [redacted]" in error
    assert "password" not in error
    assert len(error) < 6100
    assert "see postgres.log" in error


@pytest.mark.parametrize("nested", [False, True])
def test_contrib_files_follow_the_native_runtime_layout(tmp_path, nested):
    root = tmp_path / "runtime"
    share = root / "share" / "postgresql" if nested else root / "share"
    library = root / "lib" / "postgresql" if nested else root / "lib"
    (share / "extension").mkdir(parents=True)
    library.mkdir(parents=True)
    files = {
        "share/extension/btree_gist.control": b"control",
        "share/extension/btree_gist--1.0.sql": b"sql",
        "share/extension/unaccent.control": b"control",
        "share/extension/unaccent--1.0.sql": b"sql",
        "share/tsearch_data/unaccent.rules": b"rules",
        "lib/btree_gist.dll": b"library",
        "lib/unaccent.dll": b"library",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as bundle:
        for name, payload in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
    archive = tmp_path / "extensions.jar"
    with zipfile.ZipFile(archive, "w") as jar:
        jar.writestr("postgres.txz", data.getvalue())
    install_extensions(archive, root)
    assert (share / "extension/btree_gist.control").read_bytes() == b"control"
    assert (share / "extension/unaccent--1.0.sql").read_bytes() == b"sql"
    assert (share / "tsearch_data/unaccent.rules").read_bytes() == b"rules"
    assert (library / "btree_gist.dll").read_bytes() == b"library"
    assert (library / "unaccent.dll").read_bytes() == b"library"
    if nested:
        assert not (root / "share/extension").exists()
        assert not (root / "lib/btree_gist.dll").exists()
