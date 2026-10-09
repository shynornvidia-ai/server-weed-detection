"""FastAPI app — phan 2: nhan du lieu tu edge + REST cho web UI."""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .map import build_heatmap
from .schemas import IngestBatch

logger = logging.getLogger("weed_server")

DB_PATH = Path(os.environ.get("WEED_SERVER_DB", "data/weed_server.db"))
API_KEY = os.environ.get("WEED_SERVER_API_KEY", "change-me-please")
DEFAULT_API_KEY = "change-me-please"

# CORS: mac dinh cho phep tat ca (van hanh tren may rieng). Khi len server cong
# cong hay dat WEED_SERVER_CORS_ORIGINS=https://ten-cua-ban.vercel.app
CORS_ORIGINS = [o.strip() for o in
                os.environ.get("WEED_SERVER_CORS_ORIGINS", "*").split(",") if o.strip()]

# Gioi han kich thuoc batch (chong tran memory / loi phan cung)
MAX_BATCH_SAMPLES = int(os.environ.get("WEED_SERVER_MAX_BATCH_SAMPLES", "1000"))
MAX_BATCH_FRAMES = int(os.environ.get("WEED_SERVER_MAX_BATCH_FRAMES", "8000"))
# 0 = tat rate limit
RATE_LIMIT_PER_MIN = int(os.environ.get("WEED_SERVER_RATE_LIMIT", "600"))
# 0 = khong tu xoa du lieu cu
RETENTION_DAYS = int(os.environ.get("WEED_SERVER_RETENTION_DAYS", "0"))

app = FastAPI(title="Weed Detector Server", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = Database(DB_PATH)
    return _db


def admin_key() -> str:
    """Khoa quan ly (xoa phien...). Mac dinh = API key; tach rieng duoc qua env.

    Doc env TAI KHI GOI de test/override chay duoc (khong chot tu luc import).
    """
    return os.environ.get("WEED_SERVER_ADMIN_KEY", "").strip() or API_KEY


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key khong hop le")


def require_admin(x_api_key: str = Header(default="")) -> None:
    key = admin_key()
    if key and x_api_key != key:
        raise HTTPException(status_code=401, detail="Khoa quan tri khong hop le")


class RateLimiter:
    """Gioi han so request/phay theo IP — chan bot/tran bang."""

    def __init__(self, per_minute: int):
        self.per_minute = max(0, int(per_minute))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 5000:          # don sach khoa cu de khong ro ri
                for k in list(self._hits):
                    if not self._hits[k]:
                        self._hits.pop(k, None)
            q = self._hits[key]
            while q and now - q[0] > 60.0:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


_limiter = RateLimiter(RATE_LIMIT_PER_MIN)


@app.on_event("startup")
def _startup() -> None:
    if API_KEY == DEFAULT_API_KEY:
        logger.warning(
            "DANG DUNG API KEY MAC DINH '%s' — dat WEED_SERVER_ADMIN_KEY / "
            "WEED_SERVER_API_KEY truoc khi cho phep tu ben ngoai.",
            DEFAULT_API_KEY,
        )
    get_db()
    if RETENTION_DAYS > 0:
        _prune_loop()


def _prune_loop(interval_s: float = 6 * 3600) -> None:
    """Tu xoa phien cu hon RETENTION_DAYS ngay — chay nen, khong can cron."""

    def run() -> None:
        while True:
            try:
                res = get_db().prune(RETENTION_DAYS)
                if res.get("deleted_sessions"):
                    logger.info("Retention: da xoa %s phien cu hon %s ngay",
                                res["deleted_sessions"], RETENTION_DAYS)
            except Exception:
                logger.exception("Retention prune loi")
            time.sleep(interval_s)

    threading.Thread(target=run, name="retention", daemon=True).start()


# --------------------------- ingest ---------------------------
@app.post("/api/v1/ingest")
def ingest(batch: IngestBatch, request: Request, _=Depends(require_api_key)):
    ip = request.client.host if request.client else "unknown"
    if not _limiter.allow(ip):
        raise HTTPException(status_code=429, detail="Qua nhieu request — thu lai sau")

    n_samples, n_frames = len(batch.samples), sum(len(s.frames) for s in batch.samples)
    if n_samples > MAX_BATCH_SAMPLES or n_frames > MAX_BATCH_FRAMES:
        raise HTTPException(
            status_code=413,
            detail=f"Batch qua lon ({n_samples} sample / {n_frames} frame, "
                   f"gioi han {MAX_BATCH_SAMPLES}/{MAX_BATCH_FRAMES})",
        )

    t0 = time.perf_counter()
    db = get_db()
    stored, duplicate = db.insert_batch(batch)
    dt = (time.perf_counter() - t0) * 1000
    if duplicate:
        # Idempotent: edge gui lai batch cu (retry outbox) -> khong luu them
        logger.info("ingest TRUNG (bo qua) device=%s session=%s batch_id=%s",
                    batch.device_id, batch.session_id, Database.batch_id_of(batch)[:24])
        return {"ok": True, "duplicate": True, "stored_frames": 0,
                "elapsed_ms": round(dt, 1)}
    logger.info(
        "ingest device=%s session=%s samples=%d track=%d frames=%d (%.1fms)",
        batch.device_id, batch.session_id, len(batch.samples), len(batch.track),
        stored, dt,
    )
    return {"ok": True, "duplicate": False, "stored_frames": stored,
            "elapsed_ms": round(dt, 1)}


# --------------------------- REST cho UI ---------------------------
@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}


@app.get("/api/v1/stats")
def stats(device_id: str | None = Query(None), session_id: str | None = Query(None),
          since: str | None = Query(None), until: str | None = Query(None),
          db: Database = Depends(get_db)):
    return db.stats(device_id, session_id, since, until)


@app.get("/api/v1/daily")
def daily(device_id: str | None = Query(None),
          session_id: str | None = Query(None),
          days: int = Query(30, ge=1, le=365),
          since: str | None = Query(None), until: str | None = Query(None),
          db: Database = Depends(get_db)):
    """Thong ke theo ngay: frame, frame co coves, mau GPS, mau uoc luong."""
    rows = db.daily_stats(device_id, session_id, days, since, until)
    return {"days": len(rows), "rows": rows}


@app.get("/api/v1/devices")
def devices(db: Database = Depends(get_db)):
    return db.devices()


@app.get("/api/v1/sessions")
def sessions(device_id: str | None = None, db: Database = Depends(get_db)):
    return db.sessions(device_id)


@app.delete("/api/v1/sessions/{session_id}")
def delete_session(session_id: str, device_id: str | None = Query(None),
                   db: Database = Depends(get_db), _=Depends(require_admin)):
    """Xoa mot phien (dung de xoa phien test). Can X-API-Key."""
    res = db.delete_session(session_id, device_id)
    if not res.get("deleted"):
        raise HTTPException(status_code=404, detail="Khong thay phien nay")
    logger.info("delete session=%s %s", session_id, res)
    return res


@app.delete("/api/v1/devices/{device_id}/sessions")
def delete_device_sessions(device_id: str, db: Database = Depends(get_db),
                           _=Depends(require_admin)):
    """Xoa TAT CA phien cua mot thiet bi."""
    res = db.delete_device_sessions(device_id)
    if not res.get("deleted"):
        raise HTTPException(status_code=404, detail="Khong thay thiet bi nay")
    logger.info("delete sessions device=%s %s", device_id, res)
    return res


@app.get("/api/v1/track")
def track(
    device_id: str = Query(...),
    session_id: str | None = Query(None),
    since: str | None = Query(None, description="ISO — loc tu thoi diem nay"),
    until: str | None = Query(None, description="ISO — den thoi diem nay"),
    db: Database = Depends(get_db),
):
    pts = db.track(device_id, session_id, since=since, until=until)
    return {"device_id": device_id, "session_id": session_id, "count": len(pts),
            "points": pts}


@app.get("/api/v1/heatmap")
def heatmap(
    device_id: str | None = Query(None),
    session_id: str | None = Query(None),
    cell_size_m: float = Query(5.0, gt=0, le=200),
    since: str | None = Query(None),
    until: str | None = Query(None),
    db: Database = Depends(get_db),
):
    frames, _ = db.grass_frames(device_id, session_id, since=since, until=until)
    cells = build_heatmap(frames, cell_size_m)
    return {
        "cell_size_m": cell_size_m,
        "total_grass_frames": len(frames),
        "total_cells": len(cells),
        "cells": cells,
    }


@app.get("/api/v1/grass-points")
def grass_points(
    device_id: str | None = Query(None),
    session_id: str | None = Query(None),
    limit: int = Query(5000, ge=1, le=50000),
    offset: int = Query(0, ge=0),
    since: str | None = Query(None),
    until: str | None = Query(None),
    db: Database = Depends(get_db),
):
    """Diem co chi tiet (khong gom o) — de web click chi duong den dung vi tri.

    Co phan trang: `offset`/`limit` + `total` (tong so khong gioi han).
    """
    rows, total = db.grass_frames(device_id, session_id, limit, offset,
                                  since=since, until=until)
    return {
        "count": len(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
        "points": [
            {"lat": f["lat"], "lon": f["lon"], "prob": f["grass_probability"],
             "camera": f["camera"], "camera_position": f["camera_position"],
             "captured_at": f["captured_at"],
             "estimated": bool(f.get("estimated") or 0),
             "attention": f["patch_attention"],
             "inference_ms": f.get("inference_ms")}
            for f in rows
        ],
    }


# --------------------------- UI ---------------------------
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    global DB_PATH
    parser = argparse.ArgumentParser(prog="weed-server", description="Weed Detector Server (Phan 2)")
    parser.add_argument("--host", default=os.environ.get("WEED_SERVER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEED_SERVER_PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("WEED_SERVER_DB", "data/weed_server.db"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.db != str(DB_PATH):
        DB_PATH = Path(args.db)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    import uvicorn
    uvicorn.run("weed_server.api:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
