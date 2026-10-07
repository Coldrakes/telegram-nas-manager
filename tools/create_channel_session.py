import asyncio
from pathlib import Path

from telethon import TelegramClient

from config import API_HASH, API_ID, CHANNEL_SESSION_PATH


async def main():
    Path(CHANNEL_SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(CHANNEL_SESSION_PATH, API_ID, API_HASH)
    print("Autoriza la cuenta de Telegram que tiene acceso a los canales.")
    print(f"La sesión se guardará en: {CHANNEL_SESSION_PATH}.session")
    await client.start()
    me = await client.get_me()
    print(f"Sesión creada correctamente para {me.first_name} ({me.id}).")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
