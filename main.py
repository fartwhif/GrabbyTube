"""GrabbyTube - YouTube to MP3 web app."""
import logging
import os
from contextlib import asynccontextmanager
from hmac import compare_digest

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from db import init_db, get_config
from api.routes import router, start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("grabbytube")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GrabbyTube starting up...")
    init_db()
    persisted_ua = get_config("user_agent")
    if persisted_ua and "GRABBY_USER_AGENT" not in os.environ:
        settings.user_agent = persisted_ua
        logger.info("User agent restored from database")
    start_worker()
    logger.info(f"Output directory: {settings.output_path}")
    yield
    logger.info("GrabbyTube shutting down")


app = FastAPI(title="GrabbyTube", lifespan=lifespan)

# Destructive endpoints require a shared secret (X-Grabby-Token header) when
# GRABBY_API_TOKEN is set. Protects the public API (grabbytube.example.com) from
# arbitrary track deletion; DrivePod sends the same token from its .env.
PROTECTED_DELETE_PATHS = (
    "/tracks/",          # DELETE /tracks/{video_id}, POST /tracks/batch-delete
    "/jobs",             # DELETE /jobs, /jobs/all
)


@app.middleware("http")
async def verify_delete_token(request: Request, call_next):
    path = request.url.path
    is_destructive = (
        request.method == "DELETE" and path.startswith(PROTECTED_DELETE_PATHS)
    ) or (request.method == "POST" and path == "/tracks/batch-delete")
    if is_destructive and settings.api_token and not compare_digest(request.headers.get("x-grabby-token", ""), settings.api_token):
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing token"})
    return await call_next(request)


app.include_router(router)
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
