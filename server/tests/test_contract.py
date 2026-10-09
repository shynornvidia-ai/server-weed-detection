"""Hop dong giua phan 1 (edge) va phan 2 (server): 2 file schemas.py phai khop nhau.

Chi so field cua tung model — neu edge them field ma server khong co (hoac nguoc
lai) thi payload se bi nhan default hoac bi 422, va he thong chay cham chay ma
khong bao loi. File nay doc ca 2 file (khong can import ca 2 package).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EDGE = ROOT / "edge" / "src" / "weed_edge" / "schemas.py"
SERVER = ROOT / "server" / "src" / "weed_server" / "schemas.py"

MODELS = ("GpsFix", "FrameResult", "CaptureSample", "TrackPoint", "IngestBatch")


def fields_of(path: Path, model: str) -> list[str]:
    """Lay ten field (khong ke comment/di) cua mot model Pydantic trong file."""
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"^class {model}\(BaseModel\):\n(?P<body>.*?)(?=^class |\Z)",
                  src, re.S | re.M)
    assert m, f"Khong thay class {model} trong {path}"
    body = re.sub(r"^\s*#.*$", "", m.group("body"), flags=re.M)   # bo comment
    return re.findall(r"^\s{4}(\w+)\s*:", body, flags=re.M)


def test_both_schema_files_exist() -> None:
    assert EDGE.is_file(), EDGE
    assert SERVER.is_file(), SERVER


def test_ingest_contract_matches_between_edge_and_server() -> None:
    for model in MODELS:
        e, s = fields_of(EDGE, model), fields_of(SERVER, model)
        assert e == s, f"{model}: edge={e} khac server={s}"


def test_batch_id_present_on_both_sides() -> None:
    """batch_id la khoa idempotency — thieu mot ben thi retry se nhan doi du lieu."""
    assert "batch_id" in fields_of(EDGE, "IngestBatch")
    assert "batch_id" in fields_of(SERVER, "IngestBatch")
