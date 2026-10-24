from uuid import uuid4

from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DDL = """
CREATE SCHEMA IF NOT EXISTS autostacgis;
CREATE TABLE IF NOT EXISTS autostacgis.configurations (
 id text PRIMARY KEY, document jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS autostacgis.jobs (
 id uuid PRIMARY KEY, state text NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
 configuration jsonb NOT NULL, request jsonb NOT NULL, inputs jsonb, trigger_name text,
 result jsonb, error text, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE autostacgis.jobs ADD COLUMN IF NOT EXISTS trigger_name text;
CREATE INDEX IF NOT EXISTS jobs_queue ON autostacgis.jobs (created_at) WHERE state='queued';
"""


class Store:
    def __init__(self, settings):
        self.settings = settings

    def connection(self):
        return connect(self.settings.database_url, row_factory=dict_row)

    def initialize(self):
        with self.connection() as conn:
            conn.execute(DDL)

    def configurations(self):
        with self.connection() as conn:
            return conn.execute(
                "SELECT id,document,updated_at FROM autostacgis.configurations ORDER BY id"
            ).fetchall()

    def save_configuration(self, identifier, document):
        with self.connection() as conn:
            return conn.execute(
                """INSERT INTO autostacgis.configurations(id,document) VALUES (%s,%s)
                ON CONFLICT(id) DO UPDATE SET document=EXCLUDED.document,updated_at=now()
                RETURNING id,document,updated_at""",
                (identifier, Jsonb(document)),
            ).fetchone()

    def configuration(self, identifier):
        with self.connection() as conn:
            return conn.execute(
                "SELECT id,document,updated_at FROM autostacgis.configurations WHERE id=%s", (identifier,)
            ).fetchone()

    def create_configuration(self, identifier, document):
        with self.connection() as conn:
            return conn.execute(
                "INSERT INTO autostacgis.configurations(id,document) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id,document,updated_at",
                (identifier, Jsonb(document)),
            ).fetchone()

    def update_configuration(self, identifier, document):
        with self.connection() as conn:
            return conn.execute(
                "UPDATE autostacgis.configurations SET document=%s,updated_at=now() WHERE id=%s RETURNING id,document,updated_at",
                (Jsonb(document), identifier),
            ).fetchone()

    def rename_configuration(self, identifier, name):
        with self.connection() as conn:
            return conn.execute(
                "UPDATE autostacgis.configurations SET id=%s,updated_at=now() WHERE id=%s RETURNING id,document,updated_at",
                (name, identifier),
            ).fetchone()

    def delete_configuration(self, identifier):
        with self.connection() as conn:
            return conn.execute(
                "DELETE FROM autostacgis.configurations WHERE id=%s RETURNING id", (identifier,)
            ).fetchone()

    def create_job(self, configuration, request, inputs, trigger_name=None):
        with self.connection() as conn:
            return conn.execute(
                """INSERT INTO autostacgis.jobs(id,state,configuration,request,inputs,trigger_name)
                VALUES (%s,'queued',%s,%s,%s,%s) RETURNING id,state,created_at""",
                (uuid4(), Jsonb(configuration), Jsonb(request), Jsonb(inputs), trigger_name),
            ).fetchone()

    def jobs(self):
        with self.connection() as conn:
            return conn.execute(
                "SELECT id,state,error,created_at,updated_at FROM autostacgis.jobs ORDER BY created_at DESC LIMIT 100"
            ).fetchall()

    def job(self, identifier):
        with self.connection() as conn:
            return conn.execute("SELECT * FROM autostacgis.jobs WHERE id=%s", (identifier,)).fetchone()

    def claim(self):
        with self.connection() as conn:
            return conn.execute("""UPDATE autostacgis.jobs SET state='running',updated_at=now() WHERE id=(
                SELECT id FROM autostacgis.jobs WHERE state='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                ) RETURNING *""").fetchone()

    def finish(self, identifier, state, result=None, error=None):
        with self.connection() as conn:
            conn.execute(
                """UPDATE autostacgis.jobs SET state=%s,result=%s,error=%s,updated_at=now()
                WHERE id=%s AND state='running'""",
                (state, Jsonb(result), error, identifier),
            )

    def cancel(self, identifier):
        with self.connection() as conn:
            return conn.execute(
                """UPDATE autostacgis.jobs SET state='cancelled',updated_at=now()
                WHERE id=%s AND state IN ('queued','running') RETURNING id,state""",
                (identifier,),
            ).fetchone()
