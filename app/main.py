"""FastAPI 入口：REST 接口 + 单页操作界面。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import database, services
from .errors import ApiError
from .schemas import SliceIn, TrialIn


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = database.get_conn()
    try:
        database.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="风机告警静默间隔试算",
    description="评分切片版本（含触发告警 UTC 整秒明细）登记与七日回放静默间隔寻优",
    version="1.0.0",
    lifespan=lifespan,
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    content = {"error": {"code": exc.code, "message": exc.message}}
    extra = getattr(exc, "extra", None)
    if extra:
        content["error"]["details"] = extra
    return JSONResponse(status_code=exc.status_code, content=content)


# --------------------------------------------------------------------------- #
# 切片登记
# --------------------------------------------------------------------------- #
@app.post("/api/slices", status_code=201)
def register_slice(payload: SliceIn):
    """登记评分切片版本；可附触发告警 UTC 整秒列表，随该版一并保存。"""
    conn = database.get_conn()
    try:
        result = services.register_slice(conn, payload)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/slices")
def list_slices():
    conn = database.get_conn()
    try:
        return services.list_slices(conn)
    finally:
        conn.close()


@app.get("/api/slices/{slice_id}")
def get_slice(slice_id: int, include_alarms: bool = False):
    conn = database.get_conn()
    try:
        return services.get_slice(conn, slice_id, include_alarms=include_alarms)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 核算试算
# --------------------------------------------------------------------------- #
@app.post("/api/trials", status_code=201)
def run_trial(payload: TrialIn):
    """选择同风机切片版本做七日回放，二分求最短可行静默间隔并落库。"""
    conn = database.get_conn()
    try:
        result = services.run_trial(conn, payload)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/trials")
def list_trials():
    conn = database.get_conn()
    try:
        return services.list_trials(conn)
    finally:
        conn.close()


@app.get("/api/trials/{trial_id}")
def get_trial(trial_id: int):
    conn = database.get_conn()
    try:
        return services.get_trial(conn, trial_id)
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
