# Bot Telegram: novedades -> qBittorrent

Reemplazo del antiguo ColdNas Manager. Sin Telethon, sin lotes ni Radarr/Sonarr.

## Instalación en NAS UGREEN

1. Copiar esta carpeta al NAS (por ejemplo `/volume1/docker/telegram-novedades`).
2. Copiar `.env.example` a `.env` y rellenar `BOT_TOKEN`, `ALLOWED_USERS`, `QBIT_URL`, `QBIT_USER`, `QBIT_PASSWORD`.
3. Si qBittorrent está en otra pila de Portainer, `QBIT_URL` puede apuntar a la IP LAN del NAS y el puerto publicado de su Web UI, por ejemplo `http://192.168.100.100:8080` (ajustar al puerto real).
4. En el directorio del proyecto ejecutar `docker compose up -d --build` o desplegar con Portainer usando el Dockerfile y Compose adjuntos.
5. Enviar `/start` al bot **desde la cuenta autorizada** para registrar el chat y activar el resumen diario. Probar `/novedades`.
6. Asegurar que qBittorrent tenga creadas las categorías exactas `movies` y `tv`.

## Funcionamiento

- 07:00 Europe/Madrid: consulta `/ultimos`, publica las secciones Películas y Series y abre una selección múltiple paginada.
- `/novedades`: consulta inmediata.
- Los elementos seleccionados se consultan en su ficha. Se descarga el `.torrent` por HTTPS y se entrega a la Web API de qBittorrent con su categoría.
- La selección queda persistida en SQLite hasta que se añade correctamente o se desmarca.
- No se comunica con Radarr ni Sonarr.

## Limitación importante de esta primera versión

**El extractor HTML es una implementación inicial, no verificada en vivo contra la estructura actual de DonTorrent.** La página puede necesitar selectores específicos y su botón de descarga podría generar el enlace mediante JavaScript. En ese caso el bot informará `No se encontró enlace .torrent en el HTML` y NO adivinará la URL. Para completar la integración hay que contrastar el HTML de `/ultimos` y una ficha concreta, y adaptar `parse_latest()` y `find_torrent()` si corresponde. Si una ficha tiene varios torrents, la versión actual se detiene para evitar descargar uno equivocado.

**No se ha probado contra Telegram ni contra tu qBittorrent real.** Realizar una prueba con un torrent de distribución autorizada antes de usarlo habitualmente.

El bot no evita duplicados por hash por sí mismo: qBittorrent gestiona el rechazo de torrents ya añadidos. La respuesta de la API indica aceptación, no confirma la descarga posterior.

## Seguridad

Mantener `.env` privado. `ALLOWED_USERS` es obligatorio. No publicar la Web UI de qBittorrent en Internet; usar solo acceso LAN/Docker. El bot limita las URLs descargadas al dominio configurado, exige HTTPS y no sigue redirecciones HTTP. Usa solo torrents cuyo contenido tengas autorización para descargar.
