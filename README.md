# Telegram NAS Manager — Novedades unificadas

## Comandos

- `/novedades`: envía primero las películas y después las series publicadas ayer.
- `/peliculas`: envía solamente las películas publicadas ayer.
- `/series`: envía solamente las series publicadas ayer; una ficha por serie, temporada y calidad, con selección individual de capítulos y sus fechas.
- `/start`: registra el chat; si es nuevo, envía un saludo y las novedades inmediatamente.

## Arranque y horario

- **En cada inicio del contenedor** se envían las novedades a los chats ya registrados, sin esperar a la hora programada.
- **El saludo de arranque** se envía una sola vez por chat, persistido en `/app/data/state.sqlite3`. Si el chat se registra por primera vez con `/start`, recibe su saludo y las novedades en ese momento.
- **A las 07:00 Europe/Madrid todos los días**, el planificador envía las novedades de películas y series a los chats registrados.
- Los comandos manuales funcionan a cualquier hora. Se mantiene el filtro de publicaciones de **ayer**, no de hoy.
- Si se reinicia el contenedor varias veces, cada arranque vuelve a enviar novedades. El saludo no se repite.

## Instalación

Mantener `BOT_TOKEN`, `ALLOWED_USERS`, `QBIT_URL`, `QBIT_USER`, `QBIT_PASSWORD`, `TZ=Europe/Madrid` y el volumen persistente `/volume1/data/telegram-nas-manager:/app/data`. Reconstruir la imagen y redeplegar en Portainer.

No se ha probado contra servicios externos reales ni se ha construido una imagen Docker; se ha comprobado la sintaxis Python y la integridad del ZIP.
