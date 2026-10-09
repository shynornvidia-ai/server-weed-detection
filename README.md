# Weed Detection System (MVP)

Hệ thống phát hiện cỏ dại bằng AI trên **Jetson Orin** (phần 1) + **Web server** visualize bản đồ heatmap cỏ (phần 2).

```
┌─────────────────────────────────┐        HTTP POST         ┌──────────────────────────┐
│  PHẦN 1 — EDGE (Jetson Orin)    │   /api/v1/ingest         │  PHẨN 2 — SERVER         │
│                                 │ ───────────────────────► │  (FastAPI + SQLite)      │
│  cam trái ─┐                    │   (batch JSON + API key) │                          │
│  cam phải ─┼→ capture → queue → │                          │  REST /api/v1/*          │
│  GPS USB ──┘   (khi di chuyển)  │   outbox nếu mất mạng    │  → / (web UI Leaflet)    │
│  AttentionMIL (best_model.pt)   │                          │  heatmap + track + chỉ   │
│  heatmap ô lưới 5m              │                          │  đường tới điểm cỏ       │
└─────────────────────────────────┘                          └──────────────────────────┘
```

## Cấu trúc

| Thư mục | Là gì | Chạy ở đâu |
|---|---|---|
| `edge/` | Phần 1: capture + predict + push (Python, PyTorch, uv) | Jetson Orin |
| `server/` | Phần 2: nhận dữ liệu + web UI (FastAPI, uv) | Server bất kỳ |
| `model/best_model.pt` | Checkpoint AttentionMIL (`torch.save` state_dict) | — |
| `notebook/weed-daklak-solution.ipynb` | Notebook train gốc (tham khảo kiến trúc + preprocess) | — |

Hai phần **độc lập hoàn toàn** (pyproject riêng, uv riêng, deploy riêng) — chỉ gặp nhau ở contract JSON `/api/v1/ingest` (xem `edge/src/weed_edge/schemas.py` ≡ `server/src/weed_server/schemas.py`).

> 🧭 **Hệ thống hiện có gì và chạy được gì: [SYSTEM.md](SYSTEM.md).**
> 📘 **Toàn bộ lệnh cần chạy, và mỗi lệnh để làm gì: [RUNBOOK.md](RUNBOOK.md).**

## Chạy nhanh (mock, không cần hardware)

```bash
# Terminal 1 — server
cd server && uv sync && uv run weed-server --port 8000

# Terminal 2 — edge (mock camera + mock GPS, tự sinh dữ liệu di chuyển)
cd edge && uv sync && ./setup.sh   # lần đầu
uv run weed-edge run
```

Mở `http://<server>:8000` → chọn thiết bị → xem heatmap cỏ + lộ trình, click điểm cỏ để chỉ đường.

## Test

```bash
cd edge   && uv run pytest -q    # 59 test (chạy ~30s, có GPU thì nhanh hơn)
cd server && uv run pytest -q    # 15 test (~2s)
```

- `edge/tests/test_model.py` — so sánh preprocess của service với bản reimplementation **độc lập từ notebook** (`allclose` 1e-5) và load checkpoint bằng `strict=True` ⇒ kiến trúc + tiền xử lý không bị trôi khi sửa code.
- `edge/tests/test_pipeline.py` — chạy pipeline thật với mock camera + mock GPS + model giả + chặn mạng: kiểm tra **cả 2 camera đều capture**, lọc frame không cỏ, retry/outbox.
- `server/tests/test_api.py` — ingest + API key + REST cho web UI + toán chia ô heatmap (phải khớp với lưới ở edge).

## Hardware thật (đã cấu hình & kiểm tra)

2 camera UGREEN 4K (USB) + GPS u-blox 7 (USB) đã nhận diện và cấu hình sẵn trong `edge/config/config.yaml`:

- `cameras[].mock: false`; dùng symlink ổn định `/dev/weed-cam-left` (cổng USB `2.3`) và `/dev/weed-cam-right` (cổng USB `2.4`). Mỗi camera UVC có **2 node** `/dev/video*` (index 0 = capture, index 1 = metadata không mở được) nên nếu dùng index thì là `0` và `2`.
- `gps.mock: false`, `port: /dev/weed-gps` (symlink udev → `/dev/ttyACM0`), `baudrate: 9600`.
- Symlink do `edge/deploy/99-weed-edge.rules` tạo — `edge/setup.sh` tự cài, hoặc copy thủ công rồi `sudo udevadm trigger --action=add`.
- Kiểm tra nhanh: `cd edge && uv run weed-edge check` (mở thật từng camera đọc 1 frame + đọc NMEA thật từ GPS).
- Theo dõi GPS đến khi có fix: `cd edge && uv run weed-edge gps` (in số vệ tinh + toạ độ mỗi giây).

Lưu ý: GPS cần ăng-ten thoáng ngoài trời mới có fix (≥ 4 vệ tinh). Trong nhà `check` sẽ báo chưa có fix; khi đó sau `gps.fallback_after_s` (mặc định 60s) hệ thống **vẫn capture bằng toạ độ mặc định và đánh dấu ước lượng** (không đứng im khi ra đồng), và luôn tiếp tục thử kết nối GPS. Xem mục “GPS mất fix” trong `edge/README.md`.

Chi tiết: `edge/README.md`, `server/README.md`.
