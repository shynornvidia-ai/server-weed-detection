FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv (cài từ official) để sync từ uv.lock
# static/ nam TRONG src/weed_server/static -> chi can COPY src/
COPY uv.lock pyproject.toml ./
COPY src/ src/

RUN pip install --no-cache-dir uv
RUN uv sync --frozen --no-dev

EXPOSE 8000
# Chay truc tiep binary trong .venv (khong dung `uv run` -> khong re-sync dev
# deps moi lan boot, khong phu thuoc mang luc start). Render/PORT bieu dien
# qua env PORT, mac dinh 8000 khi chay local/docker.
CMD ["/bin/sh", "-c", "exec /app/.venv/bin/weed-server --host 0.0.0.0 --port ${PORT:-8000} --db ${WEED_SERVER_DB:-/data/weed_server.db}"]
