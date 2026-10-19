# ColdNas Manager

Bot de Telegram para descargar lotes manuales al NAS (Películas, Series y 3D).

## Despliegue

1. Copia `.env.example` a `.env` y configura `BOT_TOKEN`, `API_ID`, `API_HASH`, `ALLOWED_USERS` y las rutas `*_HOST_PATH`.
2. Mantén persistentes los volúmenes `/app/data`, `/app/session`, `/data/temp` y los destinos del NAS.
3. Ejecuta `docker compose up -d --build`.
4. Abre `/start`, selecciona «Nuevo lote», envía archivos y cierra el lote para descargar.

## Configuración

- `TG_MAX_PARALLEL=3`: descargas simultáneas.
- `TG_PROGRESS_INTERVAL=30`: refresco entre 30 y 60 segundos (usar 60 para menos peticiones).
- `BATCH_DB_PATH=/app/data/batches.db`: persistencia SQLite.
- Los archivos comprimidos reconocidos se extraen tras terminar todas las descargas, se prueban con 7z y se eliminan al extraerse correctamente.
- Los destinos se sobrescriben cuando existe un archivo con el mismo nombre.

## Recuperación

Los lotes cerrados pendientes se recuperan al iniciar. Los archivos ya completos y presentes en el directorio temporal no se descargan de nuevo. Una transferencia interrumpida se reinicia desde cero para ese archivo; Telethon `download_media` no garantiza reanudación por bytes.

## Avisos

Esta versión se ha comprobado mediante compilación de Python, pero **no se ha probado en Telegram ni en el NAS**. Antes de producción, prueba con un lote pequeño y conserva copia de seguridad de los archivos que puedan sobrescribirse. La pausa afecta a nuevos archivos, no a transferencias ya iniciadas. La recuperación durante extracción y movimiento no es totalmente transaccional.

Consulta `CHANGES.md` para detalles.
