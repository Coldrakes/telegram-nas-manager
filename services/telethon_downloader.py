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
    TG_DL_TIMEOUT,
    TG_SESSION_PATH,
)


ProgressCallback = Callable[[int, int], Awaitable[None]]


class TelethonDownloader:
    """Cliente Telethon usado exclusivamente para descargar archivos."""

    def __init__(self):
        self.client = TelegramClient(
            TG_SESSION_PATH,
            API_ID,
            API_HASH,
        )
        self._started = False

    async def start(self) -> None:
        if self._started:
            return

        Path(TG_SESSION_PATH).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

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

        message = await self.client.get_messages(
            chat_id,
            ids=message_id,
        )

        if not message:
            raise RuntimeError(
                "Telethon no pudo localizar el mensaje recibido."
            )

        if not message.file:
            raise RuntimeError(
                "El mensaje no contiene un archivo descargable."
            )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        callback = None

        if progress_callback is not None:
            last_report = 0.0

            def telethon_progress(current: int, total: int) -> None:
                nonlocal last_report

                now = time.monotonic()

                if (
                    total > 0
                    and current < total
                    and now - last_report < 2.0
                ):
                    return

                last_report = now

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        progress_callback(current, total)
                    )
                except RuntimeError:
                    pass

            callback = telethon_progress

        try:
            downloaded = await asyncio.wait_for(
                self.client.download_media(
                    message,
                    file=str(destination),
                    progress_callback=callback,
                ),
                timeout=TG_DL_TIMEOUT,
            )
        except asyncio.TimeoutError as error:
            raise TimeoutError(
                f"La descarga superó el tiempo máximo de "
                f"{TG_DL_TIMEOUT} segundos."
            ) from error

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


telethon_downloader = TelethonDownloader()
