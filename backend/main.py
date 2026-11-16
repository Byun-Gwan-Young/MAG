from fastapi import Depends, FastAPI
from psycopg import Connection

from backend.db import get_db
from backend.routers.sessions import router
from backend.routers.projects import router as projects_router, providers_router
from backend.routers.auth import router as auth_router
from backend.routers.keys import router as keys_router
from backend.routers.live import router as live_router


app = FastAPI(title="MAG Backend (Multi-Agent Guardrail)", version="0.1.0")
app.include_router(router)
app.include_router(projects_router)
app.include_router(providers_router)   # C-19-8P : GET /api/providers
app.include_router(auth_router)
app.include_router(keys_router)   # C-19-8K : API keys and project mode
app.include_router(live_router)   # C-19-9 : the user's gateway sessions for the live dashboard


@app.get("/healthz", tags=["health"])
def health_check(conn: Connection = Depends(get_db)):
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return {"status": "ok", "database": "ok"}
