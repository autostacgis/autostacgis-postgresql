import argparse

from .config import Settings


def main():
    parser = argparse.ArgumentParser(description="Independent AutostacGIS PostgreSQL service")
    parser.add_argument("command", choices=["serve", "worker", "migrate", "execute"])
    parser.add_argument("job_id", nargs="?")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "autostacgis_postgresql.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
        )
    elif args.command == "migrate":
        from pypgstac.db import PgstacDB
        from pypgstac.migrate import Migrate

        from .store import Store

        with PgstacDB(dsn=settings.database_url) as db:
            Migrate(db).run_migration("0.9.9")
        Store(settings).initialize()
    elif args.command == "worker":
        from .worker import run

        run(settings)
    elif args.command == "execute":
        from .worker import execute_job

        if not args.job_id:
            parser.error("execute requires a job ID")
        execute_job(args.job_id, settings)


if __name__ == "__main__":
    main()
