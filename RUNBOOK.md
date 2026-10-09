# RUNBOOK — Cần chạy những gì, và mỗi cái để làm gì

> Muốn biết hệ thống **có gì / chạy được gì / giới hạn nào**: xem [SYSTEM.md](SYSTEM.md).

Hệ thống gồm **2 phần chạy độc lập** (mỗi phần có `pyproject.toml` + `uv.lock` riêng):

```
[PHẦN 1: edge]  Jetson Orin  ──HTTP POST /api/v1/ingest──►  [PHẦN 2: server]  Ubuntu/VPS
 camera + GPS + model                                        FastAPI + SQLite + web UI
```

- **Phần 1 (`edge/`)** chạy trên **Jetson Orin** (nơi có camera + GPS).
- **Phần 2 (`server/`)** chạy trên máy chủ (hoặc chính Jetson nếu chỉ test).

---

## 1. Chuẩn bị (chạy MỘT LẦN)

| Lệnh | Tác dụng |
|---|---|
| `cd edge && ./setup.sh` | Cài `uv` nếu chưa có, `uv sync` (tải torch ~800MB lần đầu), tạo `config/config.yaml` từ example nếu chưa có, **cài udev rule** cho camera+GPS (`deploy/99-weed-edge.rules`), thêm user vào group `dialout`, rồi chạy `weed-edge check`. |
| `cd server && uv sync` | Tạo môi trường ảo + cài FastAPI/uvicorn/pydantic cho server. |

Sau `setup.sh`, **đăng xuất/đăng nhập lại** nếu nó vừa thêm bạn vào group `dialout` (để quyền có hiệu lực). Riêng GPS thì udev rule đã đặt `MODE=0666` nên đọc được ngay, không cần chờ.

Kiểm tra udev đã tạo symlink chưa:

```bash
ls -la /dev/weed-cam-left /dev/weed-cam-right /dev/weed-gps
```

Nếu thiếu, cài lại rule thủ công:

```bash
sudo cp edge/deploy/99-weed-edge.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add      # phải là --action=add mới tạo lại symlink
```

---

## 2. PHẦN 2 — SERVER

| Lệnh | Tác dụng |
|---|---|
| `cd server && uv run weed-server` | Chạy web server (uvicorn) ở `0.0.0.0:8000`. Nhận batch từ edge, lưu SQLite, phục vụ web UI. |
| `uv run weed-server --host 0.0.0.0 --port 8000 --db data/weed_server.db` | Như trên nhưng chỉ định rõ host/port/đường dẫn DB. |
| `uv run pytest -q` | Chạy 17 test: ingest + API key + REST cho web UI + toán chia ô heatmap. |
| Mở `http://<ip-server>:8000` | Web UI: chọn thiết bị → xem heatmap cỏ + lộ trình, click điểm cỏ để chỉ đường (Google Maps), xem thống kê (gồm "⚠ Mẫu ước lượng"). |

Biến môi trường (không cần sửa code):

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `WEED_SERVER_HOST` | `0.0.0.0` | Địa chỉ bind |
| `WEED_SERVER_PORT` | `8000` | Cổng |
| `WEED_SERVER_DB` | `data/weed_server.db` | File SQLite |
| `WEED_SERVER_API_KEY` | `change-me-please` | Khoá edge phải gửi ở header `X-API-Key` |

> **Đổi `WEED_SERVER_API_KEY` ở cả 2 phía khi lên thật.** Nếu lệch khoá, edge sẽ trả 401 và đẩy batch vào `outbox`.

REST sẵn có: `/health`, `/api/v1/stats`, `/api/v1/devices`, `/api/v1/sessions`, `/api/v1/track`, `/api/v1/heatmap`, `/api/v1/grass-points`.

---

## 3. PHẦN 1 — EDGE (Jetson)

| Lệnh | Tác dụng |
|---|---|
| `cd edge && uv run weed-edge check` | **Kiểm tra môi trường, không chạy thật**: load model + predict thử, mở **thật từng camera đọc 1 frame**, mở GPS đọc NMEA thật, in trạng thái torch/CUDA, gọi thử `/health` của server. |
| `uv run weed-edge run` | **Chạy service chính**: 2 camera capture → gắn GPS → model AttentionMIL → gom ô lưới heatmap → đẩy lên server (mất mạng thì lưu `outbox`). |
| `uv run weed-edge ui` | **Màn hình cảm ứng**: màn hình khởi động (kiểm tra phần cứng + nút khởi động) → 2 ô camera trực tiếp + trạng thái GPS + nút tắt. Chạy bằng service `weed-edge-ui`. |
| `uv run weed-edge predict-image duong/dan/anh.jpg` | Predict 1 ảnh trên đĩa: in `grass_probability`, attention 12 patch, thời gian suy luận — để kiểm tra model đúng chưa. |
| `uv run pytest -q` | Chạy 69 test: preprocess vs notebook, load checkpoint `strict=True`, motion/queue/aggregator/uploader/pipeline, GPS NMEA + fallback. |
| `-c config/khac.yaml` (thêm vào lệnh bất kỳ) | Dùng file config khác. Ví dụ: `uv run weed-edge -c config/config.yaml run`. |

Biến môi trường (override không cần sửa YAML):

| Biến | Ý nghĩa |
|---|---|
| `WEED_EDGE_CONFIG` | Đường dẫn file config |
| `WEED_EDGE_SERVER_URL` | `server.base_url` (trỏ về server thật) |
| `WEED_EDGE_API_KEY` | `server.api_key` |
| `WEED_EDGE_DEVICE_ID` | `device.id` (mỗi máy một ID) |
| `WEED_EDGE_DATA_DIR` | `device.data_dir` |
| `WEED_EDGE_LOG_LEVEL` | `logging.level` |
| `WEED_EDGE_MOCK_CAMERA=true` | Bật chế độ camera giả cho **tất cả** camera |
| `WEED_EDGE_MOCK_GPS=true` | Bật GPS giả (để test đường ống khi chưa ra trời) |

Ví dụ test không cần trời: `WEED_EDGE_MOCK_GPS=true uv run weed-edge run`

---

## 4. Thứ tự chạy thực tế trên đồng

```bash
# 1) Trên máy server (hoặc Jetson nếu test local)
cd server && uv run weed-server

# 2) Trên Jetson — kiểm tra phần cứng trước (camera + GPS)
cd edge && uv run weed-edge check

# 3) Chạy thật — để GPS hướng lên trời, chờ có fix
uv run weed-edge run
```

Khi chạy, log mỗi 5s in một dòng `[status]` cho biết: GPS đang thật hay ước lượng, số frame đã suy luận, số ô heatmap, số batch đã gửi / còn trong outbox.

---

## 5. Chạy như service (tự khởi động cùng máy)

### 5a. Máy có màn hình cảm ứng (mặc định)

Boot lên → GDM **auto-login** → màn hình khởi động của `weed-edge ui` hiện ra → ấn **KHỞI ĐỘNG HỆ THỐNG** → hiện 2 ô camera + GPS; ấn **TẮT HỆ THỐNG** để dừng.

```bash
sudo cp edge/deploy/weed-edge-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weed-edge-ui
journalctl -u weed-edge-ui -f        # xem log
sudo systemctl restart weed-edge-ui  # khởi động lại UI
```

Auto-login (để có màn hình X khi boot) — `/etc/gdm3/custom.conf`, phần `[daemon]`:

```ini
AutomaticLoginEnable=true
AutomaticLogin=jetson
```

`edge/setup.sh` đã cài unit này và chỉ dẫn bật auto-login.

> **Chỉ enable MỘT trong hai unit** `weed-edge-ui` / `weed-edge` — cả hai đều mở camera + GPS. Hai unit khai báo `Conflicts` với nhau nên systemd sẽ dừng unit đang chạy khi unit kia start.

### 5b. Máy chạy nền, không màn hình

**Edge (Jetson):** `edge/deploy/weed-edge.service` đã cấu hình sẵn user `jetson`, `WorkingDirectory=/home/jetson/Documents/sugarcane_weed/edge`, và `SupplementaryGroups=video dialout`.

```bash
sudo cp edge/deploy/weed-edge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weed-edge
journalctl -u weed-edge -f            # xem log
sudo systemctl stop weed-edge         # dừng
```

**Server:** `server/deploy/weed-server.service` (mặc định `/opt/weed-server`, user `www-data`) — **sửa `User`/`WorkingDirectory`/`PATH` cho khớp máy bạn** trước khi cài.

---

## 6. Xử lý sự cố nhanh

| Hiện tượng | Nguyên nhân thường gặp | Xử lý |
|---|---|---|
| `check` báo camera `LOI ... khong doc duoc frame` | Sai node (node metadata không mở được) hoặc thiếu udev rule | Kiểm tra `ls /dev/video*`; node capture là index chẵn (0, 2); cài lại udev rule |
| `check` báo GPS `Permission denied` | Chưa có quyền đọc `/dev/ttyACM*` | `sudo usermod -aG dialout $USER` rồi đăng nhập lại, hoặc udev rule `MODE=0666` |
| `check` báo `chua co fix` | Chưa đủ 4 vệ tinh (trong nhà, ăng-ten bị che) | Đem ra cửa/ngoài trời, hướng ăng-ten lên trời, **chờ ~50 giây** (cold start) |
| `run` không thấy frame nào | `motion.mode: gps` mà GPS chưa có fix | Chờ có fix, hoặc `WEED_EDGE_MOCK_GPS=true` để test |
| Server `/health` không kết nối được | Server chưa chạy / sai `server.base_url` | Bật server; dữ liệu sẽ nằm trong `edge/data/outbox/` và tự gửi lại khi có mạng |
| Server trả `401` | `server.api_key` ≠ `WEED_SERVER_API_KEY` | Đặt lại cho khớp hai bên |
| Màn hình cảm ứng không hiện gì khi boot | GDM chưa auto-login nên chưa có màn hình X | Bật `AutomaticLoginEnable`/`AutomaticLogin` trong `/etc/gdm3/custom.conf`, reboot; hoặc `journalctl -u weed-edge-ui -f` xem có báo `Không lên được màn hình X` không |
| UI hiện nhưng đỏ cả 5 dòng kiểm tra | Chưa cắm camera/GPS hoặc sai udev symlink | `ls /dev/video* /dev/weed-*`; cắm lại camera, cài lại udev rule (`--action=add`) |
| `weed-edge-ui` start xong rồi tắt ngay | Đang có `weed-edge` (nunit khác) giữ camera, hoặc X chưa sẵn sàng | `systemctl status weed-edge-ui`; chỉ enable một trong hai unit |

---

## 7. Ghi chú phần cứng (đã đo trên máy thật)

- **Camera**: 2× UGREEN 4K, MJPG 1920×1080@30. Mỗi camera UVC chiếm **2 node** `/dev/video*` (index 0 = capture, index 1 = metadata). Hai camera dùng chung serial nên `/dev/v4l/by-id` bị trùng → dùng symlink theo **cổng USB**.
- **GPS**: u-blox 7 (GPS-only, ROM CORE 1.00). Ăng-ten patch gắn liền (`ANTSTATUS=OK`). Chỉ GPS, không GLONASS/BeiDou.
- **Mỗi lần mở cổng serial module bị reset** → mỗi lần khởi động `weed-edge` là một lần **cold start**, đo được **~49 giây** mới có fix khi ăng-ten thoáng. Trong lúc đó `motion.mode: gps` chưa capture (bình thường).
- **Không có toạ độ mặc định trong config.** Hệ thống lấy vị trí ở đâu thì dùng ở đó. Nếu mất fix quá `gps.fallback_after_s` (mặc định 60s), hệ thống dùng lại **vị trí fix thật cuối cùng của phiên** và đánh dấu `estimated` (UI tô màu cam, có thống kê riêng). Chưa từng có fix → **không ước lượng**.
