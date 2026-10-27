"""Gestión de sesiones WebRTC: señalización, ICE y consumo de video."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

# pyrefly: ignore [missing-import]
import cv2
# pyrefly: ignore [missing-import]
from ultralytics import YOLO

logger = logging.getLogger(__name__)
# Usar yolov8m.pt (Medium): Balance perfecto de altísima velocidad (100+ FPS en GPU RTX 2070 Super) y alta precisión
model = YOLO('yolov8m.pt')
# Forzar uso de GPU si está disponible
try:
    import torch
    if torch.cuda.is_available():
        model.to('cuda')
        logger.info("YOLO cargado exitosamente en GPU: %s", torch.cuda.get_device_name(0))
    else:
        logger.info("GPU no disponible para PyTorch, usando CPU")
except Exception as e:
    logger.warning("No se pudo verificar GPU: %s", e)

track_history: dict[int, tuple[float, float]] = {}
LINEA_Y_PROPORCION = 0.5  # 50% de la altura de la imagen (ajustable)
ultima_telemetria: dict[str, Any] = {"lat": None, "lng": None, "timestamp": None}

# Pool de conexiones a PostgreSQL (se inicializa en main.py)
db_pool = None  # asyncpg.Pool | None

# Directorio para snapshots de eventos
SNAPSHOTS_DIR = Path("snapshots")
SNAPSHOTS_DIR.mkdir(exist_ok=True)

# Umbral de altura en px para clasificar Adulto vs Niño
ALTURA_UMBRAL_NINO = 120

# pyrefly: ignore [missing-import]
from aiortc import (
    MediaStreamError,
    RTCIceCandidate,
    RTCPeerConnection,
    RTCSessionDescription,
)
# pyrefly: ignore [missing-import]
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


async def save_evento_pasajero(
    track_id: int,
    tipo_evento: str,
    clasificacion: str,
    foto_path: str,
) -> None:
    """Inserta un evento de cruce en PostgreSQL sin bloquear el hilo de video."""
    if db_pool is None:
        logger.warning("db_pool no inicializado, evento no guardado")
        return

    lat = ultima_telemetria.get("lat")
    lng = ultima_telemetria.get("lng")
    ts = ultima_telemetria.get("timestamp")

    # Usar timestamp de la telemetría o el actual
    if isinstance(ts, (int, float)):
        event_ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    else:
        event_ts = datetime.now(tz=timezone.utc)

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO eventos_pasajeros
                    (track_id, tipo_evento, categoria, foto_path,
                     coordenada, timestamp_evento)
                VALUES
                    ($1, $2, $3, $4,
                     ST_SetSRID(ST_MakePoint($5, $6), 4326), $7)
                """,
                track_id,
                tipo_evento,
                clasificacion,
                foto_path,
                float(lng) if lng is not None else 0.0,
                float(lat) if lat is not None else 0.0,
                event_ts,
            )
        logger.info(
            "Evento guardado en PostgreSQL: %s track_id=%s cls=%s",
            tipo_evento, track_id, clasificacion,
        )
    except Exception:
        logger.exception("Error guardando evento en PostgreSQL")


def parse_ice_candidate(data: dict[str, Any]) -> RTCIceCandidate | None:
    """Convierte un candidato ICE del cliente (formato navegador) a aiortc."""
    candidate_info = data.get("candidate")
    
    if isinstance(candidate_info, dict):
        candidate_dict = candidate_info
    else:
        candidate_dict = data
        
    candidate_str = candidate_dict.get("candidate")
    if not candidate_str:
        return None

    candidate_str = str(candidate_str).strip(" '\"")
    if candidate_str.startswith("candidate:"):
        candidate_str = candidate_str.split(":", 1)[1]

    candidate = candidate_from_sdp(candidate_str)
    candidate.sdpMid = candidate_dict.get("sdpMid")
    candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
    return candidate


class WebRTCSession:
    """Maneja una RTCPeerConnection y el procesamiento de video asociado."""

    WINDOW_NAME = "Bus Stream"

    def __init__(self, sid: str, emit: EmitCallback) -> None:
        self.sid = sid
        self._emit = emit
        self.pc: RTCPeerConnection | None = None
        self._video_task: asyncio.Task[None] | None = None
        self._closed = False

    async def handle_offer(self, data: dict[str, Any]) -> None:
        """Recibe SDP offer, crea answer y la envía al cliente."""
        await self.close()

        pc = self._create_peer_connection()
        self.pc = pc

        sdp_data = data.get("offer", data)
        if not isinstance(sdp_data, dict) or "sdp" not in sdp_data:
            sdp_data = data

        offer = RTCSessionDescription(sdp=sdp_data["sdp"], type=sdp_data["type"])
        await pc.setRemoteDescription(offer)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        await self._emit(
            "answer",
            {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
            },
        )

        asyncio.create_task(self._emit_local_ice_candidates())

    async def handle_answer(self, data: dict[str, Any]) -> None:
        """Aplica un SDP answer remoto (flujo alternativo de señalización)."""
        if self.pc is None:
            logger.warning("[%s] Answer recibido sin PeerConnection activa", self.sid)
            return

        sdp_data = data.get("answer", data)
        if not isinstance(sdp_data, dict) or "sdp" not in sdp_data:
            sdp_data = data

        answer = RTCSessionDescription(sdp=sdp_data["sdp"], type=sdp_data["type"])
        await self.pc.setRemoteDescription(answer)

    async def handle_ice_candidate(self, data: dict[str, Any]) -> None:
        """Agrega un candidato ICE remoto a la PeerConnection."""
        if self.pc is None:
            logger.warning("[%s] ICE recibido sin PeerConnection activa", self.sid)
            return

        candidate = parse_ice_candidate(data)
        try:
            await self.pc.addIceCandidate(candidate)
        except Exception:
            logger.exception("[%s] Error al agregar candidato ICE", self.sid)

    def _create_peer_connection(self) -> RTCPeerConnection:
        pc = RTCPeerConnection()

        @pc.on("track")
        def on_track(track) -> None:
            logger.info("[%s] Track recibido: %s", self.sid, track.kind)
            if track.kind == "video":
                self._video_task = asyncio.create_task(self._consume_video(track))

        @pc.on("connectionstatechange")
        async def on_connection_state_change() -> None:
            logger.info("[%s] connectionState: %s", self.sid, pc.connectionState)
        return pc

    async def _consume_video(self, track) -> None:
        """Decodifica frames WebRTC y procesa con YOLO descartando buffer para tiempo real 100% fluido."""
        cv2.namedWindow(self.WINDOW_NAME, cv2.WINDOW_NORMAL)

        loop = asyncio.get_running_loop()
        latest_frame = None
        new_frame_evt = asyncio.Event()

        # Tarea 1: Drenar el buffer WebRTC a la máxima velocidad posible
        async def _drain_stream():
            nonlocal latest_frame
            try:
                while True:
                    frame = await track.recv()
                    latest_frame = frame.to_ndarray(format="bgr24")
                    new_frame_evt.set()
            except (MediaStreamError, asyncio.CancelledError):
                pass

        drain_task = asyncio.create_task(_drain_stream())

        def _process_yolo(img_bgr):
            alto_img = img_bgr.shape[0]
            linea_y_actual = int(alto_img * LINEA_Y_PROPORCION)
            
            results = model.track(
                img_bgr,
                persist=True,
                classes=[0],
                conf=0.20,
                iou=0.45,
                imgsz=640,
                tracker="bytetrack.yaml",
                verbose=False,
            )

            if len(results) > 0 and results[0].boxes is not None and results[0].boxes.id is not None:
                boxes = results[0].boxes.xywh.cpu()
                track_ids = results[0].boxes.id.int().cpu().tolist()

                for box, track_id in zip(boxes, track_ids):
                    x, y, w, h = box
                    cx, cy = float(x), float(y)
                    h_val = float(h)

                    clasificacion = "Nino" if h_val < ALTURA_UMBRAL_NINO else "Adulto"

                    if track_id in track_history:
                        prev_y = track_history[track_id][1]

                        tipo_evento = None
                        if prev_y < linea_y_actual and cy >= linea_y_actual:
                            tipo_evento = "SALIDA"
                        elif prev_y > linea_y_actual and cy <= linea_y_actual:
                            tipo_evento = "ENTRADA"

                        if tipo_evento:
                            print(f"Pasajero [{track_id}] {tipo_evento} ({clasificacion}) | Telemetría: {ultima_telemetria}")

                            # Guardar snapshot
                            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                            foto_name = f"{tipo_evento}_{track_id}_{ts_str}.jpg"
                            foto_path = str(SNAPSHOTS_DIR / foto_name)
                            cv2.imwrite(foto_path, img_bgr)

                            asyncio.run_coroutine_threadsafe(
                                save_evento_pasajero(
                                    track_id, tipo_evento,
                                    clasificacion, foto_path,
                                ),
                                loop,
                            )

                    track_history[track_id] = (cx, cy)

                display = results[0].plot()
            else:
                display = img_bgr.copy()

            return display

        try:
            while True:
                await new_frame_evt.wait()
                new_frame_evt.clear()

                if latest_frame is None:
                    continue

                img_to_process = latest_frame

                # Procesar en GPU en hilo secundario
                display = await asyncio.to_thread(_process_yolo, img_to_process)

                alto_img = display.shape[0]
                linea_y_actual = int(alto_img * LINEA_Y_PROPORCION)
                
                cv2.line(display, (0, linea_y_actual), (display.shape[1], linea_y_actual), (0, 0, 255), 2)
                cv2.imshow(self.WINDOW_NAME, display)
                cv2.waitKey(1)
        except MediaStreamError:
            logger.info("[%s] Stream de video finalizado", self.sid)
        except asyncio.CancelledError:
            logger.debug("[%s] Consumo de video cancelado", self.sid)
            raise
        except Exception:
            logger.exception("[%s] Error procesando fotogramas", self.sid)
        finally:
            drain_task.cancel()
            try:
                cv2.destroyWindow(self.WINDOW_NAME)
            except cv2.error:
                pass

    async def _emit_local_ice_candidates(self) -> None:
        """Envía candidatos ICE locales al cliente vía Socket.IO."""
        pc = self.pc
        if pc is None:
            return

        try:
            emitted = set()
            while True:
                if pc.iceGatheringState == "complete":
                    break

                for transceiver in pc.getTransceivers():
                    if not transceiver.receiver or not transceiver.receiver.transport:
                        continue
                    ice_transport = transceiver.receiver.transport.transport
                    if not ice_transport:
                        continue
                        
                    gatherer = ice_transport.iceGatherer
                    sdp_mid = transceiver.mid
                    sdp_mline_index = transceiver._get_mline_index()

                    for candidate in gatherer.getLocalCandidates():
                        candidate_str = f"candidate:{candidate_to_sdp(candidate)}"
                        if candidate_str not in emitted:
                            emitted.add(candidate_str)
                            candidate.sdpMid = sdp_mid
                            candidate.sdpMLineIndex = sdp_mline_index
                            await self._emit(
                                "ice-candidate",
                                {
                                    "candidate": candidate_str,
                                    "sdpMid": sdp_mid,
                                    "sdpMLineIndex": sdp_mline_index,
                                },
                            )
                await asyncio.sleep(0.1)

            await self._emit("ice-candidate", {"candidate": None})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] Error emitiendo candidatos ICE locales", self.sid)

    async def close(self) -> None:
        """Libera recursos de WebRTC y cancela tareas de video."""
        if self._closed:
            return
        self._closed = True

        if self._video_task and not self._video_task.done():
            self._video_task.cancel()
            try:
                await self._video_task
            except asyncio.CancelledError:
                pass

        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:
                logger.exception("[%s] Error cerrando PeerConnection", self.sid)
            finally:
                self.pc = None
