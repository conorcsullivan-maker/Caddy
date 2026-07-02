"""
Caddy Web Backend — FastAPI + SQLite
Beta-gated authentication with admin approval flow.

Entry point only: app construction, CORS, startup. The actual behavior
lives in focused modules —
    db.py        schema, connections, bootstrap
    security.py  PIN hashing, tokens, cookie/session policy
    store.py     SQLite-backed state (users, conversations, shot stats, geometry)
    deps.py      auth dependencies
    pipeline.py  the per-message chat pipeline
    routers/     auth, profile, chat, admin endpoints
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (../../.env) for local dev — MUST happen
# before any import that reads env at import time (caddy_engine requires
# ANTHROPIC_API_KEY/OPENAI_API_KEY, security.py reads COOKIE_SECURE).
# override=True because Conor's shell exports an empty ANTHROPIC_API_KEY
# (set by Claude Desktop) that would otherwise mask the .env value.
# In production, env vars are injected by the host (Render), so missing .env is fine.
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=True)

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from db import DB_PATH, init_db, purge_expired_sessions, seed_initial_admin  # noqa: E402
from routers import admin, auth, chat, profile  # noqa: E402

app = FastAPI(title="Caddy API", version="0.2.0")

# Production: set FRONTEND_ORIGIN env var to the deployed Vercel URL.
# Dev: regex allows localhost + any local network IP.
PROD_FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "").strip()
ALLOWED_ORIGINS = [PROD_FRONTEND_ORIGIN] if PROD_FRONTEND_ORIGIN else []
LOCAL_ORIGIN_REGEX = r"http://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+):\d+"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=LOCAL_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(chat.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "caddy-api"}


@app.on_event("startup")
def startup():
    init_db()
    seed_initial_admin()
    purge_expired_sessions()
    print(f"Database ready at {DB_PATH}")


if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=8000)
