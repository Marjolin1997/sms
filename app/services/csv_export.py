"""Eksport CSV i përbashkët, vetëm-lexim (M11-G): një vend për escaping, BOM UTF-8, kufi rreshtash dhe
shkarkim — jo kod i përsëritur për çdo ekran.

Rregulla: qelizat që fillojnë me `= + - @ TAB CR` marrin një apostrof (formula injection); numrat e
thjeshtë/telefonat (`+355…`) mbeten të pandryshuar. Kufiri është i shprehur: mbi `MAX_EXPORT_ROWS`
kthehet 422 `export_too_large` PARA se të nisë trupi (kërkon filtra më të ngushtë) — jo prerje e heshtur.
"""

import csv
import io
import re
from collections.abc import Iterator
from datetime import datetime

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

MAX_EXPORT_ROWS = 100_000
CHUNK_ROWS = 500
_PLAIN_NUMBER = re.compile(r"^\+?\d[\d.]*$")


def safe_cell(v) -> str:
    """Kundër CSV/formula injection."""
    s = "" if v is None else (v.isoformat() if isinstance(v, datetime) else str(v))
    if _PLAIN_NUMBER.match(s):
        return s
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_stream(header: list[str], rows: Iterator[list]) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    yield "\ufeff" + buf.getvalue()  # BOM: Excel e hap si UTF-8 (ë, ç)
    buf.seek(0)
    buf.truncate()
    for n, r in enumerate(rows, 1):
        w.writerow([safe_cell(c) for c in r])
        if (
            n % CHUNK_ROWS == 0
        ):  # grupe rreshtash: shumë chunk të vegjël e ngadalësojnë transmetimin
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    if buf.tell():
        yield buf.getvalue()


def download(name: str, gen: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def require_bounded(db: Session, stmt, limit: int = MAX_EXPORT_ROWS) -> None:
    """Numëron (me kufi limit+1) rreshtat e pyetjes; mbi `limit` ⇒ 422, pa nisur shkarkimin."""
    n = db.scalar(select(func.count()).select_from(stmt.order_by(None).limit(limit + 1).subquery()))
    if (n or 0) > limit:
        raise HTTPException(
            422,
            {"code": "export_too_large",
             "message": f"the export has more than {limit} rows; narrow the filters or the dates"},
        )  # fmt: skip
