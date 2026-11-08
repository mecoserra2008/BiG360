"""Message bus for the real-time layer (R1.1).

Interface (`Bus`) with two implementations:
* `SqliteBus` (default): one SQLite file in WAL mode, data/realtime/bus.sqlite. Many readers (the API process reads it
  read-only for the live overlay + SSE), one writer process (the realtime supervisor). Write transactions use
  BEGIN IMMEDIATE + busy_timeout so concurrent writers serialise instead of failing.
* `RedisBus`: stub with the same interface for a later swap (Redis Streams); every method raises NotImplementedError.
  `get_bus()` picks the backend from env BIG360_BUS (sqlite | redis; default sqlite).

Semantics
* Message ids are ONE global, strictly increasing integer sequence across topics (SQLite AUTOINCREMENT): the SSE
  endpoint uses it as `id:` / Last-Event-ID, consumer offsets are ids.
* Idempotent publish: (topic, key) is recorded in `seen_keys`; publishing a key seen before (within the dedupe
  retention, default 30 days) is a no-op that returns None. Producers use natural keys (accession number, NSM
  disclosure id, url hash, release_event_id|series_id ...). `force=True` publishes a new version anyway.
* Consumer groups: `offsets(group, topic) = last committed id`. `poll(group, topics)` returns messages after the
  committed offsets, `commit()` advances them (monotonic). Messages are delivered AT LEAST ONCE: anything not committed
  is redelivered by the next poll (also after a crash), so handlers must be idempotent.
* Failures: `nack(group, msg, error)` counts delivery attempts; after `max_attempts` the message is copied to the
  dead-letter table and the offset moves past it (returns True). Dead letters can be listed and re-published.
* Replay: `read(topics, after_id)` reads any range still retained; `seek(group, topic, id)` rewinds a group.
* Retention: per-topic max age + minimum kept, optional compaction (keep only the newest message per `ckey`), never
  deleting messages a protected group (the lake compactor) has not committed yet; seen_keys / attempts are pruned too.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

#: public topics (SSE / live endpoints) and internal raw topics written by the stream workers
TOPICS = ("filings", "news", "events", "macro", "alerts", "signals", "news.item", "news.story", "quotes.tick",
          "quotes.session")
RAW_TOPICS = ("raw.filings", "raw.news", "raw.events", "raw.macro")

#: default retention per topic: (max_age_hours, keep_min, compact_by_ckey)
DEFAULT_RETENTION: dict[str, tuple[float, int, bool]] = {
    "raw.filings": (72, 1000, False), "raw.news": (72, 1000, False), "raw.events": (48, 2000, False),
    "raw.macro": (24 * 14, 500, False),
    "filings": (24 * 14, 5000, False), "news": (24 * 7, 5000, False), "events": (72, 5000, False),
    "macro": (24 * 60, 2000, False), "alerts": (24 * 30, 2000, False), "signals": (24 * 7, 2000, True),
    # roadmap v3 N2: news desk output (the hot hour files + intraday DuckDB keep 7 days; the bus 72 h)
    "news.item": (72, 5000, False), "news.story": (72, 2000, True),
    # workstream B: delayed quotes (durable in data/realtime/quotes.sqlite; the quotes service prunes them itself)
    "quotes.tick": (6, 50, False), "quotes.session": (48, 50, False),
}
DEDUPE_DAYS = 30


def _now() -> float:
    return time.time()


def to_iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Envelope:
    """A message to publish."""

    topic: str
    key: str
    payload: dict[str, Any]
    type: str = ""
    ts_source: datetime | None = None   # when the source published it (UTC)
    ckey: str | None = None             # compaction key (keep newest per ckey on compacted topics)
    force: bool = False                 # publish even if (topic, key) was seen before (new version)
    origin: str | None = None           # producing stream / consumer (metrics only, not stored)


@dataclass
class Message:
    id: int
    topic: str
    key: str
    type: str
    ts_source: str | None    # ISO UTC
    ts_ingest: float         # epoch seconds, first seen by the bus (knowledge time)
    payload: dict[str, Any] = field(default_factory=dict)
    ckey: str | None = None

    @property
    def ingest_dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ingest, UTC)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "topic": self.topic, "key": self.key, "type": self.type, "ts_source": self.ts_source,
                "ts_ingest": datetime.fromtimestamp(self.ts_ingest, UTC).isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"), "payload": self.payload}


class Bus(ABC):
    """Bus interface (see module docstring). All methods are synchronous and thread-safe."""

    # ---- produce
    @abstractmethod
    def publish_many(self, items: Sequence[Envelope]) -> list[int | None]: ...

    def publish(self, topic: str, key: str, payload: dict[str, Any], *, type: str = "",  # noqa: A002
                ts_source: datetime | None = None, ckey: str | None = None, force: bool = False) -> int | None:
        return self.publish_many([Envelope(topic, key, payload, type, ts_source, ckey, force)])[0]

    # ---- read / replay
    @abstractmethod
    def read(self, topics: Sequence[str] | None, after_id: int = 0, limit: int = 500) -> list[Message]: ...

    @abstractmethod
    def latest(self, topic: str, n: int = 50, where: dict[str, Any] | None = None,
               types: Sequence[str] | None = None) -> list[Message]: ...

    @abstractmethod
    def head_id(self) -> int: ...

    # ---- consumer groups
    @abstractmethod
    def poll(self, group: str, topics: Sequence[str], limit: int = 200) -> list[Message]: ...

    @abstractmethod
    def commit(self, group: str, topic: str, msg_id: int) -> None: ...

    @abstractmethod
    def nack(self, group: str, msg: Message, error: str, max_attempts: int = 5) -> bool: ...

    @abstractmethod
    def seek(self, group: str, topic: str, msg_id: int) -> None: ...

    @abstractmethod
    def offsets(self, group: str | None = None) -> dict[str, dict[str, int]]: ...

    # ---- dead letters
    @abstractmethod
    def dead_letters(self, group: str | None = None, limit: int = 100) -> list[dict[str, Any]]: ...

    @abstractmethod
    def requeue_dead(self, dlq_ids: Iterable[int]) -> int: ...

    # ---- ops
    @abstractmethod
    def retention(self, policies: dict[str, tuple[float, int, bool]] | None = None,
                  protect_groups: Sequence[str] = ("lake",), dedupe_days: float = DEDUPE_DAYS) -> dict[str, int]: ...

    @abstractmethod
    def stats(self) -> dict[str, Any]: ...

    def close(self) -> None:  # pragma: no cover - default no-op
        pass


# ================================================================================================ SQLite
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    key TEXT NOT NULL,
    ckey TEXT,
    type TEXT NOT NULL DEFAULT '',
    ts_source TEXT,
    ts_ingest REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_topic_id ON messages(topic, id);
CREATE INDEX IF NOT EXISTS ix_messages_ckey ON messages(topic, ckey) WHERE ckey IS NOT NULL;
CREATE TABLE IF NOT EXISTS seen_keys (
    topic TEXT NOT NULL, key TEXT NOT NULL, first_seen REAL NOT NULL, msg_id INTEGER,
    PRIMARY KEY (topic, key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS offsets (
    grp TEXT NOT NULL, topic TEXT NOT NULL, offset INTEGER NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (grp, topic)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS attempts (
    grp TEXT NOT NULL, msg_id INTEGER NOT NULL, n INTEGER NOT NULL, last_error TEXT, updated_at REAL NOT NULL,
    PRIMARY KEY (grp, msg_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grp TEXT NOT NULL, msg_id INTEGER NOT NULL, topic TEXT NOT NULL, key TEXT NOT NULL, type TEXT,
    ts_source TEXT, payload TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL, dead_at REAL NOT NULL,
    requeued INTEGER NOT NULL DEFAULT 0
);
"""


def default_bus_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.data_dir / "realtime" / "bus.sqlite"


class SqliteBus(Bus):
    def __init__(self, path: str | Path | None = None, *, read_only: bool = False, settings: Settings | None = None,
                 busy_timeout_ms: int = 15000):
        self.path = Path(path) if path else default_bus_path(settings)
        self.read_only = read_only
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            con = self._con()
            con.executescript(_SCHEMA)

    # ---- connections (one per thread)
    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is not None:
            return con
        if self.read_only:
            con = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, isolation_level=None,
                                  check_same_thread=False, timeout=self.busy_timeout_ms / 1000)
        else:
            con = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False,
                                  timeout=self.busy_timeout_ms / 1000)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        con.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        self._local.con = con
        with self._all_lock:
            self._all.append(con)
        return con

    def _write(self, fn: Any) -> Any:
        """Run fn(con) inside BEGIN IMMEDIATE ... COMMIT (retrying on 'database is locked')."""
        if self.read_only:
            raise PermissionError("bus opened read-only")
        con = self._con()
        for attempt in range(8):
            try:
                con.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                if "locked" in str(e) or "busy" in str(e):
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                raise
            try:
                out = fn(con)
                con.execute("COMMIT")
                return out
            except BaseException:
                con.execute("ROLLBACK")
                raise
        raise sqlite3.OperationalError("bus: could not acquire write lock")

    @staticmethod
    def _msg(row: Sequence[Any]) -> Message:
        return Message(id=row[0], topic=row[1], key=row[2], ckey=row[3], type=row[4], ts_source=row[5],
                       ts_ingest=row[6], payload=orjson.loads(row[7]))

    _COLS = "id, topic, key, ckey, type, ts_source, ts_ingest, payload"

    # ---- produce
    def publish_many(self, items: Sequence[Envelope]) -> list[int | None]:
        if not items:
            return []

        def tx(con: sqlite3.Connection) -> list[int | None]:
            out: list[int | None] = []
            now = _now()
            for e in items:
                if e.force:
                    con.execute("INSERT OR IGNORE INTO seen_keys(topic, key, first_seen) VALUES (?, ?, ?)",
                                (e.topic, e.key, now))
                else:
                    cur = con.execute("INSERT OR IGNORE INTO seen_keys(topic, key, first_seen) VALUES (?, ?, ?)",
                                      (e.topic, e.key, now))
                    if cur.rowcount == 0:
                        out.append(None)
                        continue
                cur = con.execute(
                    "INSERT INTO messages(topic, key, ckey, type, ts_source, ts_ingest, payload) VALUES (?,?,?,?,?,?,?)",
                    (e.topic, e.key, e.ckey, e.type or "", to_iso(e.ts_source), now,
                     orjson.dumps(e.payload, default=str).decode()))
                mid = int(cur.lastrowid)
                con.execute("UPDATE seen_keys SET msg_id = ? WHERE topic = ? AND key = ?", (mid, e.topic, e.key))
                out.append(mid)
            return out

        return self._write(tx)

    def seen(self, topic: str, keys: Sequence[str]) -> set[str]:
        """Keys already published on `topic` (lets producers skip expensive work for known items)."""
        if not keys:
            return set()
        con = self._con()
        out: set[str] = set()
        for i in range(0, len(keys), 500):
            chunk = list(keys[i:i + 500])
            q = f"SELECT key FROM seen_keys WHERE topic = ? AND key IN ({','.join('?' * len(chunk))})"
            out |= {r[0] for r in con.execute(q, [topic, *chunk])}
        return out

    # ---- read
    def read(self, topics: Sequence[str] | None, after_id: int = 0, limit: int = 500) -> list[Message]:
        con = self._con()
        if topics:
            q = (f"SELECT {self._COLS} FROM messages WHERE id > ? AND topic IN ({','.join('?' * len(topics))}) "
                 f"ORDER BY id LIMIT ?")
            rows = con.execute(q, [after_id, *topics, limit]).fetchall()
        else:
            rows = con.execute(f"SELECT {self._COLS} FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                               (after_id, limit)).fetchall()
        return [self._msg(r) for r in rows]

    def latest(self, topic: str, n: int = 50, where: dict[str, Any] | None = None,
               types: Sequence[str] | None = None) -> list[Message]:
        con = self._con()
        q = f"SELECT {self._COLS} FROM messages WHERE topic = ?"
        args: list[Any] = [topic]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            args += list(types)
        for k, v in (where or {}).items():
            if v is None:
                continue
            path = "$." + k.replace('"', "")
            if isinstance(v, (list, tuple, set)):
                vals = list(v)
                if not vals:
                    continue
                q += f" AND json_extract(payload, ?) IN ({','.join('?' * len(vals))})"
                args += [path, *vals]
            else:
                # scalar match, or membership when the payload field is a JSON array
                q += (" AND (json_extract(payload, ?) = ? OR EXISTS (SELECT 1 FROM json_each(payload, ?) "
                      "WHERE json_each.value = ?))")
                args += [path, v, path, v]
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(n))
        return [self._msg(r) for r in con.execute(q, args).fetchall()]

    def head_id(self) -> int:
        r = self._con().execute("SELECT max(id) FROM messages").fetchone()
        return int(r[0] or 0)

    def get(self, msg_id: int) -> Message | None:
        r = self._con().execute(f"SELECT {self._COLS} FROM messages WHERE id = ?", (msg_id,)).fetchone()
        return self._msg(r) if r else None

    # ---- consumer groups
    def _offsets(self, group: str, topics: Sequence[str]) -> dict[str, int]:
        con = self._con()
        rows = con.execute(f"SELECT topic, offset FROM offsets WHERE grp = ? AND topic IN ({','.join('?' * len(topics))})",
                           [group, *topics]).fetchall()
        got = dict(rows)
        return {t: int(got.get(t, 0)) for t in topics}

    def poll(self, group: str, topics: Sequence[str], limit: int = 200) -> list[Message]:
        offs = self._offsets(group, topics)
        con = self._con()
        out: list[Message] = []
        for t, off in offs.items():
            rows = con.execute(f"SELECT {self._COLS} FROM messages WHERE topic = ? AND id > ? ORDER BY id LIMIT ?",
                               (t, off, limit)).fetchall()
            out += [self._msg(r) for r in rows]
        out.sort(key=lambda m: m.id)
        return out[:limit]

    def commit(self, group: str, topic: str, msg_id: int) -> None:
        def tx(con: sqlite3.Connection) -> None:
            con.execute("INSERT INTO offsets(grp, topic, offset, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(grp, topic) DO UPDATE SET offset = max(offset, excluded.offset), "
                        "updated_at = excluded.updated_at", (group, topic, int(msg_id), _now()))
            con.execute("DELETE FROM attempts WHERE grp = ? AND msg_id <= ? AND msg_id IN "
                        "(SELECT id FROM messages WHERE topic = ?)", (group, int(msg_id), topic))
        self._write(tx)

    def commit_many(self, group: str, msgs: Iterable[Message]) -> None:
        last: dict[str, int] = {}
        for m in msgs:
            last[m.topic] = max(last.get(m.topic, 0), m.id)
        for t, i in last.items():
            self.commit(group, t, i)

    def nack(self, group: str, msg: Message, error: str, max_attempts: int = 5) -> bool:
        def tx(con: sqlite3.Connection) -> bool:
            con.execute("INSERT INTO attempts(grp, msg_id, n, last_error, updated_at) VALUES (?, ?, 1, ?, ?) "
                        "ON CONFLICT(grp, msg_id) DO UPDATE SET n = n + 1, last_error = excluded.last_error, "
                        "updated_at = excluded.updated_at", (group, msg.id, error[:2000], _now()))
            n = con.execute("SELECT n FROM attempts WHERE grp = ? AND msg_id = ?", (group, msg.id)).fetchone()[0]
            if n < max_attempts:
                return False
            con.execute("INSERT INTO dead_letters(grp, msg_id, topic, key, type, ts_source, payload, error, attempts, "
                        "dead_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (group, msg.id, msg.topic, msg.key, msg.type, msg.ts_source,
                         orjson.dumps(msg.payload, default=str).decode(), error[:2000], n, _now()))
            con.execute("DELETE FROM attempts WHERE grp = ? AND msg_id = ?", (group, msg.id))
            con.execute("INSERT INTO offsets(grp, topic, offset, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(grp, topic) DO UPDATE SET offset = max(offset, excluded.offset), "
                        "updated_at = excluded.updated_at", (group, msg.topic, msg.id, _now()))
            return True
        dead = self._write(tx)
        if dead:
            log.warning("bus: message %s (%s/%s) dead-lettered for group %s: %s", msg.id, msg.topic, msg.key, group,
                        error[:200])
        return dead

    def attempts(self, group: str, msg_id: int) -> int:
        r = self._con().execute("SELECT n FROM attempts WHERE grp = ? AND msg_id = ?", (group, msg_id)).fetchone()
        return int(r[0]) if r else 0

    def seek(self, group: str, topic: str, msg_id: int) -> None:
        def tx(con: sqlite3.Connection) -> None:
            con.execute("INSERT INTO offsets(grp, topic, offset, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(grp, topic) DO UPDATE SET offset = excluded.offset, updated_at = excluded.updated_at",
                        (group, topic, int(msg_id), _now()))
        self._write(tx)

    def offsets(self, group: str | None = None) -> dict[str, dict[str, int]]:
        con = self._con()
        q = "SELECT grp, topic, offset FROM offsets" + (" WHERE grp = ?" if group else "")
        out: dict[str, dict[str, int]] = {}
        for g, t, o in con.execute(q, (group,) if group else ()):
            out.setdefault(g, {})[t] = int(o)
        return out

    # ---- dead letters
    def dead_letters(self, group: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        con = self._con()
        q = ("SELECT id, grp, msg_id, topic, key, type, ts_source, payload, error, attempts, dead_at, requeued "
             "FROM dead_letters" + (" WHERE grp = ?" if group else "") + " ORDER BY id DESC LIMIT ?")
        rows = con.execute(q, ((group, limit) if group else (limit,))).fetchall()
        cols = ("id", "group", "msg_id", "topic", "key", "type", "ts_source", "payload", "error", "attempts", "dead_at",
                "requeued")
        out = []
        for r in rows:
            d = dict(zip(cols, r, strict=True))
            d["payload"] = orjson.loads(d["payload"])
            out.append(d)
        return out

    def requeue_dead(self, dlq_ids: Iterable[int]) -> int:
        ids = [int(i) for i in dlq_ids]
        if not ids:
            return 0
        rows = self._con().execute(
            f"SELECT id, topic, key, type, ts_source, payload FROM dead_letters WHERE id IN ({','.join('?' * len(ids))})",
            ids).fetchall()
        envs = [Envelope(t, f"{k}#requeue{i}", orjson.loads(p), ty or "",
                         datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None, force=True)
                for i, t, k, ty, ts, p in rows]
        self.publish_many(envs)
        self._write(lambda con: con.execute(
            f"UPDATE dead_letters SET requeued = 1 WHERE id IN ({','.join('?' * len(ids))})", ids))
        return len(envs)

    # ---- ops
    def retention(self, policies: dict[str, tuple[float, int, bool]] | None = None,
                  protect_groups: Sequence[str] = ("lake",), dedupe_days: float = DEDUPE_DAYS) -> dict[str, int]:
        pol = {**DEFAULT_RETENTION, **(policies or {})}
        offs = self.offsets()
        now = _now()
        rep: dict[str, int] = {}

        def tx(con: sqlite3.Connection) -> None:
            topics = [r[0] for r in con.execute("SELECT DISTINCT topic FROM messages")]
            for t in topics:
                max_age_h, keep_min, compact = pol.get(t, (72.0, 1000, False))
                # never delete what a protected group (the lake compactor) has not committed; a group that has never
                # committed on this topic protects everything (offset 0)
                protected = [offs.get(g, {}).get(t, 0) for g in protect_groups]
                guard = min(protected) if protected else None
                kth = con.execute("SELECT id FROM messages WHERE topic = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                                  (t, max(keep_min - 1, 0))).fetchone()
                floor_keep = (kth[0] if kth else 0) if keep_min > 0 else 1 << 62   # ids >= floor_keep: newest keep_min
                q = "DELETE FROM messages WHERE topic = ? AND ts_ingest < ? AND id < ?"
                args: list[Any] = [t, now - max_age_h * 3600, floor_keep]
                if guard is not None:
                    q += " AND id <= ?"
                    args.append(guard)
                n = con.execute(q, args).rowcount
                if compact:
                    q2 = ("DELETE FROM messages WHERE topic = ? AND ckey IS NOT NULL AND id < "
                          "(SELECT max(m2.id) FROM messages m2 WHERE m2.topic = messages.topic AND m2.ckey = messages.ckey)")
                    a2: list[Any] = [t]
                    if guard is not None:
                        q2 += " AND id <= ?"
                        a2.append(guard)
                    n += con.execute(q2, a2).rowcount
                if n:
                    rep[t] = n
            rep["seen_keys"] = con.execute("DELETE FROM seen_keys WHERE first_seen < ?",
                                           (now - dedupe_days * 86400,)).rowcount
            rep["attempts"] = con.execute("DELETE FROM attempts WHERE updated_at < ?", (now - 7 * 86400,)).rowcount
            rep["dead_letters"] = con.execute("DELETE FROM dead_letters WHERE dead_at < ?", (now - 30 * 86400,)).rowcount

        self._write(tx)
        try:
            self._con().execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:  # pragma: no cover
            pass
        return rep

    def prune_topic(self, topic: str, max_age_h: float, keep_min: int = 0) -> int:
        """Delete messages of ONE topic older than max_age_h (keeping the newest keep_min) and their dedupe keys.
        Used by producers of high-volume, durable-elsewhere topics (quotes.*) between the hourly retention runs."""
        cut = _now() - max_age_h * 3600

        def tx(con: sqlite3.Connection) -> int:
            kth = con.execute("SELECT id FROM messages WHERE topic = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                              (topic, max(keep_min - 1, 0))).fetchone()
            floor_keep = (kth[0] if kth else 0) if keep_min > 0 else 1 << 62
            n = con.execute("DELETE FROM messages WHERE topic = ? AND ts_ingest < ? AND id < ?",
                            (topic, cut, floor_keep)).rowcount
            con.execute("DELETE FROM seen_keys WHERE topic = ? AND first_seen < ?", (topic, cut))
            return n

        return int(self._write(tx))

    def stats(self) -> dict[str, Any]:
        con = self._con()
        topics = {}
        for t, n, lo, hi, last in con.execute(
                "SELECT topic, count(*), min(id), max(id), max(ts_ingest) FROM messages GROUP BY topic"):
            n1h = con.execute("SELECT count(*) FROM messages WHERE topic = ? AND ts_ingest >= ?",
                              (t, _now() - 3600)).fetchone()[0]
            topics[t] = {"messages": n, "first_id": lo, "last_id": hi, "messages_1h": n1h,
                         "last_ingest": datetime.fromtimestamp(last, UTC).isoformat(timespec="seconds") if last else None}
        groups: dict[str, Any] = {}
        for g, offs in self.offsets().items():
            lag = {}
            for t, o in offs.items():
                lag[t] = con.execute("SELECT count(*) FROM messages WHERE topic = ? AND id > ?", (t, o)).fetchone()[0]
            groups[g] = {"offsets": offs, "lag": lag, "lag_total": sum(lag.values())}
        dlq = con.execute("SELECT count(*) FROM dead_letters WHERE requeued = 0").fetchone()[0]
        try:
            size = self.path.stat().st_size + (Path(str(self.path) + "-wal").stat().st_size
                                               if Path(str(self.path) + "-wal").exists() else 0)
        except OSError:
            size = None
        return {"backend": "sqlite", "path": str(self.path), "head_id": self.head_id(), "topics": topics,
                "groups": groups, "dead_letters": dlq, "bytes": size}

    def close(self) -> None:
        with self._all_lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._all.clear()
        self._local = threading.local()


# ================================================================================================ Redis stub
class RedisBus(Bus):
    """Placeholder for the Redis Streams backend (not used yet: no Redis/Docker on this machine).

    Mapping when implemented:
      publish_many  -> SET bus:seen:{topic}:{key} NX EX <dedupe> then XADD bus:{topic} MAXLEN ~ N * {fields}
                       (global id: INCR bus:seq stored in the entry, so SSE ids stay integers)
      read/latest   -> XRANGE / XREVRANGE bus:{topic}
      poll/commit   -> XGROUP CREATE ... MKSTREAM; XREADGROUP GROUP g c STREAMS ...; XACK
      nack/DLQ      -> XPENDING (delivery counts) + XCLAIM; after max_attempts XADD bus:dlq:{group} + XACK
      seek          -> XGROUP SETID
      retention     -> XTRIM MINID / MAXLEN; compaction via a side hash of latest ids per ckey
    """

    def __init__(self, url: str | None = None, **_: Any):
        self.url = url or os.environ.get("BIG360_REDIS_URL", "redis://127.0.0.1:6379/0")

    def _ni(self) -> Any:
        raise NotImplementedError("RedisBus is a stub; set BIG360_BUS=sqlite (default)")

    def publish_many(self, items: Sequence[Envelope]) -> list[int | None]:
        return self._ni()

    def read(self, topics: Sequence[str] | None, after_id: int = 0, limit: int = 500) -> list[Message]:
        return self._ni()

    def latest(self, topic: str, n: int = 50, where: dict[str, Any] | None = None,
               types: Sequence[str] | None = None) -> list[Message]:
        return self._ni()

    def head_id(self) -> int:
        return self._ni()

    def poll(self, group: str, topics: Sequence[str], limit: int = 200) -> list[Message]:
        return self._ni()

    def commit(self, group: str, topic: str, msg_id: int) -> None:
        return self._ni()

    def nack(self, group: str, msg: Message, error: str, max_attempts: int = 5) -> bool:
        return self._ni()

    def seek(self, group: str, topic: str, msg_id: int) -> None:
        return self._ni()

    def offsets(self, group: str | None = None) -> dict[str, dict[str, int]]:
        return self._ni()

    def dead_letters(self, group: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._ni()

    def requeue_dead(self, dlq_ids: Iterable[int]) -> int:
        return self._ni()

    def retention(self, policies: dict[str, tuple[float, int, bool]] | None = None,
                  protect_groups: Sequence[str] = ("lake",), dedupe_days: float = DEDUPE_DAYS) -> dict[str, int]:
        return self._ni()

    def stats(self) -> dict[str, Any]:
        return self._ni()


def get_bus(settings: Settings | None = None, read_only: bool = False, path: str | Path | None = None) -> Bus:
    backend = os.environ.get("BIG360_BUS", "sqlite").lower()
    if backend == "redis":
        return RedisBus()
    return SqliteBus(path, read_only=read_only, settings=settings)
