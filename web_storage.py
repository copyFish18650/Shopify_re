"""Local persistence. Credentials and task payloads are protected by Windows DPAPI."""
import base64
import ctypes
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def protect(raw, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("此本机版使用 Windows 凭据保护，请在 Windows 中启动")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def seal(value):
    return base64.b64encode(protect(json.dumps(value, ensure_ascii=False).encode("utf-8"))).decode("ascii")


def unseal(value):
    return json.loads(protect(base64.b64decode(value), decrypt=True).decode("utf-8"))


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, stage TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}', prompt TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                time TEXT NOT NULL, message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS logs_job ON logs(job_id, id);
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, job_ids TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS queue_state (id INTEGER PRIMARY KEY CHECK(id=1), blocked_job_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mailboxes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                email_hash TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL
            );
        """)
        self.db.execute("UPDATE jobs SET state='interrupted', prompt='', updated=? WHERE state IN ('queued','preparing','running','waiting','stopping')", (now(),))
        self.db.commit()

    def get_settings(self):
        with self.lock:
            row = self.db.execute("SELECT payload FROM settings WHERE id=1").fetchone()
            return unseal(row[0]) if row else {}

    def queue_blocker(self):
        with self.lock:
            row = self.db.execute("SELECT blocked_job_id FROM queue_state WHERE id=1").fetchone()
            return row[0] if row else ""

    def set_queue_blocker(self, job_id):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO queue_state VALUES(1,?)", (job_id,))

    def save_settings(self, settings):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (seal(settings),))

    def import_mailboxes(self, records):
        rows = [(str(uuid.uuid4()), hashlib.sha256(r["email"].casefold().encode("utf-8")).hexdigest(), seal(r), now()) for r in records]
        added = 0
        with self.lock, self.db:
            for row in rows:
                added += self.db.execute("INSERT OR IGNORE INTO mailboxes(id,email_hash,payload,created) VALUES(?,?,?,?)", row).rowcount
        return {"added": added, "existing": len(records) - added}

    def mailboxes(self):
        with self.lock:
            return [{"id": r["id"], "seq": r["seq"], "created": r["created"], **unseal(r["payload"])}
                    for r in self.db.execute("SELECT * FROM mailboxes ORDER BY seq")]

    def mailbox(self, mailbox_id=None, email=None):
        with self.lock:
            if mailbox_id:
                row = self.db.execute("SELECT * FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
            else:
                key = hashlib.sha256(email.casefold().encode("utf-8")).hexdigest()
                row = self.db.execute("SELECT * FROM mailboxes WHERE email_hash=?", (key,)).fetchone()
            if row is None:
                if mailbox_id:
                    raise ValueError("所选邮箱不存在，请刷新邮箱库")
                return None
            return {"id": row["id"], "seq": row["seq"], "created": row["created"], **unseal(row["payload"])}

    def add(self, job_id, payload):
        timestamp = now()
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs(id,state,stage,created,updated,payload) VALUES(?,?,?,?,?,?)",
                            (job_id, "queued", "等待开始", timestamp, timestamp, seal(payload)))

    def set_mailbox_state(self, mailbox_id, state):
        with self.lock, self.db:
            row = self.db.execute("SELECT payload FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
            if row is None:
                raise ValueError("所选邮箱不存在，请刷新邮箱库")
            payload = unseal(row[0])
            payload.update(usage_state=state, usage_updated=now(), usage_revision=payload.get("usage_revision", 0) + 1)
            self.db.execute("UPDATE mailboxes SET payload=? WHERE id=?", (seal(payload), mailbox_id))

    def add_batch(self, batch_id, fingerprint, entries):
        timestamp = now()
        # Encrypt everything before the transaction; either the entire batch exists or none does.
        rows = [(job_id, "queued", "等待开始", timestamp, timestamp, seal(payload)) for job_id, payload in entries]
        with self.lock, self.db:
            self.db.executemany("INSERT INTO jobs(id,state,stage,created,updated,payload) VALUES(?,?,?,?,?,?)", rows)
            self.db.execute("INSERT INTO batches VALUES(?,?,?)", (batch_id, fingerprint, json.dumps([r[0] for r in entries])))

    def batch(self, batch_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError("批次不存在")
            return {"id": row["id"], "fingerprint": row["fingerprint"], "job_ids": json.loads(row["job_ids"])}

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("任务不存在")
            data = dict(row)
            data["payload"] = unseal(data["payload"])
            data["result"] = json.loads(data["result"])
            return data

    def jobs(self, limit=200):
        with self.lock:
            sql = "SELECT id FROM jobs ORDER BY created DESC, rowid DESC"
            ids = [r[0] for r in self.db.execute(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ())]
            return [self.get(job_id) for job_id in ids]

    def update(self, job_id, **changes):
        allowed = {"state", "stage", "payload", "result", "prompt"}
        if not changes.keys() <= allowed:
            raise ValueError("Invalid job update")
        if "payload" in changes:
            changes["payload"] = seal(changes["payload"])
        if "result" in changes:
            changes["result"] = json.dumps(changes["result"], ensure_ascii=False)
        changes["updated"] = now()
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(key + "=?" for key in changes) + " WHERE id=?",
                            list(changes.values()) + [job_id])

    def log(self, job_id, message):
        with self.lock, self.db:
            self.db.execute("INSERT INTO logs(job_id,time,message) VALUES(?,?,?)", (job_id, now(), message[:8000]))

    def logs(self, job_id, after=0):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id,time,message FROM logs WHERE job_id=? AND id>? ORDER BY id LIMIT 500", (job_id, after)
            )]

    def close(self):
        with self.lock:
            self.db.close()
