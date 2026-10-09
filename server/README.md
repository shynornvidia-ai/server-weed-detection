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

### Cách A — Render free (khuyến nghị, không cần máy chủ riêng)

Repo có `render.yaml` ở gốc (Blueprint). Các bước:

1. Push repo lên GitHub (đã có remote `origin`).
2. Vào [dashboard.render.com](https://dashboard.render.com) → **New +** → **Blueprint** → chọn repo → Render tự đọc `render.yaml`.
3. Sau khi deploy xong, lấy 2 thứ:
   - URL service: `https://weed-server.onrender.com` (Environment → Name)
   - API key: giá trị env `WEED_SERVER_API_KEY` (Render tự generate, xem trong Environment)
4. Dán vào `edge/config/config.yaml`: `server.base_url` + `server.api_key`, rồi `sudo systemctl restart weed-edge`.

**Lưu ý Render free (quan trọng):**

| Hạng mục | Thực tế |
|---|---|
| Sleep | Server ngủ sau 15 phút idle; request đầu wake ~1 phút. Edge tự xử lý: retry fail → outbox → gửi lại 30s sau → lúc này server đã awake. |
| **SQLite** | Filesystem ephemeral → **DB bị xóa mỗi lần sleep/restart/redeploy**. Dữ liệu chỉ giữ trong phiên gần nhất. |
| Muốn giữ DB | Nâng `$7/tháng` + attach Persistent Disk, hoặc chuyển sang Neon Postgres free (sửa `db.py`). |
| Giữ luôn awake (không sleep) | Đặt cron/free pinger (uptimerobot) gọi `/health` mỗi 10 phút — nhưng **vẫn mất DB nếu redeploy**. |
| Giữ nguyên outbox trên Jetson | Muốn server mất DB rồi vẫn khôi phục được: đừng xóa outbox ngay — xem mục "phục hồi dữ liệu" dưới đây. |

**Phục hồi dữ liệu khi server mất DB (Render free):** vì server idempotent theo `batch_id`,
bạn có thể copy lại toàn bộ `edge/data/outbox/` (giữ bản cũ, không xóa sau khi gửi) và
re-send để dựng lại DB. Nếu muốn tự động hẳn thì cần sửa edge để giữ archive N ngày —
nói rõ nếu cần, sẽ làm.

### Cách B — VPS/Ubuntu riêng (systemd)

```bash
sudo cp deploy/weed-server.service /etc/systemd/system/
# sửa User/WorkingDirectory cho đúng
sudo systemctl daemon-reload && sudo systemctl enable --now weed-server
```

Đặt reverse proxy (nginx/Caddy) trước khi expose ra ngoài, và **đổi API key**.

### Test Docker local (trước khi deploy)

```bash
docker build -t weed-server .
docker run -p 8000:8000 -e WEED_SERVER_API_KEY=your-key weed-server
# mở http://localhost:8000
```
