# AutostacGIS PostgreSQL

Independent STAC API backed by PostgreSQL, PostGIS, pgSTAC 0.9.9, STAC FastAPI PgSTAC 7.1.0 and TiTiler 1.2.1. The published compatible releases are pinned together. Standard collections, items, GET/POST search, CQL2, sorting, fields, queryables and transactions come from STAC FastAPI rather than a custom STAC implementation.

## Start

Check out `autostacgis-python` and `autostacgis-plugins` beside this repository, then:

```sh
python -m pip install build
python scripts/build_engine_wheels.py
cp .env.example .env
# Set unique POSTGRES_PASSWORD and AUTOSTACGIS_API_TOKEN in .env.
mkdir -p incoming
docker compose up --build -d
```

The API is at `http://127.0.0.1:8080`, OpenAPI at `/api.html`, search at `/search`, and COG metadata/tiles at `/cog`. The COG proxy accepts HTTP(S) assets and serves previews through this backend rather than sending private catalog URLs to a public tiler. Compose starts PostgreSQL, performs a one-shot migration, then starts the API and a durable job worker. The database is not published to the host. Volumes retain catalog, configurations, jobs and output assets. Use `docker compose down` to stop; adding `-v` deliberately removes persistent data. For an existing PostgreSQL/PostGIS installation, install the wheels and this package, set `AUTOSTACGIS_DATABASE_URL`, then run `autostacgis-server migrate`, `autostacgis-server serve`, and `autostacgis-server worker` separately.

The desktop application bundles service, engine, and plugin wheels. Its embedded mode downloads checksum-pinned native PostgreSQL/PostGIS, matching contrib extensions, and a managed Python runtime; it starts the API and worker only after the user chooses Start and connect. Docker remains an optional independent deployment. It can connect to an independently deployed instance or any standard STAC API instead.

## Native embedded mode

The Electron application manages a private native PostgreSQL cluster, API and worker through `python -m autostacgis_postgresql.embedded`. A prepared PostgreSQL runtime must include PostGIS, `btree_gist`, and `unaccent`; the desktop runtime manifest pins compatible PostgreSQL 16.13 bundles and contrib modules. It uses Serverpod's PostgreSQL/PostGIS binaries and Zonky's matching contrib files. pgembed alone does not provide PostGIS.

For a manually prepared native runtime:

```sh
export AUTOSTACGIS_DATABASE_PASSWORD=your-private-database-password
export AUTOSTACGIS_API_TOKEN=your-private-api-token
python -m autostacgis_postgresql.embedded --postgres-root /path/to/postgres --directory /path/to/private-state
```

The owner chooses available loopback ports, retains the API port across restarts so published asset URLs remain valid, prints its API URL after readiness, and retains database/results across starts. Send `stop` on stdin or interrupt the process to stop its owned services; loss of the owning stdin also stops them. An exclusive cluster lock prevents concurrent owners. This does not touch independently deployed PostgreSQL or Compose volumes. Native input providers use host paths directly; the Docker `/data/incoming` path applies only to the Compose deployment.

## API

Standard STAC reads and search are public. All catalog mutations and `/autostacgis/*` endpoints require `Authorization: Bearer <AUTOSTACGIS_API_TOKEN>`; an empty server token disables those endpoints. Use HTTPS and scoped credentials at your reverse proxy for a shared deployment. CORS origins are an explicit JSON array in `AUTOSTACGIS_CORS_ORIGINS`.

- `GET /autostacgis/plugins`: every installed plugin grouped by capability, raw defaults, settings JSON Schema, dependencies and processing/solver contracts. Unknown upstream plugin parameters remain editable as JSON.
- `GET /autostacgis/configurations`, `PUT /autostacgis/configurations/{name}`: validated shared configuration documents.
- `POST /autostacgis/validate`, `POST /autostacgis/plan`: `{configuration, request?, inputs?, trigger?}`. `inputs` is an optional array of selected STAC Items; omission discovers through configured metadata providers.
- `POST /autostacgis/jobs`: queue the same invocation; returns a durable UUID. `GET /autostacgis/jobs` and `GET /autostacgis/jobs/{id}` report state/results. `DELETE` cancels queued/running jobs; the supervising worker terminates the workflow process group.
- A job with `trigger: "apscheduler"` or `trigger: "watchdog"` starts that enabled trigger using its raw plugin settings. Trigger jobs discover inputs on each invocation, publish each run back into the STAC catalog, and retain their most recent result. Cancel stops the managed trigger and its processing descendants. They consume one worker each; run additional workers for concurrent workflows.
- Published product IDs are namespaced by job; original IDs are retained in `properties.autostacgis:source_item_id`. Final goal validation runs again after publication, and a conflicting identity/collection/URL constraint fails rather than reporting an unsatisfied goal as successful. Completed outputs are durably encoded as COG or Parquet, ingested into `autostacgis-results`, and served under `/assets/{job}/{file}` for both clients. Execution runs separately from HTTP requests. The PostgreSQL queue uses `FOR UPDATE SKIP LOCKED`; add workers to consume independent jobs. A crashed supervisor leaves its running rows visible for explicit cancellation/retry; workflows with side effects are not silently replayed.

API catalogs accept only compact references to installed plugin names, not arbitrary Python module imports. This API grants trusted users the ability to run configured processing and access mounted data; it is not a multi-tenant execution sandbox. The configured `AUTOSTACGIS_INPUT_DIRECTORY` is mounted read-only at `/data/incoming`; the desktop app provides an input-folder chooser. Use those backend paths for file providers and watchers. On Docker Desktop, `backend: polling` can be used when native file events are not propagated through a shared folder. Source files/models must be available to the worker at the configured locations. Standard STAC endpoints remain usable when AutostacGIS extensions are absent from a remote API.

## Development and validation

```sh
pip install -e ../autostacgis-python -e '../autostacgis-plugins[triggers]' -e '.[dev]'
AUTOSTACGIS_TEST_DSN=postgresql://... pytest
ruff check src tests scripts
ruff format --check src tests scripts
```

Integration tests create collections/items through real STAC transactions, exercise search, configurations, planning, queue claims/cancellation and completed-job publication. They require a migrated disposable database. CI supplies one.

Upstream references: [STAC FastAPI PgSTAC](https://github.com/stac-utils/stac-fastapi-pgstac), [pgSTAC](https://github.com/stac-utils/pgstac/tree/v0.9.9), [STAC API specification](https://github.com/radiantearth/stac-api-spec).


## Plugins, metadata contracts and desktop releases

`GET /autostacgis/plugins/available` retrieves the configured online plugin manifest. Authenticated `POST /autostacgis/plugins/install` accepts `{name, version}`; `POST /autostacgis/plugins/install-file` accepts `{filename, source}` for a single Python file. Installs live in `AUTOSTACGIS_DATA_DIR/plugins` in the `autostacgis_plugins` namespace and are available to workers. Set `AUTOSTACGIS_PLUGIN_REGISTRY_URL` for another HTTPS manifest. These are executable Python installations and require trusted plugin code and the backend write token.

Configurations may include `metadata_structure` with `schemas`, `inline`, and `strict`. The engine exposes the resolved declarations to plugin contexts and validates input/output Items. Schema URLs should be HTTPS STAC Item JSON Schemas. The inline schema also describes an entire Item, with custom Item metadata nested under `properties.properties`.

`make build` builds the engine, standard plugins and service wheels. `make release-manifest VERSION=0.2.0` also generates the checksummed `backend-release.json`. Pushing a matching version tag runs `.github/workflows/release.yml` and publishes these assets to GitHub Releases. Electron downloads this feed for independent backend updates; it checks the minimum supported app version and wheel checksums before installation. PostgreSQL/PostGIS binary upgrades remain coupled to the desktop runtime manifest. The backend remains independently deployable without Electron.

## Workflow library and plugin parameters

Workflows use one JSON document with plugin types as the top-level sections. The smallest explicit selection is `{"solver":{"backward_chain":{}},"goal":{"metadata-value":{"path":"properties.ready","value":true}}}`. Plugin dependencies and runtime defaults are resolved in Python. At most one trigger is active; omission runs immediately. Use `now`, `apscheduler`, or `watchdog` to choose how a workflow starts. See the [core configuration guide](https://github.com/autostacgis/autostacgis-python/blob/main/docs/workflows.md).

Plugin descriptions come from the required Python module docstring. Defaults, parameter descriptions and schemas are supplied by the library. Both JSON views edit the same document; the GUI offers declared optional parameters with their defaults. Unsupported complex annotations remain editable as JSON.

The workflow library lists published workflows and saved copies on the connected backend. Activate loads the JSON into the workflow editor. Published workflows can be saved as named copies; saved workflows can be updated, copied, renamed or deleted. Discovery and downloads use `autostacgis.workflow_manager.WorkflowManager`, sharing HTTPS and checksum verification with plugin installation. The backend exposes `/autostacgis/workflows`, `/workflows/available`, and `/workflows/published/{id}`. Save-copy is POST, update is PUT, rename is PATCH and delete is DELETE. `AUTOSTACGIS_WORKFLOW_REGISTRY_URL` selects another registry.

## Prerelease and result saving

AutostacGIS packages stay at `0.0.1` during prerelease development. Jobs return result metadata by default. The backend does not automatically encode or copy computed payloads. Enable `process.<plugin>.outputs.<result>.save` to run that result’s configured encoder and destination; named steps may override the same fields. Explicitly saved files are served in place through the API and durable Items are indexed in pgSTAC. Transient results remain in job metadata and are not added to the persistent catalog.
