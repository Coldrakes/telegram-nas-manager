from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient

from config import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
    TG_DL_RETRIES,
    TG_DL_STALL_TIMEOUT,
    TG_SESSION_PATH,
)

ProgressCallback = Callable[[int, int], Awaitable[None]]


class TelethonDownloader:
    """Cliente Telethon usado exclusivamente para descargar archivos."""

    def __init__(self):
        self.client = TelegramClient(TG_SESSION_PATH, API_ID, API_HASH)
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        Path(TG_SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)
        await self.client.start(bot_token=BOT_TOKEN)
        self._started = True
        me = await self.client.get_me()
        username = f"@{me.username}" if me.username else "(sin username)"
        print("🔌 Telethon integrado.")
        print(f"🤖 Bot: {me.first_name}")
        print(f"🆔 ID: {me.id}")
        print(f"👤 Username: {username}")

    async def stop(self) -> None:
        if self._started:
            await self.client.disconnect()
            self._started = False

    async def download_message(
        self,
        chat_id: int,
        message_id: int,
        destination: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        if not self._started:
            raise RuntimeError("Telethon no está iniciado.")

        message = await self.client.get_messages(chat_id, ids=message_id)
        if not message:
            raise RuntimeError("Telethon no pudo localizar el mensaje recibido.")
        if not message.file:
            raise RuntimeError("El mensaje no contiene un archivo descargable.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None

        for attempt in range(TG_DL_RETRIES + 1):
            last_progress_at = time.monotonic()
            last_bytes = 0
            last_report = 0.0

            def telethon_progress(current: int, total: int) -> None:
                nonlocal last_progress_at, last_bytes, last_report
                now = time.monotonic()
                if current > last_bytes:
                    last_bytes = current
                    last_progress_at = now
                if progress_callback is None:
                    return
                if total > 0 and current < total and now - last_report < 2.0:
                    return
                last_report = now
                try:
                    asyncio.get_running_loop().create_task(
                        progress_callback(current, total)
                    )
                except RuntimeError:
                    pass

            download_task = asyncio.create_task(
                self.client.download_media(
                    message,
                    file=str(destination),
                    progress_callback=telethon_progress,
                )
            )

            try:
                while not download_task.done():
                    await asyncio.sleep(min(5.0, max(1.0, TG_DL_STALL_TIMEOUT / 10)))
                    if time.monotonic() - last_progress_at > TG_DL_STALL_TIMEOUT:
                        download_task.cancel()
                        try:
                            await download_task
                        except asyncio.CancelledError:
                            pass
                        raise TimeoutError(
                            f"La descarga no mostró progreso durante "
                            f"{TG_DL_STALL_TIMEOUT} segundos."
                        )

                downloaded = await download_task
                if not downloaded:
                    raise RuntimeError(
                        "Telethon no devolvió una ruta de archivo descargado."
                    )

                result = Path(downloaded)
                if not result.exists():
                    raise RuntimeError(
                        "La descarga terminó pero el archivo no existe en disco."
                    )
                return result

            except asyncio.CancelledError:
                if not download_task.done():
                    download_task.cancel()
                raise
            except Exception as error:
                last_error = error
                if not download_task.done():
                    download_task.cancel()
                    try:
                        await download_task
                    except (asyncio.CancelledError, Exception):
                        pass

                if attempt >= TG_DL_RETRIES:
                    break

                print(
                    f"⚠️ Descarga fallida/bloqueada: {destination.name}. "
                    f"Reintento {attempt + 1}/{TG_DL_RETRIES}: {error}"
                )
                # download_media no garantiza reanudación sobre un parcial.
                # Se elimina antes de reintentar para evitar archivos corruptos.
                if destination.exists():
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                await asyncio.sleep(min(5, attempt + 1))

        raise RuntimeError(
            f"La descarga falló tras {TG_DL_RETRIES + 1} intento(s): {last_error}"
        ) from last_error


telethon_downloader = TelethonDownloader()
