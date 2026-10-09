# SYSTEM — Hệ thống hiện có những gì và chạy được gì

> Ảnh chụp trạng thái tại **01/10/2026**. Lệnh cần chạy: xem [RUNBOOK.md](RUNBOOK.md).

Hệ thống phát hiện cỏ dại bằng AI: **Jetson Orin gắn 2 camera + 1 GPS** thu thập khi di chuyển,
suy luận bằng mạng `AttentionMIL`, gom kết quả thành **heatmap ô lưới theo toạ độ**, đẩy lên
**server web** để xem bản đồ và chỉ đường tới từng điểm cỏ.

```
┌──────────────────────────────────────┐        HTTP POST          ┌────────────────────────────┐
│ PHẦN 1 — EDGE  (Jetson Orin Nano)    │   /api/v1/ingest          │ PHẦN 2 — SERVER            │
│                                      │ ────────────────────────► │ (FastAPI + SQLite)         │
│  2 camera USB ─┐                     │   batch JSON + X-API-Key  │                            │
│  1 GPS USB ────┼─► capture ─► queue  │                           │  REST /api/v1/*            │
│  (chỉ khi di   │   (kèm GPS)         │   mất mạng ─► outbox      │  Web UI Leaflet: heatmap,  │
│   chuyển)      │        │            │   ─► gửi lại sau          │  lộ trình, click điểm cỏ   │
│                │   AttentionMIL      │                           │  ─► chỉ đường Google Maps  │
│                │   (best_model.pt)   │                           │                            │
│                └─► heatmap 5m/ô      │                           │  SQLite: devices, sessions,│
│                                      │                           │  samples, frames, track    │
└──────────────────────────────────────┘                           └────────────────────────────┘
```

Hai phần **độc lập hoàn toàn**: `pyproject.toml` + `uv.lock` + entrypoint riêng, deploy riêng.
Chúng chỉ gặp nhau ở **hợp đồng JSON** `/api/v1/ingest` (schemas hai bên trùng khớp).

---

## 1. Chạy được gì ngay bây giờ

| Việc | Lệnh | Trạng thái |
|---|---|---|
| Kiểm tra phần cứng (mở thật camera, đọc NMEA thật) | `weed-edge check` | ✅ chạy được, đã xác nhận trên máy thật |
| Chạy service thu thập + suy luận + đẩy lên server | `weed-edge run` | ✅ đã chạy thật, đã đẩy 637 frame lên server |
| Suy luận 1 ảnh để kiểm tra model | `weed-edge predict-image anh.jpg` | ✅ |
| Chạy **không cần phần cứng** (mock camera + mock GPS) | `WEED_EDGE_MOCK_GPS=true weed-edge run` | ✅ |
| Server web + bản đồ | `weed-server` | ✅ đang chạy ở `http://localhost:8000` |
| Test tự động | `pytest -q` | ✅ 69 test edge + 17 test server |
| Chạy nền dạng daemon (systemd) | unit files đã có | ⚠️ **chưa cài** (`systemctl is-enabled` → `not-found`) |
| Xác nhận chất lượng phát hiện trên cỏ thật | — | ⚠️ **chưa kiểm chứng** (camera mới chỉ nhìn cảnh trong nhà, `grass=0`) |

---

## 2. PHẦN 1 — EDGE (Jetson Orin)

Môi trường đã verify: JetPack 7.2.1 / L4T R39.2, kernel 6.8, CUDA 13.2, Python 3.12,
`torch 2.14.0+cu132`, `torch.cuda.is_available() == True`, device `Orin`.
torch/torchvision lấy từ index chính thức `download.pytorch.org/whl/cu132`.

### 2.1 Các module

| Module | Làm gì |
|---|---|
| `config.py` | Nạp YAML + override bằng env `WEED_EDGE_*`; resolve đường dẫn model/data theo vị trí file config. Ưu tiên `config/config.yaml`, fallback `config.example.yaml`. |
| `cameras.py` | `UsbCamera` (OpenCV/V4L2 + MJPG, xoay theo `rotation`) và `MockCamera` (sinh frame cỏ giả). Mỗi camera chạy 1 thread riêng. |
| `gps.py` | `GpsReader`: đọc NMEA qua pyserial + pynmea2, tự kết nối lại khi mất cổng; `GpsConfig`, `GpsFixData`, `haversine_m`. Có **chế độ dự phòng** khi mất fix. |
| `motion.py` | `MotionGate`: chỉ cho capture khi đã đi đủ `min_distance_m` (hoặc quá `max_interval_s`). Mỗi camera một gate riêng. |
| `queue_mgr.py` | `CaptureQueue`: hàng đợi có giới hạn, giữ frame + metadata GPS, bỏ frame cũ nhất khi đầy (không chặn capture). |
| `model.py` | `AttentionMIL` — kiến trúc khớp **chính xác** notebook: EfficientNet-B0 4 kênh, gated attention, `bmm`, classifier. Kèm tiền xử lý đúng notebook (resize 384×512 → LBP 8,1,uniform → 4 kênh → grid 3×4 → Normalize). Có `patch_centers()` trả 12 tâm patch. |
| `aggregator.py` | `HeatmapAggregator`: gom kết quả vào ô lưới `cell_size_m`, giữ histogram nhỏ mỗi ô, ring buffer giới hạn số ô. |
| `uploader.py` | `Uploader`: POST batch lên server (retry + backoff). `Outbox`: lưu batch chưa gửi được ra file JSON, tự gửi lại khi có mạng. |
| `pipeline.py` | `EdgePipeline`: nối tất cả lại; `PendingBuffer` gom sample + track; vòng lặp upload; log trạng thái mỗi 5s. |
| `schemas.py` | Pydantic models của hợp đồng dữ liệu (xem mục 4). |
| `main.py` | CLI: `run` / `check` / `predict-image`. |

### 2.2 Luồng dữ liệu

```
[cam thread 1] ─┐
[cam thread 2] ─┼─► MotionGate (theo GPS) ─► CaptureItem(frame + gps) ─► CaptureQueue
[gps thread]  ──┘                                                              │
                                                                      [infer worker]
                                                    preprocess đúng notebook ─► AttentionMIL
                                                              │ prob + attention 12 patch
                                          HeatmapAggregator (ô lưới 5 m)
                                                              │
                                              PendingBuffer (samples + track)
                                                              │
                                                  [upload thread] POST /api/v1/ingest
                                                       ├── OK  → xong
                                                       └── lỗi → outbox/*.json → gửi lại sau
```

### 2.3 Phần cứng đã gắn (xác nhận trên máy thật)

| Thiết bị | Nhận dạng | Symlink ổn định |
|---|---|---|
| Camera trái | UGREEN 4K, cổng USB `2.3` | `/dev/weed-cam-left` → `/dev/video0` |
| Camera phải | UGREEN 4K, cổng USB `2.4` | `/dev/weed-cam-right` → `/dev/video2` |
| GPS | u-blox 7 (GPS-only), `ANTSTATUS=OK` | `/dev/weed-gps` → `/dev/ttyACM0` |
| Chuột | Xenta 2.4G | — (không dùng) |

- Mỗi camera UVC chiếm **2 node** `/dev/video*` (index 0 = capture, index 1 = metadata không mở được).
- Hai camera **dùng chung serial** nên `/dev/v4l/by-id` trỏ sai → phải đặt tên theo **cổng USB** (udev rule `edge/deploy/99-weed-edge.rules`).
- Camera: MJPG 1920×1080@30, chạy đồng thời tốt.

### 2.4 Xử lý đặc biệt trong edge

- **Khi di chuyển**: `motion.mode: gps` — không di chuyển thì không capture (tiết kiệm dữ liệu).
- **GPS mất fix**: sau `gps.fallback_after_s` (mặc định 60s) dùng lại **vị trí fix thật cuối cùng của phiên**, đánh dấu `estimated=true`. **Chưa từng có fix → không ước lượng** (không bịa toạ độ). Thread GPS luôn tiếp tục thử kết nối lại.
- **Không có toạ độ mặc định** trong config — lấy vị trí ở đâu thì dùng ở đó.
- **Mất mạng**: batch vào `outbox`, tự gửi lại khi có mạng.
- **Model**: nếu fp16 cho ra giá trị không hợp lệ thì tự fallback fp32.
- **Tải model**: hỗ trợ `.pt`/`.pth`/`.zip` (zip chứa state dict) và báo lỗi kèm gợi ý nếu thiếu file.

---

## 3. PHẦN 2 — SERVER

| Module | Làm gì |
|---|---|
| `api.py` | FastAPI app: endpoint ingest + REST cho web UI + phục vụ `index.html` |
| `db.py` | SQLite: schema + ghi batch + truy vấn; có **migration** tự thêm cột mới (`estimated`) cho DB cũ |
| `map.py` | Tính ô heatmap từ danh sách frame — **dùng chung công thức ô lưới với edge** (có test đối chiếu) |
| `schemas.py` | Schemas nhận từ edge (phải trùng với `edge/.../schemas.py`) |
| `static/index.html` | Web UI: Leaflet + leaflet.heat + OSM tiles |

### 3.1 REST API

| Method | Endpoint | Làm gì |
|---|---|---|
| POST | `/api/v1/ingest` | Nhận batch từ edge (bắt buộc header `X-API-Key`) |
| GET | `/health` | Kiểm tra sống chết (không cần key) |
| GET | `/api/v1/stats` | Tổng: devices, sessions, samples, frames, grass_frames, track_points, **estimated_samples** |
| GET | `/api/v1/devices` | Danh sách thiết bị + số phiên |
| GET | `/api/v1/sessions` | Danh sách phiên (lọc theo `device_id`) |
| GET | `/api/v1/track` | Lộ trình di chuyển (điểm kèm cờ `estimated`) |
| GET | `/api/v1/heatmap` | Các ô lưới có cỏ (`cell_size_m` tuỳ chọn) |
| GET | `/api/v1/grass-points` | Từng điểm cỏ chi tiết (để click chỉ đường) |
| GET | `/` | Web UI |

### 3.2 Web UI

- Bản đồ Leaflet + nền OpenStreetMap.
- **Heatmap** xác suất cỏ (leaflet.heat, đỏ = xác suất cao).
- **Lộ trình** di chuyển (`polyline`); đoạn ước lượng vẽ **nét đứt màu cam**.
- **Danh sách điểm cỏ**, click để bay tới; click marker để **mở chỉ đường Google Maps**.
- Thống kê có dòng **"⚠ Mẫu ước lượng"**; điểm/cảnh báo ước lượng tô màu cam và ghi rõ trong popup.
- Chọn thiết bị / phiên, chỉnh kích thước ô lưới, nút tự động tải lại 15s.

### 3.3 Bảng dữ liệu SQLite

`devices` — `sessions` — `samples` (kèm `estimated`) — `frames` (xác suất + attention 12 patch) — `track_points` (kèm `estimated`).

---

## 4. Hợp đồng dữ liệu giữa 2 phần

`edge/src/weed_edge/schemas.py` ≡ `server/src/weed_server/schemas.py`:

| Model | Nội dung |
|---|---|
| `GpsFix` | lat, lon, alt, satellites, hdop, fix_time, **estimated** |
| `FrameResult` | captured_at, camera, camera_position (left/right/center/other), grass_probability, is_grass, patch_attention (12), threshold, model_device, inference_ms |
| `CaptureSample` | gps + frames + cell_lat/cell_lon |
| `TrackPoint` | lat, lon, timestamp, speed_mps, **estimated** |
| `IngestBatch` | device_id, device_name, created_at, samples, track, session_id, `schema_version=1` |

---

## 5. Cấu hình

`edge/config/config.yaml` (runtime, không commit) — tạo từ `config.example.yaml`. Các nhóm:

| Nhóm | Nội dung |
|---|---|
| `device` | id, tên, `data_dir` (**không có toạ độ**) |
| `model` | path checkpoint, device, fp16, batch, queue_max, threshold (0.10 theo notebook) |
| `cameras` | 2 camera: mock, device (symlink hoặc index), width/height/fps, interval, rotation, position |
| `gps` | mock, port, baudrate, min_satellites, **fallback_after_s**, mock_start_* (chỉ cho mock) |
| `motion` | mode `gps`/`always`, min_distance_m, max_interval_s, max_hdop_m |
| `heatmap` | cell_size_m, max_cells, max_samples_per_cell, upload_only_grass_cells |
| `server` | base_url, endpoint, api_key, timeout, retry, upload_interval, batch_size |
| `outbox` | enabled, dir, max_batches, retry_interval_s |
| `logging` | level, file, rotate |

Server: env `WEED_SERVER_HOST/PORT/DB/API_KEY`.
Edge: env `WEED_EDGE_CONFIG/SERVER_URL/API_KEY/DEVICE_ID/DATA_DIR/LOG_LEVEL/MOCK_CAMERA/MOCK_GPS`.

---

## 6. Kiểm thử (86 test, tất cả pass)

| File | Kiểm gì |
|---|---|
| `edge/tests/test_model.py` | Preprocess service khớp bản dựng lại **độc lập từ notebook** (`allclose 1e-5`); checkpoint load `strict=True` ⇒ không bị trôi kiến trúc/tiền xử lý khi sửa code |
| `edge/tests/test_gps.py` | Parse NMEA thật (GGA/RMC/GSV/GSA), HDOP từ `horizontal_dil`, min_satellites, và **chế độ dự phòng** (không bịa toạ độ khi chưa từng có fix) |
| `edge/tests/test_motion.py` | Gate di chuyển, HDOP kém, fix 0 vệ tinh, fix ước lượng, 2 gate độc lập |
| `edge/tests/test_pipeline.py` | Pipeline thật với mock camera/GPS + model giả + chặn mạng: cả 2 camera đều capture, retry/outbox |
| `edge/tests/test_aggregator.py` | Gom ô lưới, giới hạn mẫu mỗi ô, ring buffer |
| `edge/tests/test_queue_mgr.py` | Hàng đợi bounded, drop-oldest |
| `edge/tests/test_uploader.py` | Gửi batch, retry, outbox |
| `edge/tests/test_config.py` | Ưu tiên `config.yaml`, env override, đường dẫn model tồn tại |
| `server/tests/test_api.py` | Ingest + API key + 422 + REST + ô heatmap + cờ `estimated` |

---

## 7. Bằng chứng đã chạy thật

- `weed-edge check`: model OK trên **cuda** (`Orin`), **cả 2 camera mở được** `(1080, 1920, 3)`, GPS đọc được NMEA đủ loại câu.
- **Chạy thật với GPS có fix**: `sats=10`, toạ độ thật `10.96853, 106.80444`, `est=0` → server nhận `samples 595→637`, `track_points 308→311`, `estimated_samples` **không tăng**.
- **Chạy thật khi GPS mất fix** (đo với `fallback_after_s=5` để quan sát nhanh): log `GPS mat fix 6s (> 5s) -> dung lai VI TRI CUOI ... va danh dau UOC LUONG`; server nhận `estimated_samples: 12`; track có điểm ước lượng. Trước đây khi mất fix hệ thống **đứng im hoàn toàn**.
- **Đường ống trọn vẹn** (camera thật → server): 66 frame thật → infer → 7 batch → session mới trên server.
- GPS: cold start **~49 giây** lần đầu (chưa có almanac); các lần sau **~5 giây** (hot start). Mỗi lần mở cổng serial module reset, nên mỗi lần khởi động chương trình là một lần bắt đầu lại.

---

## 8. Giới hạn đã biết / chưa làm

1. **Chưa cài systemd service** → hiện phải gõ lệnh thủ công, chưa tự khởi động sau reboot.
2. **Chưa kiểm chứng chất lượng phát hiện trên cỏ thật** — mọi lần chạy thật cho `grass=0` vì camera đang nhìn cảnh trong nhà. Model đúng về mặt kỹ thuật nhưng chưa xác nhận ngoài đồng.
3. **Chưa test khi thực sự di chuyển** — `motion.mode: gps` mới kiểm chứng ở trạng thái đứng yên và qua mock.
4. **Ingest chưa idempotent** — nếu gửi lại cùng batch (retry), server ghi trùng.
5. GPS là **GPS-only** (u-blox 7) — trong nhà chỉ nghe 1–2 vệ tinh; cần ăng-ten thoáng.
6. Ăng-ten là patch gắn liền, không có ăng-ten ngoài.

Nói ngắn: **đường ống đã xong và chạy được với phần cứng thật; phần còn thiếu là vận hành (service) và kiểm chứng giá trị thực địa.**
