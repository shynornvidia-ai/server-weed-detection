"""Tinh heatmap cells tu danh sach frame grass — dung chung logic o luoi voi edge."""

from __future__ import annotations

import math
from collections import defaultdict

M_PER_DEG_LAT = 111_320.0


def cell_key(lat: float, lon: float, cell_size_m: float) -> tuple[int, int]:
    row = math.floor(lat * M_PER_DEG_LAT / cell_size_m)
    col = math.floor(lon * M_PER_DEG_LAT * math.cos(math.radians(lat)) / cell_size_m)
    return row, col


def cell_center(key: tuple[int, int], cell_size_m: float) -> tuple[float, float]:
    row, col = key
    lat_center = (row + 0.5) * cell_size_m / M_PER_DEG_LAT
    lon_center = (col + 0.5) * cell_size_m / (M_PER_DEG_LAT * math.cos(math.radians(lat_center)))
    return lat_center, lon_center


def build_heatmap(grass_frames: list[dict], cell_size_m: float = 5.0) -> list[dict]:
    """Gom cac frame grass vao o luoi. Tra ve danh sach cell de web ve heatmap.

    Moi cell: lat/lon tam, grass_hits (so frame co), max_prob, last_seen.
    """
    cells: dict[tuple[int, int], dict] = {}
    for f in grass_frames:
        key = cell_key(f["lat"], f["lon"], cell_size_m)
        c = cells.get(key)
        if c is None:
            lat_c, lon_c = cell_center(key, cell_size_m)
            c = {"lat": lat_c, "lon": lon_c, "grass_hits": 0, "max_prob": 0.0,
                 "first_seen": f["captured_at"], "last_seen": f["captured_at"],
                 "cameras": defaultdict(int)}
            cells[key] = c
        c["grass_hits"] += 1
        c["max_prob"] = max(c["max_prob"], f["grass_probability"])
        c["last_seen"] = max(c["last_seen"], f["captured_at"])
        c["cameras"][f.get("camera") or "?"] += 1

    out = []
    for c in cells.values():
        out.append({
            "lat": c["lat"],
            "lon": c["lon"],
            "grass_hits": c["grass_hits"],
            "max_prob": round(c["max_prob"], 4),
            "first_seen": c["first_seen"],
            "last_seen": c["last_seen"],
            "cameras": dict(c["cameras"]),
        })
    out.sort(key=lambda c: -c["max_prob"])
    return out
