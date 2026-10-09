# Phần 2 — Server

FastAPI + SQLite nhận batch từ phần 1, web UI Leaflet: heatmap cỏ, track di chuyển, click điểm cỏ để chỉ đường Google Maps.

## Chạy

```bash
cd server
uv sync
uv run weed-server --port 8000
# Mở http://localhost:8000
uv run pytest -q                 # test ingest + auth + REST + chia ô heatmap
```

## Env

| Env | Mặc định | Ý nghĩa |
|---|---|---|
| `WEED_SERVER_API_KEY` | `change-me-please` | Kiểm tra header `X-API-Key` của edge (**đổi khi prod**, rỗng = tắt kiểm tra) |
| `WEED_SERVER_DB` | `data/weed_server.db` | File SQLite |
| `WEED_SERVER_HOST` / `WEED_SERVER_PORT` | `0.0.0.0` / `8000` | Bind |

## API

| Endpoint | Mô tả |
|---|---|
| `POST /api/v1/ingest` | Edge đẩy batch (header `X-API-Key`) — contract trong `schemas.py` |
| `GET /health` | Health check |
| `GET /api/v1/stats` | Tổng quan số liệu |
| `GET /api/v1/devices` | Danh sách thiết bị |
| `GET /api/v1/sessions?device_id=` | Phiên làm việc |
| `GET /api/v1/track?device_id=&session_id=` | Lộ trình GPS |
| `GET /api/v1/heatmap?cell_size_m=&device_id=&session_id=` | Cells heatmap (gom frame cỏ theo ô lưới) |
| `GET /api/v1/grass-points?device_id=` | Điểm cỏ chi tiết để chỉ đường |

## Web UI

- **Heatmap**: gradient đỏ (prob cao → thấp), kích thước ô lưới chỉnh được.
- **Track**: polyline xanh lộ trình di chuyển, chấm sáng = vị trí hiện tại.
- **Điểm cỏ**: danh sách bên trái, click → bay tới trên map, nút OK → mở Google Maps chỉ đường từ vị trí bạn → đúng điểm cỏ.

## Deploy server thật

```bash
sudo cp deploy/weed-server.service /etc/systemd/system/
# sửa User/WorkingDirectory cho đúng
sudo systemctl daemon-reload && sudo systemctl enable --now weed-server
```

Đặt reverse proxy (nginx/Caddy) trước khi expose ra ngoài, và **đổi API key**.
