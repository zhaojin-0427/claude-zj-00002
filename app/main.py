from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .database import Base, engine
from .exceptions import ApiException
from .response import ok
from .routers import alerts, devices, ingest, stats

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="智能电表用电异常检测与告警归档 API",
    version="1.0.0",
    description="电表数据幂等接收、用电异常检测、告警状态流转与统计分析",
)

app.include_router(ingest.router, prefix="/api/v1")
app.include_router(alerts.router, prefix="/api/v1")
app.include_router(devices.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")


@app.get("/", summary="服务信息")
def root():
    return ok({"service": "smart-meter-anomaly-api", "version": "1.0.0", "docs": "/docs"})


@app.exception_handler(ApiException)
async def api_exception_handler(request: Request, exc: ApiException):
    return JSONResponse(status_code=exc.http_status,
                        content={"code": exc.code, "message": exc.message, "data": None})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    msg = f"参数校验失败: {loc} {first.get('msg', '')}".strip()
    return JSONResponse(status_code=400,
                        content={"code": 40000, "message": msg, "data": None})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"code": exc.status_code * 100,
                                 "message": str(exc.detail), "data": None})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500,
                        content={"code": 50000, "message": "服务器内部错误", "data": None})
