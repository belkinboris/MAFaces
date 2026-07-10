"""Реестр — MVP платформы о сделках и компаниях. Статическая выдача через FastAPI."""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Реестр")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/{full_path:path}")
def index(full_path: str):
    # SPA: любые пути отдают index.html, роутинг на клиенте
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
