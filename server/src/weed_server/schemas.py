"""Schemas nhan du lieu tu edge.

QUAN TRONG: cac model nay phai trung khop voi edge/src/weed_edge/schemas.py
(contract giua phan 1 va phan 2). Neu edge doi, doi theo o day.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GpsFix(BaseModel):
    lat: float
    lon: float
    alt: float | None = None
    satellites: int | None = None
    hdop: float | None = None
    fix_time: str | None = None
    # True = toa do uoc luong (edge mat GPS fix) — UI phai hien thi khac
    estimated: bool = False


class FrameResult(BaseModel):
    captured_at: str
    camera: str
    camera_position: Literal["left", "right", "center", "other"] = "other"
    grass_probability: float
    is_grass: bool
    patch_attention: list[float] = Field(default_factory=list, max_length=12)
    threshold: float
    model_device: str = "cpu"
    inference_ms: float = 0.0


class CaptureSample(BaseModel):
    gps: GpsFix
    frames: list[FrameResult]
    cell_lat: float | None = None
    cell_lon: float | None = None


class TrackPoint(BaseModel):
    lat: float
    lon: float
    timestamp: str
    speed_mps: float | None = None
    estimated: bool = False


class IngestBatch(BaseModel):
    device_id: str
    device_name: str | None = None
    created_at: str
    samples: list[CaptureSample]
    track: list[TrackPoint] = Field(default_factory=list)
    session_id: str
    schema_version: int = 1
    # Ma duy nhat cua batch. Edge tao 1 lan khi flush va giu nguyen khi retry
    # tu outbox -> server dung de khong luu trung. Thieu field nay (edge cu) thi
    # server tu tinh sha256 tu noi dung lam batch_id.
    batch_id: str | None = None
