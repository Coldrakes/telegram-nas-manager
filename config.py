import os

from dotenv import load_dotenv


load_dotenv()


# ============================================================
# TELEGRAM BOT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN no está configurado.")


# ============================================================
# USUARIOS AUTORIZADOS
# ============================================================

def get_allowed_users() -> set[int]:
    users = os.getenv("ALLOWED_USERS", "")

    if not users.strip():
        return set()

    try:
        return {
            int(user_id.strip())
            for user_id in users.split(",")
            if user_id.strip()
        }
    except ValueError as error:
        raise ValueError(
            "ALLOWED_USERS contiene un ID de Telegram no válido."
        ) from error


ALLOWED_USERS = get_allowed_users()


# ============================================================
# TELETHON
# ============================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

if not API_ID:
    raise ValueError("API_ID no está configurado.")

if not API_HASH:
    raise ValueError("API_HASH no está configurado.")


TG_MAX_PARALLEL = max(
    1,
    int(os.getenv("TG_MAX_PARALLEL", "3")),
)

TG_DL_TIMEOUT = max(
    1,
    int(os.getenv("TG_DL_TIMEOUT", "3600")),
)

TG_PROGRESS_DOWNLOAD = (
    os.getenv("TG_PROGRESS_DOWNLOAD", "True").lower() == "true"
)

TG_SESSION_PATH = os.getenv(
    "TG_SESSION_PATH",
    "/app/session/telegram_bot",
)


# ============================================================
# RUTAS
# ============================================================

MOVIES_PATH = os.getenv(
    "MOVIES_PATH",
    "/data/Peliculas",
)

SERIES_PATH = os.getenv(
    "SERIES_PATH",
    "/data/Series",
)

THREED_PATH = os.getenv(
    "THREED_PATH",
    "/data/3D",
)

TEMP_PATH = os.getenv(
    "TEMP_PATH",
    "/data/temp",
)
