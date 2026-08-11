"""GrabbyTube - YouTube to MP3 web app."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from db import init_db
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
    start_worker()
    logger.info(f"Output directory: {settings.output_path}")
    yield
    logger.info("GrabbyTube shutting down")


app = FastAPI(title="GrabbyTube", lifespan=lifespan)
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
