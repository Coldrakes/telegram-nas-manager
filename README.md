# Telegram NAS Manager — películas de ayer

Bot para NAS UGREEN. A las 07:00 (Europe/Madrid) o con `/novedades`, muestra fichas individuales de películas publicadas **ayer** en `https://dontorrent.tips/ultimos`, con portada, géneros, formato, sinopsis y botón Descargar.

Orden por calidad: 4K, BDremux-1080p, BluRay-1080p, MicroHD-1080p, HDTV-1080p, HDRip, HDTV, DVDRip; dentro de cada calidad, alfabéticamente.

Al pulsar Descargar, reutiliza el enlace `.torrent` si se encontró durante la lectura de la ficha; si no, consulta la ficha con reintentos limitados. Valida el torrent y lo envía a qBittorrent con categoría `movies`. No integra Radarr/Sonarr. Si la ficha no expone un enlace `.torrent` inequívoco, informa del error en vez de inventarlo.

## Instalación

Configura `BOT_TOKEN`, `ALLOWED_USERS`, `QBIT_URL`, `QBIT_USER`, `QBIT_PASSWORD` (ver `.env.example`). Mantén `/app/data` persistente para SQLite. Construye la imagen y súbela a `hclementearrieta/telegram-nas-manager:latest`, luego recrea el contenedor en Portainer. No se necesita publicar puertos.

**Seguridad:** si un token de Telegram aparece en logs compartidos, revócalo en BotFather y actualiza el contenedor. La versión actual silencia los logs INFO de `httpx` que suelen incluir el token en la URL.

## Limitaciones

No se ha probado contra tu instancia real de Telegram/qBittorrent ni contra el HTML en vivo. La caché del enlace funciona cuando hay un único enlace `.torrent` explícito en el HTML de la ficha. Las páginas que generen enlaces exclusivamente con JavaScript pueden requerir adaptar el extractor. Las fichas antiguas ya enviadas no cambian hasta ejecutar de nuevo `/novedades`.
