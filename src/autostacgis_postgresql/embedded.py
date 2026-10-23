"""Own a private native PostgreSQL/PostGIS cluster and its API/worker processes."""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from urllib.parse import quote
from urllib.request import urlopen

import psycopg
from psycopg import sql

from .config import Settings


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def exclusive_cluster(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "owner.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("This embedded backend is already owned by another process") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


class EmbeddedBackend:
    def __init__(self, postgres_root, directory, password, token, api_port=0):
        self.root = Path(postgres_root).resolve()
        self.directory = Path(directory).resolve()
        self.cluster = self.directory / "postgres-data"
        self.password, self.token = password, token
        port_file = self.directory / "api-port.json"
        self.api_port = api_port or (json.loads(port_file.read_text()) if port_file.exists() else free_port())
        if not isinstance(self.api_port, int) or not 1 <= self.api_port <= 65535:
            raise RuntimeError("Invalid embedded API port in api-port.json")
        self.port = free_port()
        while self.port == self.api_port:
            self.port = free_port()
        self.children = []
        self.logs = []
        self.url = f"http://127.0.0.1:{self.api_port}"
        self.dsn = f"postgresql://autostacgis:{quote(password, safe='')}@127.0.0.1:{self.port}/autostacgis"
        self.stopped = Event()

    def binary(self, name):
        path = self.root / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if not path.is_file():
            raise RuntimeError(f"Native PostgreSQL runtime is missing {name}; download the backend first")
        return str(path)

    def start_child(self, command, name, env=None):
        log = (self.directory / (name + ".log")).open("ab", buffering=0)
        self.logs.append(log)
        child = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        self.children.append((name, child))
        return child

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.password or not self.token:
            raise RuntimeError("Embedded database and API credentials must be configured")
        if not (self.cluster / "PG_VERSION").exists():
            with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as password_file:
                password_file.write(self.password + "\n")
            try:
                subprocess.run(
                    [
                        self.binary("initdb"),
                        "-D",
                        str(self.cluster),
                        "-U",
                        "autostacgis",
                        "--encoding=UTF8",
                        "--locale=C",
                        "--auth-local=trust",
                        "--auth-host=scram-sha-256",
                        "--pwfile=" + password_file.name,
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            finally:
                Path(password_file.name).unlink(missing_ok=True)
        postgres = self.start_child(
            [
                self.binary("postgres"),
                "-D",
                str(self.cluster),
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-c",
                "unix_socket_directories=",
                "-c",
                "max_connections=40",
            ],
            "postgres",
        )
        deadline = monotonic() + 45
        while True:
            if postgres.poll() is not None:
                raise RuntimeError("Embedded PostgreSQL exited; see postgres.log")
            try:
                with psycopg.connect(
                    self.dsn.rsplit("/", 1)[0] + "/postgres", connect_timeout=1, autocommit=True
                ) as conn:
                    if not conn.execute("SELECT 1 FROM pg_database WHERE datname='autostacgis'").fetchone():
                        conn.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier("autostacgis")))
                break
            except psycopg.OperationalError:
                if monotonic() > deadline or self.stopped.is_set():
                    raise RuntimeError("Embedded PostgreSQL did not become ready") from None
                sleep(0.1)
        from pypgstac.db import PgstacDB
        from pypgstac.migrate import Migrate

        from .store import Store

        with PgstacDB(dsn=self.dsn) as db:
            Migrate(db).run_migration("0.9.9")
        settings = Settings(
            database_url=self.dsn, data_dir=self.directory / "data", api_token=self.token, public_url=self.url
        )
        Store(settings).initialize()
        env = {
            **os.environ,
            "AUTOSTACGIS_DATABASE_URL": self.dsn,
            "AUTOSTACGIS_DATA_DIR": str(settings.data_dir),
            "AUTOSTACGIS_API_TOKEN": self.token,
            "AUTOSTACGIS_PUBLIC_URL": self.url,
            "AUTOSTACGIS_MANAGED": "1",
        }
        api = self.start_child(
            [
                sys.executable,
                "-m",
                "autostacgis_postgresql.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.api_port),
            ],
            "api",
            env,
        )
        self.start_child([sys.executable, "-m", "autostacgis_postgresql.cli", "worker"], "worker", env)
        deadline = monotonic() + 60
        while True:
            if api.poll() is not None:
                raise RuntimeError("Embedded API exited; see api.log")
            try:
                with urlopen(self.url + "/_mgmt/ping", timeout=1) as response:
                    if response.status == 200:
                        # Published Item asset URLs must remain valid after restart.
                        (self.directory / "api-port.json").write_text(json.dumps(self.api_port))
                        return self
            except OSError:
                pass
            if monotonic() > deadline or self.stopped.is_set():
                raise RuntimeError("Embedded API did not become ready")
            sleep(0.1)

    def stop(self):
        self.stopped.set()
        for name, child in reversed(self.children):
            if child.poll() is not None:
                continue
            if name == "postgres":
                subprocess.run(
                    [self.binary("pg_ctl"), "-D", str(self.cluster), "stop", "-m", "fast", "-w"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
            else:
                try:
                    child.stdin.write(b"stop\n")
                    child.stdin.flush()
                    if name == "api":
                        # Uvicorn does not consume the managed worker's stdin protocol.
                        if os.name == "nt":
                            child.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            child.send_signal(signal.SIGTERM)
                    child.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        os.killpg(child.pid, signal.SIGKILL)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        self.children.clear()
        for log in self.logs:
            log.close()
        self.logs.clear()


def main():
    parser = argparse.ArgumentParser(description="Private embedded AutostacGIS service")
    parser.add_argument("--postgres-root", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    backend = EmbeddedBackend(
        args.postgres_root,
        args.directory,
        os.environ.get("AUTOSTACGIS_DATABASE_PASSWORD", ""),
        os.environ.get("AUTOSTACGIS_API_TOKEN", ""),
        args.port,
    )

    def stop_input():
        # EOF also stops the service if its owning desktop process disappears.
        for line in sys.stdin:
            if line.strip() == "stop":
                break
        backend.stopped.set()

    Thread(target=stop_input, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: backend.stopped.set())
    with exclusive_cluster(backend.directory):
        try:
            backend.start()
            print("AUTOSTACGIS_READY:" + json.dumps({"url": backend.url}), flush=True)
            while not backend.stopped.wait(0.2):
                if any(child.poll() is not None for _, child in backend.children):
                    raise RuntimeError("An embedded service process exited; see backend logs")
        except KeyboardInterrupt:
            pass
        finally:
            backend.stop()


if __name__ == "__main__":
    main()
