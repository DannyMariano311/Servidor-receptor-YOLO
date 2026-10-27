"""Servidor receptor: Socket.IO (8080) + señalización WebRTC + visualización de video."""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from pathlib import Path
from typing import Any

# pyrefly: ignore [missing-import]
import socketio
# pyrefly: ignore [missing-import]
from aiohttp import web
# pyrefly: ignore [missing-import]
import asyncpg

import webrtc_session
from webrtc_session import WebRTCSession, ultima_telemetria

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

def _load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))
SSL_CERT = os.getenv("SSL_CERT", "")
SSL_KEY = os.getenv("SSL_KEY", "")
CORS_ORIGINS = os.getenv("CORS_ALLOWED_ORIGINS", "*")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:tu_contraseña@localhost:5432/bus_tracker")


def _origin_allowed(origin: str | None, _environ: dict | None = None) -> bool:
    """Acepta cualquier Origin (WebView, Expo, file://, capacitor, IP LAN)."""
    if CORS_ORIGINS.strip() in ("*", ""):
        return True
    if not origin:
        return True
    allowed = {item.strip() for item in CORS_ORIGINS.split(",") if item.strip()}
    return origin in allowed


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """CORS para rutas aiohttp. Socket.IO ya aplica sus propios encabezados."""
    if request.path.startswith("/socket.io"):
        return await handler(request)

    headers = {
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": request.headers.get(
            "Access-Control-Request-Headers",
            "Content-Type, Authorization",
        ),
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }
    origin = request.headers.get("Origin")
    if origin and _origin_allowed(origin):
        headers["Access-Control-Allow-Origin"] = origin
    else:
        headers["Access-Control-Allow-Origin"] = "*"

    if request.method == "OPTIONS":
        return web.Response(status=204, headers=headers)

    response = await handler(request)
    for key, value in headers.items():
        response.headers[key] = value
    return response


sio = socketio.AsyncServer(
    async_mode="aiohttp",
    cors_allowed_origins=_origin_allowed,
    cors_credentials=True,
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
)
app = web.Application(middlewares=[cors_middleware])
sio.attach(app)


async def health(_request: web.Request) -> web.Response:
    scheme = "https" if SSL_CERT and SSL_KEY else "http"
    return web.json_response(
        {
            "status": "ok",
            "socketio_path": "/socket.io/",
            "url": f"{scheme}://<host>:{PORT}",
            "secure": bool(SSL_CERT and SSL_KEY),
        }
    )


app.router.add_get("/health", health)

import reporte_api
app.router.add_get("/api/reporte", reporte_api.get_reporte)

sessions: dict[str, WebRTCSession] = {}


async def emit_to_client(sid: str, event: str, data: dict[str, Any]) -> None:
    await sio.emit(event, data, to=sid)


def get_session(sid: str) -> WebRTCSession:
    session = sessions.get(sid)
    if session is None:
        session = WebRTCSession(sid, lambda event, data: emit_to_client(sid, event, data))
        sessions[sid] = session
    return session


@sio.event
async def connect(sid: str, environ: dict) -> None:
    origin = environ.get("HTTP_ORIGIN", "(sin Origin)")
    logger.info("Cliente conectado: %s origin=%s", sid, origin)
    get_session(sid)


@sio.event
async def disconnect(sid: str) -> None:
    logger.info("Cliente desconectado: %s", sid)
    session = sessions.pop(sid, None)
    if session is not None:
        try:
            await session.close()
        except Exception:
            logger.exception("Error limpiando sesión de %s", sid)


@sio.on("offer")
async def on_offer(sid: str, data: dict[str, Any]) -> None:
    logger.info("Offer recibido de %s", sid)
    try:
        await get_session(sid).handle_offer(data)
    except Exception:
        logger.exception("Error procesando offer de %s", sid)


@sio.on("answer")
async def on_answer(sid: str, data: dict[str, Any]) -> None:
    logger.info("Answer recibido de %s", sid)
    try:
        await get_session(sid).handle_answer(data)
    except Exception:
        logger.exception("Error procesando answer de %s", sid)


@sio.on("ice-candidate")
async def on_ice_candidate(sid: str, data: dict[str, Any]) -> None:
    try:
        await get_session(sid).handle_ice_candidate(data)
    except Exception:
        logger.exception("Error procesando ice-candidate de %s", sid)


@sio.on("telemetry")
async def on_telemetry(sid: str, data: dict[str, Any]) -> None:
    lat = data.get("latitude")
    lon = data.get("longitude") if data.get("longitude") is not None else data.get("lng")
    ts = data.get("timestamp")
    ultima_telemetria["lat"] = lat
    ultima_telemetria["lng"] = lon
    ultima_telemetria["timestamp"] = ts
    print(f"[Telemetry] sid={sid} lat={lat} lon={lon} timestamp={ts}")


async def on_shutdown(app: web.Application) -> None:
    logger.info("Cerrando sesiones activas...")
    close_tasks = [session.close() for session in sessions.values()]
    sessions.clear()
    if close_tasks:
        await asyncio.gather(*close_tasks, return_exceptions=True)
    # Cerrar pool de PostgreSQL
    if webrtc_session.db_pool is not None:
        await webrtc_session.db_pool.close()
        logger.info("Pool de PostgreSQL cerrado")


async def on_startup(app: web.Application) -> None:
    """Crea el pool de conexiones a PostgreSQL al iniciar."""
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        webrtc_session.db_pool = pool
        logger.info("Pool de PostgreSQL creado correctamente")
    except Exception:
        logger.exception("No se pudo conectar a PostgreSQL. Los eventos NO se guardarán.")


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)


def _load_ssl_context() -> ssl.SSLContext | None:
    if not SSL_CERT and not SSL_KEY:
        return None
    if not SSL_CERT or not SSL_KEY:
        raise SystemExit("SSL_CERT y SSL_KEY deben definirse juntos.")

    cert_path = Path(SSL_CERT)
    key_path = Path(SSL_KEY)
    if not cert_path.is_file() or not key_path.is_file():
        raise SystemExit(f"Certificado o clave no encontrados: {cert_path}, {key_path}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def main() -> None:
    ssl_context = _load_ssl_context()
    scheme = "https" if ssl_context else "http"
    logger.info("Iniciando servidor Socket.IO en %s://%s:%s", scheme, HOST, PORT)
    logger.info("Prueba HTTP: %s://127.0.0.1:%s/health", scheme, PORT)
    logger.info("Socket.IO path: /socket.io/")
    web.run_app(app, host=HOST, port=PORT, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
