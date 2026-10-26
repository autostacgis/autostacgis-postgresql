from contextlib import asynccontextmanager
from hmac import compare_digest
from urllib.parse import unquote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from stac_fastapi.pgstac.app import instantiate_api
from stac_fastapi.pgstac.config import PostgresSettings
from stac_fastapi.pgstac.config import Settings as StacSettings
from stac_fastapi.pgstac.db import close_db_connection, connect_to_db
from stac_fastapi.pgstac.models.extensions import Extensions

from . import engine
from .config import Settings
from .store import Store


class DatabaseSettings(PostgresSettings):
    database_url: str

    @property
    def connection_string(self):
        return self.database_url


class Invocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configuration: dict
    inputs: list[dict] | None = Field(default=None, max_length=1000)


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "results").mkdir(exist_ok=True)
    uri = urlsplit(settings.database_url)
    postgres = DatabaseSettings(
        database_url=settings.database_url,
        pguser=unquote(uri.username or ""),
        pgpassword=unquote(uri.password or ""),
        pghost=uri.hostname or "localhost",
        pgport=uri.port or 5432,
        pgdatabase=unquote(uri.path.lstrip("/")),
    )

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        await connect_to_db(app, postgres_settings=postgres, add_write_connection_pool=True)
        try:
            yield
        finally:
            await close_db_connection(app)

    stac_settings = StacSettings(
        stac_fastapi_title="AutostacGIS STAC API",
        stac_fastapi_description="pgSTAC catalog and AutostacGIS workflows",
        stac_fastapi_landing_id="autostacgis",
        stac_fastapi_version="0.0.1",
        enable_transactions_extensions=True,
        cors_origins=settings.cors_origins,
        cors_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        cors_headers=["Authorization", "Content-Type"],
    )
    api = instantiate_api(
        settings=stac_settings, extensions=Extensions(settings=stac_settings), lifespan=lifespan
    )
    app = api.app
    app.state.store = store
    app.state.autostacgis_settings = settings

    def authorize(request: Request):
        token = settings.api_token.get_secret_value()
        supplied = request.headers.get("authorization", "")
        if not token or not compare_digest(supplied, "Bearer " + token):
            raise HTTPException(
                401, "A configured AutostacGIS API token is required", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.middleware("http")
    async def secure_writes(request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path not in ("/search",):
            try:
                authorize(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return await call_next(request)

    import os

    os.environ["AUTOSTACGIS_PLUGIN_DIR"] = str(settings.data_dir / "plugins")

    router = APIRouter(prefix="/autostacgis", dependencies=[Depends(authorize)])

    @router.get("/plugins")
    def plugin_list():
        return engine.plugins()

    def plugin_manager():
        from autostacgis.plugin_manager import PluginManager

        return PluginManager(settings.data_dir / "plugins", settings.plugin_registry_url)

    @router.get("/plugins/available")
    def available_plugins():
        try:
            return plugin_manager().catalog()
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/plugins/install")
    def install_plugin(value: dict):
        try:
            return plugin_manager().install(value["name"], value.get("version"))
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/plugins/install-file")
    def install_plugin_file(value: dict):
        try:
            return plugin_manager().install_file(value["filename"], value["source"])
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc

    def workflow_manager():
        from autostacgis.workflow_manager import WorkflowManager

        return WorkflowManager(settings.workflow_registry_url)

    def workflow_name(name):
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 200
            or any(ord(c) < 32 or c in "/\\" for c in name)
        ):
            raise HTTPException(422, "Use a workflow name of 1–200 characters without slashes")
        return name.strip()

    @router.get("/workflows/available")
    def published_workflows():
        try:
            return workflow_manager().catalog()
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.get("/workflows/published/{identifier}")
    def published_workflow(identifier: str, version: str | None = None):
        try:
            return workflow_manager().get(identifier, version)
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.get("/workflows")
    def saved_workflows():
        return store.configurations()

    @router.post("/workflows", status_code=201)
    def copy_workflow(value: dict):
        name = workflow_name(value.get("name"))
        document = value.get("document")
        try:
            application, request, _ = engine.prepare(document, None, settings)
            application.make_solver(request, [])
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        result = store.create_configuration(name, document)
        if result is None:
            raise HTTPException(409, "A saved workflow already uses that name")
        return result

    @router.get("/workflows/{identifier}")
    def saved_workflow(identifier: str):
        result = store.configuration(identifier)
        if result is None:
            raise HTTPException(404, "Unknown saved workflow")
        return result

    @router.put("/workflows/{identifier}")
    def update_workflow(identifier: str, document: dict):
        workflow_name(identifier)
        try:
            application, request, _ = engine.prepare(document, None, settings)
            application.make_solver(request, [])
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        result = store.update_configuration(identifier, document)
        if result is None:
            raise HTTPException(404, "Unknown saved workflow")
        return result

    @router.patch("/workflows/{identifier}")
    def rename_workflow(identifier: str, value: dict):
        from psycopg.errors import UniqueViolation

        try:
            result = store.rename_configuration(identifier, workflow_name(value.get("name")))
        except UniqueViolation as exc:
            raise HTTPException(409, "A saved workflow already uses that name") from exc
        if result is None:
            raise HTTPException(404, "Unknown saved workflow")
        return result

    @router.delete("/workflows/{identifier}")
    def delete_workflow(identifier: str):
        result = store.delete_configuration(identifier)
        if result is None:
            raise HTTPException(404, "Unknown saved workflow")
        return result

    @router.post("/validate")
    def validate_configuration(value: Invocation):
        try:
            app, request, records = engine.prepare(value.configuration, value.inputs, settings)
            app.make_solver(request, records or [])
            return {"valid": True, "solver": app.solver_name}
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/plan")
    def plan(value: Invocation):
        try:
            app, request, records = engine.prepare(value.configuration, value.inputs, settings)
            return app.plan(inputs=records).to_dict()
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/jobs", status_code=202)
    def create_job(value: Invocation):
        try:
            application, request, records = engine.prepare(value.configuration, value.inputs, settings)
            application.make_solver(request, records or [])
            configured_trigger = application.trigger_name
            if configured_trigger and configured_trigger != "now":
                if configured_trigger not in ("apscheduler", "watchdog"):
                    raise ValueError("This trigger runs through the local Python CLI")
            if configured_trigger != "now":
                if value.inputs is not None:
                    raise ValueError("Trigger jobs discover their inputs afresh; do not provide fixed inputs")
                application.resolve_call("trigger", {"plugin": configured_trigger})
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.create_job(
            value.configuration,
            request,
            value.inputs,
            configured_trigger if configured_trigger != "now" else None,
        )

    @router.get("/jobs")
    def jobs():
        return store.jobs()

    @router.get("/jobs/{identifier}")
    def job(identifier: UUID):
        value = store.job(identifier)
        if not value:
            raise HTTPException(404, "Unknown job")
        return value

    @router.delete("/jobs/{identifier}")
    def cancel(identifier: UUID):
        value = store.cancel(identifier)
        if not value:
            raise HTTPException(409, "Job is missing or already finished")
        return value

    from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
    from titiler.core.factory import TilerFactory

    def raster_url(url: str = Query(...)):
        if urlsplit(url).scheme not in ("http", "https"):
            raise HTTPException(422, "Raster tiles require an HTTP(S) asset URL")
        public = urlsplit(settings.public_url)
        source = urlsplit(url)
        prefix = public.path.rstrip("/") + "/assets/"
        if (source.scheme, source.netloc) == (public.scheme, public.netloc) and source.path.startswith(
            prefix
        ):
            parts = unquote(source.path[len(prefix) :]).split("/")
            if len(parts) != 2:
                raise HTTPException(404, "Unknown result asset")
            try:
                target = engine.asset_file(settings, parts[0], parts[1])
            except FileNotFoundError:
                raise HTTPException(404, "Unknown result asset") from None
            # Read our own immutable artifacts directly; avoid an HTTP round trip
            # into the same process while GDAL is opening a dataset.
            return str(target)
        return url

    tiler = TilerFactory(
        router_prefix="/cog",
        path_dependency=raster_url,
        environment_dependency=lambda: {
            "GDAL_HTTP_TIMEOUT": 30,
            "GDAL_HTTP_CONNECTTIMEOUT": 10,
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        },
    )
    app.include_router(tiler.router, prefix="/cog", tags=["Raster preview"])
    add_exception_handlers(app, DEFAULT_STATUS_CODES)
    app.include_router(router)

    @app.get("/assets/{identifier}/{name}")
    def result_asset(identifier: UUID, name: str):
        try:
            return FileResponse(engine.asset_file(settings, identifier, name))
        except FileNotFoundError:
            raise HTTPException(404, "Unknown result asset") from None

    return app
