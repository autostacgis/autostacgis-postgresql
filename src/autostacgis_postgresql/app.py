from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
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
    trigger: Literal["apscheduler", "watchdog"] | None = None
    configuration: dict
    request: dict | None = None
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
        stac_fastapi_version="1.0.0",
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

    router = APIRouter(prefix="/autostacgis", dependencies=[Depends(authorize)])

    @router.get("/plugins")
    def plugin_list():
        return engine.plugins()

    @router.get("/configurations")
    def configurations():
        return store.configurations()

    @router.put("/configurations/{identifier}")
    def save_configuration(identifier: str, document: dict):
        if len(identifier) > 200:
            raise HTTPException(422, "Configuration name is too long")
        try:
            application, request, _ = engine.prepare(document, None, None, settings)
            application.make_solver(request, [])
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.save_configuration(identifier, document)

    @router.post("/validate")
    def validate_configuration(value: Invocation):
        try:
            app, request, records = engine.prepare(value.configuration, value.request, value.inputs, settings)
            app.make_solver(request, records or [])
            return {"valid": True, "solver": app.solver_name}
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/plan")
    def plan(value: Invocation):
        try:
            app, request, records = engine.prepare(value.configuration, value.request, value.inputs, settings)
            return app.plan(request, records).to_dict()
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/jobs", status_code=202)
    def create_job(value: Invocation):
        try:
            application, request, records = engine.prepare(
                value.configuration, value.request, value.inputs, settings
            )
            application.make_solver(request, records or [])
            if value.trigger:
                if value.inputs is not None:
                    raise ValueError("Trigger jobs discover their inputs afresh; do not provide fixed inputs")
                application.resolve_call("trigger", {"plugin": value.trigger})
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.create_job(value.configuration, request, value.inputs, value.trigger)

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
            root = (settings.data_dir / "results").resolve()
            target = (root / unquote(source.path[len(prefix) :])).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise HTTPException(404, "Unknown result asset")
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
    app.mount("/assets", StaticFiles(directory=settings.data_dir / "results"), name="assets")
    return app
