"""
Disaster-monitoring backend (FastAPI + Supabase Postgres).

Environment variables (set in Render):
  SUPABASE_DB_URL   Postgres connection string (Supabase pooler URL)
  BACKEND_API_KEY   secret key that nodes must send in the X-API-Key header
  ALLOWED_ORIGINS   "*" or comma-separated list of dashboard URLs
"""
import hmac
import logging
import os
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO)

DB_URL = os.environ.get("SUPABASE_DB_URL", "").strip()
API_KEY = os.environ.get("BACKEND_API_KEY", "").strip()
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

app = FastAPI(title="Disaster Monitoring API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_pool: Optional[pool.SimpleConnectionPool] = None


def get_pool() -> pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        if not DB_URL:
            raise RuntimeError("SUPABASE_DB_URL is not set")
        _pool = pool.SimpleConnectionPool(1, 5, dsn=DB_URL, connect_timeout=10)
    return _pool


@contextmanager
def db():
    p = get_pool()
    conn = p.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id BIGSERIAL PRIMARY KEY,
    node_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    temperature_c REAL,
    humidity_pct REAL,
    pressure_hpa REAL,
    mq2_raw REAL,
    mq135_raw REAL,
    soil_moisture_pct REAL,
    rainfall_mm_h REAL,
    vibration_rms REAL,
    flood_score REAL,
    fire_score REAL,
    air_score REAL,
    landslide_score REAL,
    ai_score REAL,
    severity TEXT,
    driver TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_node_time ON telemetry (node_id, created_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    node_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    hazard TEXT,
    severity TEXT NOT NULL,
    score REAL,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts (created_at DESC);
"""


@app.on_event("startup")
def init_db():
    # Never crash the whole app if the DB is unreachable: /health must still answer,
    # and the real error will be visible in the Render logs.
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA)
        log.info("Database ready (tables telemetry, alerts).")
    except Exception as e:
        log.error("DATABASE INIT FAILED: %s", e)


def require_key(x_api_key: str = Header(default="")):
    if not API_KEY or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def severity_of(score: Optional[float]) -> Optional[str]:
    # Same thresholds as the ESP32 firmware
    if score is None or score < 0:
        return None
    if score >= 0.85:
        return "critical"
    if score >= 0.65:
        return "high"
    if score >= 0.40:
        return "medium"
    if score >= 0.20:
        return "low"
    return "informational"


class Reading(BaseModel):
    node_id: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    pressure_hpa: Optional[float] = None
    mq2_raw: Optional[float] = None
    mq135_raw: Optional[float] = None
    soil_moisture_pct: Optional[float] = None
    rainfall_mm_h: Optional[float] = None
    vibration_rms: Optional[float] = None
    flood_score: Optional[float] = None
    fire_score: Optional[float] = None
    air_score: Optional[float] = None
    landslide_score: Optional[float] = None
    ai_score: Optional[float] = None
    severity: Optional[str] = None
    driver: Optional[str] = None


@app.get("/")
def root():
    return {"service": "disaster-monitoring-api", "health": "/api/v1/health"}


@app.get("/api/v1/health")
def health():
    return {"ok": True}


@app.get("/api/v1/health/db")
def health_db():
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return {"ok": True, "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB error: {e}")


@app.post("/api/v1/telemetry", dependencies=[Depends(require_key)])
def post_telemetry(r: Reading):
    # Fill in ai_score / driver / severity if the node didn't send them
    if r.ai_score is None:
        candidates = {
            "flood": r.flood_score, "fire": r.fire_score,
            "air": r.air_score, "landslide": r.landslide_score,
        }
        valid = {k: v for k, v in candidates.items() if v is not None and v >= 0}
        if valid:
            r.driver = max(valid, key=valid.get)
            r.ai_score = valid[r.driver]
    if r.severity is None:
        r.severity = severity_of(r.ai_score)

    cols = [
        "node_id", "lat", "lon", "temperature_c", "humidity_pct", "pressure_hpa",
        "mq2_raw", "mq135_raw", "soil_moisture_pct", "rainfall_mm_h", "vibration_rms",
        "flood_score", "fire_score", "air_score", "landslide_score",
        "ai_score", "severity", "driver",
    ]
    vals = [getattr(r, c) for c in cols]
    alert_created = False
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO telemetry ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) RETURNING id",
                vals,
            )
            new_id = cur.fetchone()[0]

            # Raise an alert for medium+ severity, at most one per node+hazard per 5 minutes
            if r.severity in ("medium", "high", "critical"):
                cur.execute(
                    """SELECT 1 FROM alerts
                       WHERE node_id=%s AND hazard IS NOT DISTINCT FROM %s
                         AND created_at > now() - interval '5 minutes' LIMIT 1""",
                    (r.node_id, r.driver),
                )
                if cur.fetchone() is None:
                    msg = f"{(r.driver or 'unknown').title()} risk {r.severity} (score {r.ai_score:.2f}) at {r.node_id}"
                    cur.execute(
                        """INSERT INTO alerts (node_id, hazard, severity, score, lat, lon, message)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (r.node_id, r.driver, r.severity, r.ai_score, r.lat, r.lon, msg),
                    )
                    alert_created = True
    except Exception as e:
        log.error("insert failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    return {"ok": True, "id": new_id, "alert_created": alert_created}


def rows(sql: str, params=()):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@app.get("/api/v1/telemetry")
def list_telemetry(node_id: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
    try:
        if node_id:
            return rows("SELECT * FROM telemetry WHERE node_id=%s ORDER BY created_at DESC LIMIT %s", (node_id, limit))
        return rows("SELECT * FROM telemetry ORDER BY created_at DESC LIMIT %s", (limit,))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.get("/api/v1/nodes")
def latest_per_node():
    """Most recent reading from every node (for map / node cards)."""
    try:
        return rows("SELECT DISTINCT ON (node_id) * FROM telemetry ORDER BY node_id, created_at DESC")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.get("/api/v1/alerts")
def list_alerts(limit: int = Query(50, ge=1, le=500)):
    try:
        return rows("SELECT * FROM alerts ORDER BY created_at DESC LIMIT %s", (limit,))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
