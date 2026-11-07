# Telegram NAS Manager — películas + series (prueba)

Versión basada en la última compilación funcional del bot de películas.

## Comandos

- `/novedades`: películas publicadas ayer, una ficha por película, orden por calidad y título.
- `/series`: series con episodios publicados ayer, **una ficha por serie + temporada + calidad**. La ficha se obtiene de `/ultimos`, pero los episodios y sus fechas se extraen de la página de la serie, incluidos capítulos antiguos.
- `/start`: registra el chat para el resumen diario de películas.

## Series

1. El bot lee `/ultimos` y localiza las páginas `/serie/<id>/<id>/<slug>` con novedades de ayer.
2. Abre cada página una sola vez y extrae la temporada, formato, descripción, portada y filas de episodios.
3. Cada fila debe contener un botón `.protected-download` con `data-content-id` y `data-tabla`, además de número y fecha del capítulo.
4. Publica una ficha por página de serie/temporada/calidad, con botón **Seleccionar capítulos**.
5. El selector permite marcar episodios individualmente, marcar todos los pendientes, limpiar selección y descargar los marcados.
6. Cada episodio se valida por separado mediante el mecanismo PoW publicado por el sitio, y se añade a qBittorrent con categoría `tv`.
7. Los episodios añadidos se marcan con ✅ para ese chat; las selecciones y estados persisten en SQLite en `/app/data/state.sqlite3`.

La selección de episodios no descarga nada hasta pulsar **Descargar seleccionados**. Los errores individuales se notifican sin detener el resto. Si DonTorrent exige CAPTCHA o devuelve límite de uso, no se intenta eludirlo.

## Instalación

1. Configura `.env` o variables de entorno con `BOT_TOKEN`, `ALLOWED_USERS`, `QBIT_URL`, `QBIT_USER`, `QBIT_PASSWORD` y `TZ=Europe/Madrid`.
2. Construye la imagen con `docker build -t hclementearrieta/telegram-nas-manager:latest .` y publícala en tu registro si Portainer descarga la imagen desde allí.
3. Conserva el volumen `/volume1/data/telegram-nas-manager:/app/data` para no perder SQLite.
4. Recréalo en Portainer con la nueva imagen.
5. Prueba `/series`, abre un selector y descarga **un solo episodio de prueba** antes de seleccionar varios.

**Importante:** el código ha superado comprobación de sintaxis y pruebas de análisis HTML con una ficha de ejemplo, pero no se ha probado contra el HTML dinámico real de DonTorrent ni contra tu NAS. Si las filas reales utilizan una estructura diferente a `<tr>`, el extractor de episodios necesitará un ajuste. No se ha realizado una compilación de imagen Docker ni una prueba extremo a extremo.

**Seguridad:** revoca el token anterior si se expuso en logs. El bot reduce el registro de URLs de Telegram mediante los niveles de log de httpx/httpcore.
