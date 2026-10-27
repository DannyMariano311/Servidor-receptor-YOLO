# Servidor receptor de pasajeros

Servidor en Python que recibe video y telemetría de un cliente mediante Socket.IO y WebRTC. Procesa los fotogramas con YOLO para detectar cruces de pasajeros, guarda eventos y capturas, y ofrece un endpoint para consultar reportes de recaudo.

## Funcionalidades

- Señalización WebRTC por Socket.IO para recibir una transmisión de video.
- Detección y seguimiento de personas con YOLOv8 y ByteTrack.
- Detección de cruces de una línea horizontal central y clasificación aproximada por altura como `Nino` o `Adulto`.
- Almacenamiento de eventos y capturas en PostgreSQL/PostGIS y en el directorio `snapshots`.
- Recepción de ubicación mediante el evento `telemetry`.
- Endpoint de salud y API de reportes por rango de fechas.
- Soporte opcional para HTTPS mediante certificado y clave TLS.

## Requisitos

- Windows 10/11.
- Python 3.10 o posterior.
- PostgreSQL con la extensión PostGIS habilitada y el esquema descrito en [Base de datos](#base-de-datos).
- Una cámara/transmisión accesible desde el cliente WebRTC.
- Para procesamiento acelerado, una GPU compatible y una instalación de PyTorch con soporte CUDA. Sin GPU, el procesamiento usa CPU.

El proyecto carga el modelo `yolov8m.pt` al iniciar. Los pesos del modelo no se incluyen en Git; proporciona ese archivo localmente en el directorio raíz desde donde se ejecuta `main.py`.

## Instalación en Windows

Abre PowerShell en el directorio del proyecto y crea un entorno virtual:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Si PowerShell impide activar el entorno virtual, puedes ejecutar los comandos usando directamente `.\.venv\Scripts\python.exe`.

### PyTorch y GPU (opcional)

La instalación de `requirements.txt` incluye Ultralytics, pero el soporte CUDA depende de la versión de PyTorch instalada y de los controladores de la máquina. Consulta [la guía oficial de instalación de PyTorch](https://pytorch.org/get-started/locally/) para elegir la combinación adecuada para tu GPU. En CPU no se requiere configuración adicional.

## Configuración

El servidor carga las variables del archivo `.env` del directorio actual. Las variables ya definidas en el entorno del sistema tienen prioridad. Configura la conexión a la base de datos y, si corresponde, TLS:

```dotenv
DATABASE_URL=postgresql://usuario:contraseña@localhost:5432/bus_tracker
PORT=8080
CORS_ALLOWED_ORIGINS=http://localhost:3000,https://tu-dominio.example
SSL_CERT=
SSL_KEY=
```

| Variable | Requerida | Descripción |
|---|---:|---|
| `DATABASE_URL` | Para guardar eventos y consultar reportes | Cadena de conexión de PostgreSQL. Si se omite, el código usa una URL local de ejemplo que debes reemplazar. |
| `PORT` | No | Puerto HTTP/HTTPS. Valor predeterminado: `8080`. |
| `CORS_ALLOWED_ORIGINS` | No | Orígenes permitidos, separados por comas. Si se omite, se permite cualquier origen (`*`); en producción, especifica los orígenes del cliente. |
| `SSL_CERT` | No | Ruta al certificado TLS. Debe definirse junto con `SSL_KEY`. |
| `SSL_KEY` | No | Ruta a la clave privada TLS. Debe definirse junto con `SSL_CERT`. |

No publiques credenciales ni claves privadas en el repositorio. Usa rutas a certificados válidos y protegidos en producción; los archivos de desarrollo no deben tratarse como certificados de producción.

## Base de datos

La aplicación no crea tablas ni instala PostGIS automáticamente. Antes de iniciar el servidor, prepara una base de datos con PostGIS y el esquema que utiliza la aplicación:

- `eventos_pasajeros`: `id`, `track_id`, `tipo_evento`, `categoria`, `foto_path`, `coordenada` y `timestamp_evento`. La columna `coordenada` debe ser compatible con las operaciones espaciales de PostGIS y usar coordenadas geográficas en SRID 4326.
- `lugares`: `id`, `nombre`, `coordenada`, `radio_metros` y `es_zona_especifica`.
- `tarifas`: `origen_id`, `destino_id` y `precio`.

Los tipos, restricciones, índices y relaciones deben ser compatibles con las consultas de la aplicación. Los lugares se identifican mediante su radio geográfico; el reporte utiliza esas coincidencias para asignar origen y destino, y consulta las tarifas correspondientes.

El servidor intenta crear un pool de conexiones al arrancar. Si la base de datos no está disponible, el servicio puede iniciar, pero no guardará eventos; el endpoint de reportes responderá con error mientras no exista el pool.

## Ejecución

Con el entorno virtual activo y la configuración lista:

```powershell
python main.py
```

El servidor escucha en `0.0.0.0` en el puerto configurado. Si no configuraste TLS, las direcciones usan `http://`; si definiste `SSL_CERT` y `SSL_KEY`, usa `https://`.

Al recibir una transmisión de video, OpenCV abre una ventana llamada `Bus Stream` en la máquina del servidor. Por ello, el procesamiento actual requiere un entorno con escritorio disponible. Los eventos detectados generan archivos JPG dentro de `snapshots`.

## API HTTP

### Estado del servicio

```http
GET /health
```

Devuelve JSON con el estado, el puerto, la ruta de Socket.IO y si TLS está habilitado.

### Reporte de recaudo

```http
GET /api/reporte?fecha_inicio=2026-10-01&fecha_fin=2026-10-27
```

Ambas fechas son obligatorias y deben tener formato `YYYY-MM-DD`. El rango incluye el día inicial y el día final. La respuesta incluye:

- `total_recaudo`: suma de las tarifas de los trayectos calculados.
- `anulaciones_realizadas`: cantidad de pares de eventos anulados en zonas específicas.
- `desglose_tramos`: cantidad de trayectos y subtotal por origen y destino.

Se requiere una conexión activa a PostgreSQL y el esquema completo descrito arriba.

## Integración Socket.IO y WebRTC

Conéctate al servidor usando el path `/socket.io/`. El cliente puede enviar estos eventos:

| Evento | Datos principales | Uso |
|---|---|---|
| `offer` | `{ "sdp": "...", "type": "offer" }` | Envía la oferta WebRTC. También se acepta dentro de `{ "offer": { ... } }`. |
| `ice-candidate` | `{ "candidate": "...", "sdpMid": "...", "sdpMLineIndex": 0 }` | Envía candidatos ICE; también puede usar la estructura de candidato del navegador. |
| `telemetry` | `{ "latitude": 6.0, "longitude": -75.0, "timestamp": 1790000000000 }` | Actualiza la ubicación usada al guardar eventos. `lng` también se acepta en lugar de `longitude`. |
| `answer` | `{ "sdp": "...", "type": "answer" }` | Se acepta como flujo de señalización alternativo si ya existe una conexión WebRTC. |

En respuesta a `offer`, el servidor emite `answer` con el SDP local y, durante la negociación, `ice-candidate` con sus candidatos ICE. El marcador de fin de candidatos se envía como `{ "candidate": null }`.

El cliente debe enviar video por la conexión WebRTC. El servidor procesa personas (clase COCO `0`), usa seguimiento persistente y registra `ENTRADA` o `SALIDA` cuando una persona cruza la línea central. La clasificación por altura de imagen es una aproximación y puede requerir calibración según la cámara.

## Estructura del proyecto

```text
.
├── main.py             # Servidor aiohttp, Socket.IO, rutas y configuración
├── webrtc_session.py   # Sesiones WebRTC, procesamiento YOLO y persistencia
├── reporte_api.py      # Consulta y cálculo de reportes
├── requirements.txt    # Dependencias de Python
├── yolov8m.pt          # Modelo YOLO utilizado por defecto
└── snapshots/          # Capturas generadas durante el procesamiento
```

## Solución de problemas

- **No se conecta a PostgreSQL:** revisa `DATABASE_URL`, que PostgreSQL esté activo y que la base de datos tenga PostGIS y las tablas requeridas. Consulta los mensajes de inicio del servidor.
- **El servicio inicia, pero no guarda eventos:** confirma que el pool de base de datos se haya creado correctamente.
- **No se puede cargar `yolov8m.pt`:** ejecuta `main.py` desde el directorio del proyecto y confirma que el archivo del modelo exista allí.
- **No aparece la ventana de video:** el proceso necesita un escritorio disponible; una ejecución sin interfaz gráfica no mostrará la ventana de OpenCV.
- **La conexión WebRTC no completa:** verifica que el cliente envíe oferta y candidatos ICE, y que la red permita el tráfico WebRTC entre cliente y servidor.
- **Error TLS al iniciar:** `SSL_CERT` y `SSL_KEY` deben definirse juntos y apuntar a archivos legibles y válidos.

## Tecnologías

Python · aiohttp · python-socketio · aiortc · OpenCV · Ultralytics YOLO · PostgreSQL · PostGIS
