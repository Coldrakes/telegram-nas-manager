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
TG_DL_TIMEOUT=3600

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
