# Telegram NAS Manager

Bot de Telegram para enviar archivos a un NAS y organizarlos automáticamente.

## Funciones

- 🎬 Películas
- 📺 Series
- 🧊 Modelos 3D
- 📦 Lotes de archivos
- ⏳ Los archivos no se descargan hasta pulsar **Finalizar lote**
- 🚀 Descargas paralelas mediante Telethon
- 🔢 `TG_MAX_PARALLEL` controla el número máximo de descargas simultáneas
- 📊 Un único mensaje de progreso para todo el lote
- 📁 Organización automática de Series y 3D
- 💾 Sesión de Telethon persistente
- 🐳 Docker / Docker Compose
- 🔐 Credenciales y sesión excluidas de Git

## Organización

### Películas

Los archivos se guardan directamente:

```text
Peliculas/
├── Avatar.mkv
└── Interstellar.mkv
```

### Series

Se intenta detectar el nombre de la serie a partir de formatos como:

- `S01E01`
- `T01E01`
- `T01, E01`
- `1x01`
- `Temporada 1`
- `Season 1`

Ejemplo:

```text
Series/
└── Furious/
    ├── Furious - T01, E01 (2026).mkv
    └── Furious - T01, E02 (2026).mkv
```

### 3D

Agrupa modelos y variantes en una misma carpeta:

```text
3D/
├── RifleLadsB/
│   ├── RifleLadsB-Unsupported.zip
│   ├── RifleLadsB-Presupported.zip
│   └── RifleLadsB-Lychee.zip
│
└── Gear Guts - Mek Baby/
    ├── Gear Guts - Mek Baby O.rar
    ├── Gear Guts - Mek Baby N.rar
    └── ...
```

## Requisitos

- Docker
- Docker Compose
- Un bot de Telegram
- `API_ID` y `API_HASH` de Telegram
- Acceso de escritura del contenedor a las carpetas del NAS

## Configuración

Copia:

```bash
cp .env.example .env
```

Edita `.env`:

```env
BOT_TOKEN=...
ALLOWED_USERS=123456789

API_ID=...
API_HASH=...

TG_MAX_PARALLEL=3
TG_DL_STALL_TIMEOUT=600
TG_DL_RETRIES=2

MOVIES_HOST_PATH=/ruta/nas/Peliculas
SERIES_HOST_PATH=/ruta/nas/Series
THREED_HOST_PATH=/ruta/nas/3D
TEMP_HOST_PATH=./data/temp
SESSION_HOST_PATH=./data/session
```

### Rutas del NAS

Las variables `*_HOST_PATH` son rutas del sistema donde se ejecuta Docker.

Dentro del contenedor siempre serán:

```text
/data/Peliculas
/data/Series
/data/3D
/data/temp
/app/session
```

Esto permite mover el proyecto entre servidores sin modificar el código Python.

## Arranque

Construir e iniciar:

```bash
docker compose up -d --build
```

Ver logs:

```bash
docker compose logs -f
```

Parar:

```bash
docker compose down
```

Reiniciar:

```bash
docker compose restart
```

## Telethon y sesión

La primera vez, Telethon crea una sesión persistente en:

```text
./data/session/
```

El directorio está montado como `/app/session` dentro del contenedor.

**No borres ese directorio si quieres conservar la sesión de Telethon.**

La sesión está excluida de Git mediante `.gitignore`.

## Descargas paralelas

Por ejemplo:

```env
TG_MAX_PARALLEL=3
```

significa que como máximo habrá tres archivos descargándose simultáneamente.

El bot muestra las descargas activas en un único mensaje de Telegram y lo actualiza periódicamente.

## GitHub

No subas nunca:

- `.env`
- `*.session`
- `data/`
- `temp/`

El repositorio incluye `.gitignore` y `.env.example` para evitarlo.

## Estructura

```text
telegram-nas-manager/
├── bot.py
├── config.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── .env.example
├── README.md
├── handlers/
│   ├── __init__.py
│   ├── commands.py
│   └── files.py
└── services/
    ├── __init__.py
    ├── storage.py
    ├── telethon_downloader.py
    ├── series_organizer.py
    └── three_d_organizer.py
```


## Interfaz y progreso

- El progreso visible se actualiza por tiempo, cada `TG_PROGRESS_INTERVAL` segundos (30 por defecto).
- Cada actualización crea primero un mensaje nuevo y después elimina el anterior, para mantener el estado al final del chat.
- Los eventos importantes (inicio, organización y final) se publican inmediatamente.
- `RetryAfter`, `TimedOut` y `NetworkError` de la Bot API no detienen las descargas de Telethon.
- El bot configura el botón nativo **Menú** de Telegram con `/start`, `/peliculas`, `/series` y `/3d`.

## Sincronización de canales y descompresión automática

El bot incluye `/canal` y el botón **📡 Sincronizar canal**. Esta función recorre los archivos nuevos de un canal y recuerda el último mensaje sincronizado en `/app/session/channel_sync.json`, por lo que las siguientes ejecuciones solo procesan contenido posterior.

### Autorizar la cuenta que puede leer los canales

Las descargas normales siguen usando la sesión del bot. El historial completo de canales usa una segunda sesión Telethon de **usuario**. La cuenta debe pertenecer al canal (también funciona con canales privados a los que tenga acceso).

Con el contenedor creado, ejecuta una sola vez:

```bash
docker exec -it telegram-nas-manager python tools/create_channel_session.py
```

Telegram solicitará el teléfono, código de inicio de sesión y, si procede, la contraseña 2FA. La sesión queda persistida en el volumen `SESSION_HOST_PATH`. Reinicia después el contenedor.

### Descompresión

`AUTO_EXTRACT=True` activa la extracción automática al terminar completamente un lote o una sincronización. `DELETE_ARCHIVES_AFTER_EXTRACT=True` elimina los comprimidos **solo después** de que `7z` haya superado la prueba de integridad y la extracción haya terminado correctamente.

Se contemplan RAR, ZIP y 7z simples y multipartes, incluyendo `part01.rar`, `.rar + .r00/.r01`, `.z01/.z02 + .zip`, `.7z.001/.002` y `.zip.001/.002`. Si falta una parte, hay corrupción o falla la extracción, los comprimidos se conservan y el lote no se mueve al NAS.

## Canales grandes: cola persistente SQLite
La sincronización de canales indexa los mensajes directamente en `/app/data/coldnas.db` y procesa una cola limitada por bloques. El estado sobrevive a reinicios si `DATA_HOST_PATH` está montado. SQLite usa WAL; no requiere servidor, puerto ni credenciales. Los estados por mensaje son `pending`, `downloading`, `downloaded`, `completed` y `error`. Una descarga se escribe primero como `.part` y solo se renombra al terminar. Antes de cada bloque se comprueba espacio libre de TEMP y NAS.

Variables recomendadas: `CHANNEL_BATCH_SIZE=20`, `TG_MAX_PARALLEL=3`, `CHANNEL_MAX_RETRIES=5`, `CHANNEL_MIN_TEMP_FREE_GB=20`, `CHANNEL_MIN_NAS_FREE_GB=50`.

## Canales fijos y vinculación por chat (versión experimental)

Los canales autorizados se configuran exclusivamente en `services/allowed_channels.py`.
El canal inicial **Cajon Peliculas HD** se sincroniza en `MOVIES_PATH`.
No se aceptan enlaces arbitrarios desde el chat.

1. Asegura `API_ID`, `API_HASH`, `BOT_TOKEN` y `ALLOWED_USERS` en `.env`.
2. Inicia el bot y abre `/sesion` en un chat privado desde un usuario autorizado.
3. Pulsa **Vincular cuenta** e introduce teléfono, código y, si procede, contraseña 2FA.
4. Pulsa **Comprobar acceso**: la cuenta debe ser miembro del canal privado.
5. Abre `/canal` y selecciona **Cajon Peliculas HD**.

**Seguridad:** este flujo es experimental. Los códigos y contraseñas atraviesan el chat de Telegram; se intenta borrar cada mensaje sensible, pero no se garantiza su eliminación de todos los dispositivos o registros. Usa una cuenta de pruebas y migra después a un flujo local seguro. El fichero `.session` concede acceso a la cuenta: protege el volumen persistente y no lo publiques en Git.

## Vincular cuenta mediante QR (experimental)

Desde el chat privado autorizado, ejecuta `/sesion` y pulsa **Vincular mediante QR**.
En la app de Telegram del móvil entra en **Ajustes → Dispositivos → Vincular dispositivo**
y escanea la imagen. El QR se renueva al caducar y se elimina al finalizar.
Si Telegram solicita contraseña de verificación en dos pasos, el bot la recibe temporalmente
por chat y trata de borrar el mensaje: no es un canal adecuado para credenciales en producción.
No envíes códigos de inicio de sesión al bot. La sesión persistente se guarda según `CHANNEL_SESSION_PATH`.
