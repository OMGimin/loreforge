import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f"{prefix}_{uuid4().hex}"


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS objects(kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id));
              CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, kind TEXT, target TEXT, fingerprint TEXT);
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, value TEXT);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def put(self, kind, id, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (kind, id, json.dumps(value, ensure_ascii=False)))

    def get(self, kind, id):
        with self.connect() as db:
            row = db.execute("SELECT value FROM objects WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.connect() as db:
            rows = db.execute("SELECT value FROM objects WHERE kind=? ORDER BY rowid DESC", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def request(self, id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def claim(self, id, kind, target, fingerprint):
        with self.connect() as db:
            db.execute("INSERT INTO requests VALUES(?,?,?,?)", (id, kind, target, fingerprint))

    def event(self, run, message):
        value = {"run_id": run["id"], "stage": run["stage"], "message": message, "status": run["status"]}
        with self.connect() as db:
            cursor = db.execute("INSERT INTO events(run_id,value) VALUES(?,?)", (run["id"], json.dumps(value, ensure_ascii=False)))
            id = cursor.lastrowid
        return {"id": id, **value}

    def events(self, run_id, after=0):
        with self.connect() as db:
            rows = db.execute("SELECT id,value FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after)).fetchall()
        return [{"id": row[0], **json.loads(row[1])} for row in rows]

    def interrupt(self):
        for kind in ("run", "analysis"):
            for obj in self.all(kind):
                if obj.get("status") == "running":
                    obj.update(status="interrupted", stage="interrupted", error="서버가 재시작되어 작업이 중단되었습니다. 자동 재개하지 않습니다.")
                    self.put(kind, obj["id"], obj)
                    if kind == "run":
                        self.event(obj, obj["error"])
