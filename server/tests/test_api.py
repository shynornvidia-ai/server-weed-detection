"""Test phan 2: ingest tu edge + cac API cho web UI."""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

import weed_server.api as api
from weed_server.map import build_heatmap, cell_center, cell_key

API_KEY = "test-key"
DEVICE = "jetson-test"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(api, "_db", None)
    monkeypatch.setattr(api, "API_KEY", API_KEY)
    with TestClient(api.app) as c:
        yield c


def headers() -> dict:
    return {"X-API-Key": API_KEY}


def frame(camera: str = "cam_left", prob: float = 0.9, is_grass: bool = True) -> dict:
    return {
        "captured_at": "2026-01-01T00:00:00+00:00",
        "camera": camera,
        "camera_position": "left" if camera == "cam_left" else "right",
        "grass_probability": prob,
        "is_grass": is_grass,
        "patch_attention": [1 / 12] * 12,
        "threshold": 0.10,
        "model_device": "cuda",
        "inference_ms": 42.0,
    }


def batch(lat: float = 12.68120, lon: float = 108.06420, session: str = "s1",
          estimated: bool = False, batch_id: str | None = None) -> dict:
    out = {
        "device_id": DEVICE,
        "device_name": "Jetson test",
        "created_at": "2026-01-01T00:00:00+00:00",
        "session_id": session,
        "schema_version": 1,
        "samples": [
            {
                "gps": {"lat": lat, "lon": lon, "alt": 800.0, "satellites": 9, "hdop": 0.8,
                        "fix_time": "2026-01-01T00:00:00+00:00", "estimated": estimated},
                "frames": [frame("cam_left"), frame("cam_right", prob=0.2, is_grass=False)],
            }
        ],
        "track": [
            {"lat": lat, "lon": lon, "timestamp": "2026-01-01T00:00:00+00:00",
             "speed_mps": 1.4, "estimated": estimated}
        ],
    }
    if batch_id is not None:
        out["batch_id"] = batch_id
    return out


# ----------------------------- auth -----------------------------
def test_ingest_requires_api_key(client) -> None:
    assert client.post("/api/v1/ingest", json=batch()).status_code == 401


def test_ingest_rejects_wrong_api_key(client) -> None:
    r = client.post("/api/v1/ingest", json=batch(), headers={"X-API-Key": "sai"})
    assert r.status_code == 401


def test_ingest_rejects_invalid_payload(client) -> None:
    bad = batch()
    bad["samples"][0]["frames"][0]["camera_position"] = "goc_nhin_la"
    assert client.post("/api/v1/ingest", json=bad, headers=headers()).status_code == 422


def test_health_needs_no_key(client) -> None:
    assert client.get("/health").json()["ok"] is True


# ----------------------------- ingest -----------------------------
def test_ingest_stores_and_accumulates_distinct_batches(client) -> None:
    r = client.post("/api/v1/ingest", json=batch(batch_id="b1"), headers=headers())
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["duplicate"] is False
    assert r.json()["stored_frames"] == 2

    stats = client.get("/api/v1/stats").json()
    assert stats == {"devices": 1, "sessions": 1, "samples": 1, "frames": 2,
                     "grass_frames": 1, "track_points": 1, "estimated_samples": 0,
                     "batches": 1}

    # Batch KHAC (batch_id khac) cung session -> cong don
    client.post("/api/v1/ingest", json=batch(batch_id="b2"), headers=headers())
    assert client.get("/api/v1/stats").json()["frames"] == 4
    assert client.get("/api/v1/stats").json()["batches"] == 2


def test_ingest_retrying_same_batch_id_is_idempotent(client) -> None:
    """Edge gui lai batch tu outbox (mat mang/retry) -> khong duoc luu trung."""
    b = batch(batch_id="retry-me")
    first = client.post("/api/v1/ingest", json=b, headers=headers())
    second = client.post("/api/v1/ingest", json=b, headers=headers())
    third = client.post("/api/v1/ingest", json=b, headers=headers())

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True and second.json()["stored_frames"] == 0
    assert third.json()["duplicate"] is True
    assert second.status_code == 200 and third.status_code == 200

    stats = client.get("/api/v1/stats").json()
    assert stats["frames"] == 2          # khong nhan doi
    assert stats["track_points"] == 1
    assert stats["samples"] == 1


def test_ingest_without_batch_id_dedupes_by_content(client) -> None:
    """Edge cu khong gui batch_id -> server van nhan ra trung qua sha256 noi dung."""
    client.post("/api/v1/ingest", json=batch(), headers=headers())
    client.post("/api/v1/ingest", json=batch(), headers=headers())
    assert client.get("/api/v1/stats").json()["frames"] == 2


def test_ingest_rejects_oversized_batch(client, monkeypatch) -> None:
    # batch() co 1 sample / 2 frame -> dat gioi han frame xuong 1 la vuot
    monkeypatch.setattr(api, "MAX_BATCH_FRAMES", 1)
    r = client.post("/api/v1/ingest", json=batch(batch_id="big"), headers=headers())
    assert r.status_code == 413
    assert client.get("/api/v1/stats").json()["frames"] == 0


def test_ingest_rate_limited(client, monkeypatch) -> None:
    monkeypatch.setattr(api, "_limiter", api.RateLimiter(2))
    assert client.post("/api/v1/ingest", json=batch(batch_id="r1"),
                       headers=headers()).status_code == 200
    assert client.post("/api/v1/ingest", json=batch(batch_id="r2"),
                       headers=headers()).status_code == 200
    assert client.post("/api/v1/ingest", json=batch(batch_id="r3"),
                       headers=headers()).status_code == 429


def test_devices_and_sessions(client) -> None:
    client.post("/api/v1/ingest", json=batch(batch_id="d1"), headers=headers())
    client.post("/api/v1/ingest", json=batch(session="s2", batch_id="d2"), headers=headers())

    devs = client.get("/api/v1/devices").json()
    assert len(devs) == 1
    assert devs[0]["id"] == DEVICE
    assert devs[0]["name"] == "Jetson test"
    assert devs[0]["session_count"] == 2

    sess = client.get(f"/api/v1/sessions?device_id={DEVICE}").json()
    assert {s["id"] for s in sess} == {"s1", "s2"}


def test_track_filtered_by_session(client) -> None:
    client.post("/api/v1/ingest", json=batch(session="s1", batch_id="k1"), headers=headers())
    client.post("/api/v1/ingest", json=batch(lat=12.69, session="s2", batch_id="k2"),
                headers=headers())

    all_pts = client.get(f"/api/v1/track?device_id={DEVICE}").json()
    assert all_pts["count"] == 2

    only_s2 = client.get(f"/api/v1/track?device_id={DEVICE}&session_id=s2").json()
    assert only_s2["count"] == 1
    assert only_s2["points"][0]["lat"] == pytest.approx(12.69)


def test_estimated_samples_are_flagged(client) -> None:
    """Toa do uoc luong (GPS mat fix) phai duoc danh dau ro qua stats + grass-points + track."""
    client.post("/api/v1/ingest", json=batch(estimated=True), headers=headers())
    assert client.get("/api/v1/stats").json()["estimated_samples"] == 1
    assert client.get(f"/api/v1/grass-points?device_id={DEVICE}").json()["points"][0]["estimated"] is True
    track = client.get(f"/api/v1/track?device_id={DEVICE}").json()
    assert track["points"][0]["estimated"] == 1


def test_real_gps_samples_are_not_flagged(client) -> None:
    client.post("/api/v1/ingest", json=batch(), headers=headers())
    assert client.get("/api/v1/stats").json()["estimated_samples"] == 0
    assert client.get(f"/api/v1/grass-points?device_id={DEVICE}").json()["points"][0]["estimated"] is False


def test_grass_points_only_grass_frames(client) -> None:
    client.post("/api/v1/ingest", json=batch(), headers=headers())
    pts = client.get(f"/api/v1/grass-points?device_id={DEVICE}").json()
    assert pts["count"] == 1
    assert pts["points"][0]["camera"] == "cam_left"
    assert pts["points"][0]["prob"] == pytest.approx(0.9)
    assert pts["points"][0]["lat"] == pytest.approx(12.68120)


def test_heatmap_cell_is_near_ingested_point(client) -> None:
    client.post("/api/v1/ingest", json=batch(), headers=headers())
    heat = client.get(f"/api/v1/heatmap?device_id={DEVICE}&cell_size_m=5").json()
    assert heat["total_grass_frames"] == 1
    assert heat["total_cells"] == 1
    cell = heat["cells"][0]
    assert cell["grass_hits"] == 1
    assert cell["max_prob"] == pytest.approx(0.9)
    assert cell["cameras"] == {"cam_left": 1}
    d = haversine(12.68120, 108.06420, cell["lat"], cell["lon"])
    assert d < 5.0, f"Tam o lech {d:.1f} m"


def test_heatmap_groups_nearby_points(client) -> None:
    # Dat 3 diem quanh tam 1 o (cach tam <= 0.5 m) -> chac chan cung 1 o
    clat, clon = cell_center(cell_key(12.68120, 108.06420, 5.0), 5.0)
    for i in range(3):
        client.post("/api/v1/ingest", json=batch(lat=clat + i * 0.000002,
                                                  batch_id=f"h{i}"), headers=headers())
    heat = client.get(f"/api/v1/heatmap?device_id={DEVICE}&cell_size_m=5").json()
    assert heat["total_grass_frames"] == 3
    assert heat["total_cells"] == 1
    assert heat["cells"][0]["grass_hits"] == 3


def test_empty_db_returns_empty_shapes(client) -> None:
    assert client.get("/api/v1/devices").json() == []
    assert client.get("/api/v1/stats").json()["frames"] == 0
    assert client.get("/api/v1/grass-points").json()["count"] == 0
    assert client.get(f"/api/v1/track?device_id={DEVICE}").json()["count"] == 0


def test_index_page_served(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Leaflet" in r.text or "leaflet" in r.text


# ----------------------------- xoa phien -----------------------------
def test_delete_session_requires_key(client) -> None:
    client.post("/api/v1/ingest", json=batch(batch_id="del1"), headers=headers())
    assert client.delete("/api/v1/sessions/s1").status_code == 401
    assert client.delete("/api/v1/sessions/s1",
                         headers={"X-API-Key": "sai"}).status_code == 401
    # Van con du lieu
    assert client.get("/api/v1/stats").json()["frames"] == 2


def test_delete_session_removes_all_rows(client) -> None:
    client.post("/api/v1/ingest", json=batch(batch_id="del2"), headers=headers())
    r = client.delete("/api/v1/sessions/s1", headers=headers())
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["frames"] == 2
    assert r.json()["track_points"] == 1

    stats = client.get("/api/v1/stats").json()
    assert stats["frames"] == 0 and stats["samples"] == 0
    assert stats["track_points"] == 0 and stats["batches"] == 0
    assert stats["sessions"] == 0 and stats["devices"] == 0  # het phien -> xoa device
    assert client.get("/api/v1/sessions").json() == []
    assert client.get(f"/api/v1/grass-points?device_id={DEVICE}").json()["total"] == 0

    # Xoa lan nai -> 404 (khong con phien)
    assert client.delete("/api/v1/sessions/s1", headers=headers()).status_code == 404


def test_delete_one_session_keeps_the_others(client) -> None:
    client.post("/api/v1/ingest", json=batch(batch_id="a"), headers=headers())
    client.post("/api/v1/ingest", json=batch(session="s2", batch_id="b"), headers=headers())

    client.delete("/api/v1/sessions/s1", headers=headers())
    stats = client.get("/api/v1/stats").json()
    assert stats["sessions"] == 1 and stats["frames"] == 2
    assert {s["id"] for s in client.get("/api/v1/sessions").json()} == {"s2"}


def test_delete_all_sessions_of_device(client) -> None:
    for i, s in enumerate(("s1", "s2", "s3")):
        client.post("/api/v1/ingest", json=batch(session=s, batch_id=f"x{i}"),
                    headers=headers())
    r = client.delete(f"/api/v1/devices/{DEVICE}/sessions", headers=headers())
    assert r.status_code == 200 and r.json()["sessions"] == 3
    assert client.get("/api/v1/devices").json() == []
    assert client.get("/api/v1/stats").json()["frames"] == 0
    assert client.delete(f"/api/v1/devices/{DEVICE}/sessions",
                         headers=headers()).status_code == 404


def test_session_list_shows_frame_counts(client) -> None:
    client.post("/api/v1/ingest", json=batch(batch_id="c1"), headers=headers())
    s = client.get(f"/api/v1/sessions?device_id={DEVICE}").json()[0]
    assert s["frame_count"] == 2 and s["grass_frame_count"] == 1


# ----------------------------- phan trang + loc thoi gian --------------
def test_grass_points_pagination(client) -> None:
    for i in range(5):
        client.post("/api/v1/ingest",
                    json=batch(lat=12.68120 + i * 0.0001, session=f"p{i}",
                               batch_id=f"p{i}"),
                    headers=headers())
    r = client.get(f"/api/v1/grass-points?device_id={DEVICE}&limit=2&offset=0").json()
    assert r["count"] == 2 and r["total"] == 5 and r["has_more"] is True
    p0 = {p["lat"] for p in r["points"]}

    r2 = client.get(f"/api/v1/grass-points?device_id={DEVICE}&limit=2&offset=2").json()
    assert r2["offset"] == 2 and r2["total"] == 5
    assert p0.isdisjoint({p["lat"] for p in r2["points"]})   # khong trung

    r3 = client.get(f"/api/v1/grass-points?device_id={DEVICE}&limit=100&offset=4").json()
    assert r3["count"] == 1 and r3["has_more"] is False


def test_grass_points_returns_attention_for_ui(client) -> None:
    client.post("/api/v1/ingest", json=batch(batch_id="att"), headers=headers())
    p = client.get(f"/api/v1/grass-points?device_id={DEVICE}").json()["points"][0]
    assert len(p["attention"]) == 12
    assert p["camera_position"] == "left"


def test_time_filter_on_grass_points_and_track(client) -> None:
    b = batch(batch_id="tf")
    b["samples"][0]["frames"][0]["captured_at"] = "2026-01-01T00:00:00+00:00"
    b["track"][0]["timestamp"] = "2026-01-01T00:00:00+00:00"
    client.post("/api/v1/ingest", json=b, headers=headers())

    # Nam trong khoang
    ok = client.get(f"/api/v1/grass-points?device_id={DEVICE}"
                    "&since=2025-12-31T00:00:00&until=2026-01-02T00:00:00").json()
    assert ok["total"] == 1
    # Truoc khoang
    before = client.get(f"/api/v1/grass-points?device_id={DEVICE}"
                        "&since=2026-02-01T00:00:00").json()
    assert before["total"] == 0
    # Track cung loc duoc
    assert client.get(f"/api/v1/track?device_id={DEVICE}"
                      "&since=2026-02-01T00:00:00").json()["count"] == 0
    assert client.get(f"/api/v1/track?device_id={DEVICE}"
                      "&since=2025-12-31T00:00:00").json()["count"] == 1


def test_stats_can_filter_by_session(client) -> None:
    client.post("/api/v1/ingest", json=batch(session="s1", batch_id="f1"), headers=headers())
    client.post("/api/v1/ingest", json=batch(session="s2", batch_id="f2"), headers=headers())
    only = client.get(f"/api/v1/stats?session_id=s2").json()
    assert only["frames"] == 2 and only["sessions"] == 1


def test_stats_respects_time_filter(client) -> None:
    """Bo loc tu/den ngay phai ap dung ca cho thong ke (khong chi heatmap/diem)."""
    client.post("/api/v1/ingest", json=batch(batch_id="ts1"), headers=headers())

    full = client.get(f"/api/v1/stats?device_id={DEVICE}"
                      "&since=2025-12-31T00:00:00&until=2026-01-02T00:00:00").json()
    empty = client.get(f"/api/v1/stats?device_id={DEVICE}&since=2026-02-01T00:00:00").json()
    assert full["frames"] == 2 and full["grass_frames"] == 1
    assert full["samples"] == 1 and full["track_points"] == 1
    assert empty["frames"] == 0 and empty["samples"] == 0
    assert empty["sessions"] == 0


def test_daily_stats_groups_by_day(client) -> None:
    client.post("/api/v1/ingest", json=batch(batch_id="g1"), headers=headers())
    client.post("/api/v1/ingest", json=batch(batch_id="g2"), headers=headers())
    rows = client.get(f"/api/v1/daily?device_id={DEVICE}"
                      "&since=2025-01-01T00:00:00").json()["rows"]
    assert rows and rows[0]["day"] == "2026-01-01"
    assert rows[0]["frames"] == 4 and rows[0]["grass_frames"] == 2
    assert rows[0]["samples"] == 2


# ----------------------------- map module -----------------------------
def test_cell_key_and_center_are_consistent() -> None:
    lat, lon = 12.681234, 108.064321
    key = cell_key(lat, lon, 5.0)
    clat, clon = cell_center(key, 5.0)
    assert haversine(lat, lon, clat, clon) < 5.0 * math.sqrt(2)


def test_same_cell_for_points_within_cell_size() -> None:
    assert cell_key(12.68120, 108.06420, 5.0) == cell_key(12.68122, 108.06422, 5.0)


def test_build_heatmap_keeps_max_prob_and_last_seen() -> None:
    frames = [
        {"lat": 12.6812, "lon": 108.0642, "grass_probability": 0.3,
         "captured_at": "2026-01-01T00:00:00+00:00", "camera": "cam_left"},
        {"lat": 12.6812, "lon": 108.0642, "grass_probability": 0.95,
         "captured_at": "2026-01-02T00:00:00+00:00", "camera": "cam_right"},
    ]
    cells = build_heatmap(frames, cell_size_m=5.0)
    assert len(cells) == 1
    assert cells[0]["max_prob"] == pytest.approx(0.95)
    assert cells[0]["last_seen"] == "2026-01-02T00:00:00+00:00"
    assert cells[0]["cameras"] == {"cam_left": 1, "cam_right": 1}


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
