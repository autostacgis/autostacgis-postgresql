"""Durable PostgreSQL job queue; each workflow has an isolated process group."""

import os
import signal
import subprocess
import sys
from time import sleep
from uuid import UUID

from psycopg.types.json import Jsonb

from .config import Settings
from .store import Store


def execute_job(identifier, settings=None):
    from .engine import persist, prepare

    settings = settings or Settings()
    store = Store(settings)
    job = store.job(UUID(str(identifier)))
    if not job or job["state"] != "running":
        return
    diagnostic_delay = os.environ.get("AUTOSTACGIS_DEBUG_STACK_SECONDS")
    if diagnostic_delay:
        import faulthandler

        faulthandler.dump_traceback_later(float(diagnostic_delay), repeat=True)
    try:
        directory = settings.data_dir / "work" / str(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        app, request, inputs = prepare(job["configuration"], job["request"], job["inputs"], settings)
        request["work_directory"] = str(directory)
        if job.get("trigger_name"):
            from autostacgis.triggers import FileTrigger, TimedTrigger

            _, options = app.resolve_call("trigger", {"plugin": job["trigger_name"]})
            controller = (TimedTrigger if job["trigger_name"] == "apscheduler" else FileTrigger)(
                app, options, request, runner=execute_trigger
            )
            with controller:
                controller.wait()
            return
        result = app.run(request, inputs)
        try:
            outputs = persist(
                result.records,
                settings,
                identifier,
                stac=app.stac,
                validate=lambda records: (
                    result.solver.validate_outputs(result.plan, records)
                    if result.solver is not None
                    else True
                ),
            )
        finally:
            if hasattr(result, "close"):
                result.close()
        collection = {
            "type": "Collection",
            "stac_version": "1.1.0",
            "id": settings.result_collection,
            "description": "AutostacGIS workflow outputs",
            "license": "proprietary",
            "links": [],
            "extent": {"spatial": {"bbox": [[-180, -90, 180, 90]]}, "temporal": {"interval": [[None, None]]}},
        }
        with store.connection() as conn:
            conn.execute("SELECT pgstac.upsert_collection(%s)", (Jsonb(collection),))
            for item in outputs["features"]:
                conn.execute("SELECT pgstac.upsert_item(%s)", (Jsonb(item),))
        store.finish(identifier, "succeeded", outputs)
    except Exception as exc:
        store.finish(identifier, "failed", error=str(exc))
        raise
    finally:
        if diagnostic_delay:
            faulthandler.cancel_dump_traceback_later()


def execute_trigger(catalog, base_dir, request):
    from uuid import uuid4

    from .engine import persist

    settings = Settings()
    import os

    os.environ["AUTOSTACGIS_PLUGIN_DIR"] = str(settings.data_dir / "plugins")
    from autostacgis import Application

    # The native trigger supplies an already resolved, trusted catalog snapshot.
    # Compact-name validation belongs at the remote API boundary, before this snapshot.
    app = Application(catalog, base_dir=base_dir)
    request = app.request(request)
    request["work_directory"] = str(settings.data_dir / "work" / str(uuid4()))
    result = app.run(request)
    try:
        identifier = uuid4()
        outputs = persist(
            result.records,
            settings,
            identifier,
            stac=app.stac,
            validate=lambda records: (
                result.solver.validate_outputs(result.plan, records) if result.solver is not None else True
            ),
        )
        collection = {
            "type": "Collection",
            "stac_version": "1.1.0",
            "id": settings.result_collection,
            "description": "AutostacGIS workflow outputs",
            "license": "proprietary",
            "links": [],
            "extent": {"spatial": {"bbox": [[-180, -90, 180, 90]]}, "temporal": {"interval": [[None, None]]}},
        }
        with Store(settings).connection() as conn:
            conn.execute("SELECT pgstac.upsert_collection(%s)", (Jsonb(collection),))
            for item in outputs["features"]:
                conn.execute("SELECT pgstac.upsert_item(%s)", (Jsonb(item),))
            if os.environ.get("AUTOSTACGIS_TRIGGER_JOB_ID"):
                conn.execute(
                    "UPDATE autostacgis.jobs SET result=%s,updated_at=now() WHERE id=%s AND state='running'",
                    (Jsonb(outputs), os.environ["AUTOSTACGIS_TRIGGER_JOB_ID"]),
                )
    finally:
        if hasattr(result, "close"):
            result.close()


def supervise(job, settings):
    store = Store(settings)
    directory = settings.data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "AUTOSTACGIS_DATABASE_URL": settings.database_url,
        "AUTOSTACGIS_DATA_DIR": str(settings.data_dir.resolve()),
        "AUTOSTACGIS_PUBLIC_URL": settings.public_url,
        "AUTOSTACGIS_TRIGGER_JOB_ID": str(job["id"]),
        "AUTOSTACGIS_RESULT_COLLECTION": settings.result_collection,
    }
    with (directory / (str(job["id"]) + ".log")).open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "autostacgis_postgresql.cli", "execute", str(job["id"])],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            while child.poll() is None:
                current = store.job(job["id"])
                if current["state"] == "cancelled":
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], check=False)
                    else:
                        os.killpg(child.pid, signal.SIGTERM)
                    break
                sleep(settings.job_poll_seconds)
        finally:
            if child.poll() is None:
                if os.name != "nt":
                    os.killpg(child.pid, signal.SIGTERM)
                else:
                    child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(child.pid, signal.SIGKILL)
                    else:
                        child.kill()
            child.wait()
        if store.job(job["id"])["state"] == "running":
            store.finish(
                job["id"],
                "failed",
                error="Worker process exited before reporting completion; inspect its log",
            )


def run(settings=None):
    settings = settings or Settings()
    store = Store(settings)
    store.initialize()
    if os.environ.get("AUTOSTACGIS_MANAGED") == "1":
        from _thread import interrupt_main
        from threading import Thread

        def stop_input():
            for line in sys.stdin:
                if line.strip() == "stop":
                    break
            interrupt_main()

        Thread(target=stop_input, daemon=True).start()
    try:
        while True:
            job = store.claim()
            if job:
                supervise(job, settings)
            else:
                sleep(settings.job_poll_seconds)
    except KeyboardInterrupt:
        return
