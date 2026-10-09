"""SQLite storage — du nhe cho MVP, khong can server DB rieng.

Schema: devices, sessions, batches, track_points, samples, frames.

Idempotency: moi batch co `batch_id` (edge tao 1 lan khi flush, giu nguyen khi
retry tu outbox). Bang `batches` ghi lai cac batch da nhan -> neu edge gui lai
(cac lan retry sau mat mang) thi server tra ve duplicate va KHONG luu them dau.
Batch cu (edge khong gui batch_id) duoc ma hoa noi dung sha256 de van phan biet
duoc, nen retry cung khong nhan doi.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    frame_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_session ON batches(session_id);
CREATE TABLE IF NOT EXISTS track_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    ts TEXT NOT NULL,
    speed_mps REAL,
    estimated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_track_dev_ts ON track_points(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_track_sess_ts ON track_points(device_id, session_id, ts);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt REAL,
    satellites INTEGER,
    hdop REAL,
    estimated INTEGER DEFAULT 0,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_dev_ts ON samples(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_samples_sess_ts ON samples(device_id, session_id, ts);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    camera TEXT NOT NULL,
    camera_position TEXT,
    captured_at TEXT NOT NULL,
    grass_probability REAL NOT NULL,
    is_grass INTEGER NOT NULL,
    patch_attention TEXT,
    threshold REAL,
    inference_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_frames_dev ON frames(device_id, captured_at);
-- index cho heatmap + loc thoi gian (query hay dung nhat)
CREATE INDEX IF NOT EXISTS idx_frames_grass ON frames(device_id, session_id, is_grass, captured_at);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path = "weed_server.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Them cot moi cho DB da ton tai (CREATE TABLE IF NOT EXISTS khong lam viec nay)."""
        for table, col, decl in (
            ("samples", "estimated", "INTEGER DEFAULT 0"),
            ("track_points", "estimated", "INTEGER DEFAULT 0"),
        ):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self._conn.close()

    # ------------------------- ingest -------------------------
    @staticmethod
    def batch_id_of(batch) -> str:
        """Id dung de dedupe: lay `batch_id` neu co, khong thi sha256 noi dung."""
        bid = getattr(batch, "batch_id", None)
        if bid:
            return str(bid)
        canon = json.dumps(
            batch.model_dump(), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), default=str,
        )
        return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def has_batch(self, batch_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            return row is not None

    def insert_batch(self, batch) -> tuple[int, bool]:
        """Insert 1 IngestBatch.

        Returns (so_frame_moi_luu, duplicate).
        duplicate=True nghia la da nhan cai nay roi -> khong luu gi them.
        """
        bid = self.batch_id_of(batch)
        now = _utcnow_iso()
        with self._lock:
            # Kiem tra + ghi duoi CUNG 1 lock -> khong 2 thread cung luc nhan lai
            cur = self._conn.cursor()
            if cur.execute("SELECT 1 FROM batches WHERE batch_id=?", (bid,)).fetchone():
                return 0, True
            try:
                cur.execute(
                    "INSERT INTO batches(batch_id, device_id, session_id, created_at,"
                    " received_at, sample_count, frame_count) VALUES(?,?,?,?,?,?,?)",
                    (bid, batch.device_id, batch.session_id, batch.created_at, now,
                     len(batch.samples),
                     sum(len(s.frames) for s in batch.samples)),
                )
                cur.execute(
                    "INSERT OR IGNORE INTO devices(id, name, first_seen) VALUES(?,?,?)",
                    (batch.device_id, batch.device_name, now),
                )
                cur.execute(
                    "INSERT OR IGNORE INTO sessions(id, device_id, created_at) VALUES(?,?,?)",
                    (batch.session_id, batch.device_id,
                     batch.track[0].timestamp if batch.track else now),
                )
                for tp in batch.track:
                    cur.execute(
                        "INSERT INTO track_points(device_id, session_id, lat, lon, ts,"
                        " speed_mps, estimated) VALUES(?,?,?,?,?,?,?)",
                        (batch.device_id, batch.session_id, tp.lat, tp.lon, tp.timestamp,
                         tp.speed_mps, int(tp.estimated)),
                    )
                n_frames = 0
                for s in batch.samples:
                    cur.execute(
                        "INSERT INTO samples(device_id, session_id, lat, lon, alt,"
                        " satellites, hdop, estimated, ts) VALUES(?,?,?,?,?,?,?,?,?)",
                        (batch.device_id, batch.session_id, s.gps.lat, s.gps.lon, s.gps.alt,
                         s.gps.satellites, s.gps.hdop, int(s.gps.estimated),
                         s.gps.fix_time or s.frames[0].captured_at),
                    )
                    sample_id = cur.lastrowid
                    for f in s.frames:
                        cur.execute(
                            "INSERT INTO frames(sample_id, device_id, session_id, camera,"
                            " camera_position, captured_at, grass_probability, is_grass,"
                            " patch_attention, threshold, inference_ms)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (sample_id, batch.device_id, batch.session_id, f.camera,
                             f.camera_position, f.captured_at, f.grass_probability,
                             int(f.is_grass), json.dumps(f.patch_attention),
                             f.threshold, f.inference_ms),
                        )
                        n_frames += 1
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return n_frames, False

    # ------------------------- xoa du lieu -------------------------
    def _delete_session_rows(self, cur, session_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in ("frames", "samples", "track_points", "batches"):
            cur.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            counts[table] = cur.rowcount
        cur.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        counts["sessions"] = cur.rowcount
        return counts

    @staticmethod
    def _sum_counts(parts: list[dict[str, int]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in parts:
            for k, v in p.items():
                out[k] = out.get(k, 0) + v
        return out

    def delete_session(self, session_id: str, device_id: str | None = None) -> dict:
        """Xoa 1 phien (frame/sample/track/batch/session). Session id la toan cuc."""
        with self._lock:
            cur = self._conn.cursor()
            if device_id:
                row = cur.execute(
                    "SELECT device_id FROM sessions WHERE id=? AND device_id=?",
                    (session_id, device_id),
                ).fetchone()
            else:
                row = cur.execute(
                    "SELECT device_id FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
            if row is None:
                return {"deleted": False, "session_id": session_id}
            counts = self._delete_session_rows(cur, session_id)
            counts.update(self._maybe_drop_device(cur, row["device_id"]))
            self._conn.commit()
            counts.update({"deleted": True, "session_id": session_id})
            return counts

    def delete_device_sessions(self, device_id: str) -> dict:
        """Xoa TAT CA phien cua 1 thiet bi (dung cho 'xoa phien test')."""
        with self._lock:
            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT id FROM sessions WHERE device_id=?", (device_id,)
            ).fetchall()
            if not rows and not cur.execute(
                "SELECT 1 FROM devices WHERE id=?", (device_id,)
            ).fetchone():
                return {"deleted": False, "device_id": device_id, "sessions": 0}
            parts = [self._delete_session_rows(cur, r["id"]) for r in rows]
            counts = self._sum_counts(parts)
            cur.execute("DELETE FROM devices WHERE id=?", (device_id,))
            counts.update({"deleted": True, "device_id": device_id,
                           "sessions": len(rows)})
            self._conn.commit()
            return counts

    def _maybe_drop_device(self, cur, device_id: str) -> dict:
        """Xoa device neu khong con phien nao (khong de thiet bi rong)."""
        rest = cur.execute(
            "SELECT COUNT(*) FROM sessions WHERE device_id=?", (device_id,)
        ).fetchone()[0]
        if rest:
            return {}
        cur.execute("DELETE FROM devices WHERE id=?", (device_id,))
        return {"device_removed": cur.rowcount > 0}

    def prune(self, retention_days: int) -> dict:
        """Xoa phien cu hon `retention_days` ngay. 0 = khong giu han muc."""
        if retention_days <= 0:
            return {"deleted_sessions": 0, "cutoff": None}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT id, device_id FROM sessions WHERE datetime(created_at) < datetime(?)",
                (cutoff,),
            ).fetchall()
            parts = [self._delete_session_rows(cur, r["id"]) for r in rows]
            for dev in {r["device_id"] for r in rows}:
                self._maybe_drop_device(cur, dev)
            self._conn.commit()
        return {"deleted_sessions": len(rows),
                "frames": self._sum_counts(parts).get("frames", 0),
                "cutoff": cutoff}

    # ------------------------- queries -------------------------
    @staticmethod
    def _time_filter(column: str, since: str | None, until: str | None) -> tuple[str, list]:
        q, params = "", []
        if since:
            q += f" AND datetime({column}) >= datetime(?)"
            params.append(since)
        if until:
            q += f" AND datetime({column}) <= datetime(?)"
            params.append(until)
        return q, params

    def devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, d.name, d.first_seen,"
                " (SELECT COUNT(*) FROM sessions s WHERE s.device_id=d.id) AS session_count"
                " FROM devices d ORDER BY d.first_seen DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def sessions(self, device_id: str | None = None) -> list[dict]:
        with self._lock:
            if device_id:
                rows = self._conn.execute(
                    "SELECT s.*, (SELECT COUNT(*) FROM frames f WHERE f.session_id=s.id)"
                    " AS frame_count,"
                    " (SELECT COUNT(*) FROM frames f WHERE f.session_id=s.id AND f.is_grass=1)"
                    " AS grass_frame_count"
                    " FROM sessions s WHERE device_id=? ORDER BY created_at DESC",
                    (device_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT s.*, (SELECT COUNT(*) FROM frames f WHERE f.session_id=s.id)"
                    " AS frame_count,"
                    " (SELECT COUNT(*) FROM frames f WHERE f.session_id=s.id AND f.is_grass=1)"
                    " AS grass_frame_count"
                    " FROM sessions s ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def track(self, device_id: str, session_id: str | None = None,
              limit: int = 20000, since: str | None = None,
              until: str | None = None) -> list[dict]:
        q = "SELECT lat, lon, ts, speed_mps, estimated FROM track_points WHERE device_id=?"
        params: list = [device_id]
        if session_id:
            q += " AND session_id=?"
            params.append(session_id)
        extra, extra_p = self._time_filter("ts", since, until)
        q += extra
        params += extra_p
        q += " ORDER BY ts LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]

    def grass_frames(self, device_id: str | None = None,
                     session_id: str | None = None, limit: int = 50000,
                     offset: int = 0, since: str | None = None,
                     until: str | None = None) -> tuple[list[dict], int]:
        """Cac frame duoc danh dau la co (dung de tinh heatmap).

        Returns (rows, tong_so) — `tong_so` cho phan trang tren UI.
        """
        where = " FROM frames f JOIN samples s ON s.id=f.sample_id WHERE f.is_grass=1"
        params: list = []
        if device_id:
            where += " AND f.device_id=?"
            params.append(device_id)
        if session_id:
            where += " AND f.session_id=?"
            params.append(session_id)
        extra, extra_p = self._time_filter("f.captured_at", since, until)
        where += extra
        params += extra_p

        cols = ("SELECT f.grass_probability, f.captured_at, f.camera, f.camera_position,"
                " f.patch_attention, f.inference_ms, f.threshold, s.lat, s.lon, s.estimated"
                + where + " ORDER BY f.captured_at DESC LIMIT ? OFFSET ?")
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*)" + where, params).fetchone()[0]
            rows = self._conn.execute(cols, params + [int(limit), int(offset)]).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["patch_attention"] = json.loads(d["patch_attention"] or "[]")
                out.append(d)
            return out, int(total)

    def stats(self, device_id: str | None = None, session_id: str | None = None,
              since: str | None = None, until: str | None = None) -> dict:
        """Thong ke — loc duoc theo device/phien/khoang thoi gian (UI truyen ca 3)."""
        def build(prefix: str, ts_col: str, session_col: str = "session_id") \
                -> tuple[list[str], list]:
            conds: list[str] = []
            params: list = []
            if device_id:
                conds.append(f"{prefix}device_id=?")
                params.append(device_id)
            if session_id:
                conds.append(f"{prefix}{session_col}=?")
                params.append(session_id)
            if since:
                conds.append(f"datetime({prefix}{ts_col}) >= datetime(?)")
                params.append(since)
            if until:
                conds.append(f"datetime({prefix}{ts_col}) <= datetime(?)")
                params.append(until)
            return conds, params

        f_conds, f_params = build("f.", "captured_at")
        sa_conds, sa_params = build("", "ts")
        tr_conds, tr_params = build("", "ts")
        # sessions.khong co session_id — cot tham chieu la id
        se_conds, se_params = build("", "created_at", session_col="id")
        ba_conds, ba_params = build("", "created_at")
        est_conds = sa_conds + ["estimated=1"]
        grass_conds = f_conds + ["f.is_grass=1"]

        def count(table: str, conds: list[str], params: list) -> int:
            sql = f"SELECT COUNT(*) FROM {table}"
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            return int(self._conn.execute(sql, params).fetchone()[0])

        with self._lock:
            return {
                "devices": int(self._conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]),
                "sessions": count("sessions", se_conds, se_params),
                "samples": count("samples", sa_conds, sa_params),
                "frames": count("frames f", f_conds, f_params),
                "grass_frames": count("frames f", grass_conds, f_params),
                "track_points": count("track_points", tr_conds, tr_params),
                "estimated_samples": count("samples", est_conds, sa_params),
                "batches": count("batches", ba_conds, ba_params),
            }

    def daily_stats(self, device_id: str | None = None, session_id: str | None = None,
                    days: int = 30, since: str | None = None,
                    until: str | None = None) -> list[dict]:
        """Thong ke theo ngay (frame, frame co coves, mau GPS) — cho UI."""
        since = since or (
            datetime.now(timezone.utc) - timedelta(days=max(1, days))
        ).isoformat()
        f_where, f_p = " WHERE datetime(f.captured_at) >= datetime(?)", [since]
        if device_id:
            f_where += " AND f.device_id=?"
            f_p.append(device_id)
        if session_id:
            f_where += " AND f.session_id=?"
            f_p.append(session_id)
        extra, extra_p = self._time_filter("f.captured_at", None, until)
        f_where += extra
        f_p += extra_p

        s_where, s_p = " WHERE datetime(ts) >= datetime(?)", [since]
        if device_id:
            s_where += " AND device_id=?"
            s_p.append(device_id)
        if session_id:
            s_where += " AND session_id=?"
            s_p.append(session_id)
        extra, extra_p = self._time_filter("ts", None, until)
        s_where += extra
        s_p += extra_p

        with self._lock:
            fr = self._conn.execute(
                "SELECT substr(f.captured_at,1,10) AS day, COUNT(*) AS frames,"
                " SUM(f.is_grass) AS grass_frames"
                " FROM frames f" + f_where + " GROUP BY day ORDER BY day DESC", f_p,
            ).fetchall()
            sa = self._conn.execute(
                "SELECT substr(ts,1,10) AS day, COUNT(*) AS samples,"
                " SUM(estimated) AS estimated"
                " FROM samples" + s_where + " GROUP BY day", s_p,
            ).fetchall()
        by_day: dict[str, dict] = {}
        for r in sa:
            d = dict(r)
            by_day[d["day"]] = {"day": d["day"], "samples": d["samples"],
                                "estimated": d["estimated"] or 0,
                                "frames": 0, "grass_frames": 0}
        for r in fr:
            d = dict(r)
            row = by_day.setdefault(
                d["day"],
                {"day": d["day"], "samples": 0, "estimated": 0,
                 "frames": 0, "grass_frames": 0},
            )
            row["frames"] = d["frames"]
            row["grass_frames"] = d["grass_frames"] or 0
        return sorted(by_day.values(), key=lambda x: x["day"], reverse=True)
