"""FastAPI 앱 진입점.

    uvicorn backend.app.main:app --reload

정적 프론트엔드도 이 앱이 서빙한다. 별도 웹서버를 두면 docker compose up 한 번으로
끝난다는 전제가 깨진다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT
from .deps import build_state
from .routers import predict, realtime, stations

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

FRONTEND_DIR = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.app_state = build_state()
    try:
        yield
    finally:
        app.state.app_state.close()


app = FastAPI(
    title="지하철 실시간 혼잡 예측",
    description="서울 지하철 1~8호선 열차별 예상 혼잡도와 착석 타이밍 추천",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(stations.router)
app.include_router(realtime.router)
app.include_router(predict.router)


@app.get("/", include_in_schema=False)
def index() -> Response:
    page = FRONTEND_DIR / "index.html"
    if not page.is_file():
        # 프론트가 아직 없어도 API 는 살아 있어야 한다. 빈 500 대신 다음 할 일을 알려준다.
        return HTMLResponse(
            "<h1>지하철 혼잡 예측</h1>"
            "<p>프론트엔드가 아직 없습니다. API 는 정상 동작합니다.</p>"
            '<p><a href="/docs">/docs</a> · <a href="/api/health">/api/health</a></p>',
            status_code=200,
        )
    return FileResponse(page)


if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
