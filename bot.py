import logging

from telegram import BotCommand, MenuButtonCommands, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import (
    ALLOWED_USERS,
    BOT_TOKEN,
)

from handlers.commands import (
    action_callback,
    cancel_batch,
    mode_callback,
    movies,
    series,
    start,
    three_d,
)

from handlers.files import (
    finish_batch,
    receive_file,
    track_user_message,
)

from services.storage import prepare_directories
from services.telethon_downloader import telethon_downloader
from services.channel_downloader import channel_downloader
from services.channel_db import channel_db
from handlers.channels import channel_start, channel_callback
from handlers.session import session_menu, session_callback, session_text


async def post_init(application: Application):
    """
    Inicializa Telethon cuando python-telegram-bot ya tiene su
    event loop preparado.
    """
    channel_db.init()
    channel_db.reset_interrupted()
    await telethon_downloader.start()
    await channel_downloader.start()

    # Menú nativo de Telegram junto a la barra de escritura.
    await application.bot.set_my_commands([
        BotCommand("start", "Abrir menú principal"),
        BotCommand("peliculas", "Nuevo lote de películas"),
        BotCommand("series", "Nuevo lote de series"),
        BotCommand("3d", "Nuevo lote 3D"),
        BotCommand("canal", "Sincronizar archivos de un canal"),
        BotCommand("sesion", "Configurar sesión de Telegram"),
    ])
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )


async def post_shutdown(application: Application):
    """
    Cierra correctamente la sesión de Telethon al apagar el bot.
    """
    await channel_downloader.stop()
    await telethon_downloader.stop()


async def error_handler(update: object, context):
    """Evita el aviso 'No error handlers are registered' y registra el fallo."""
    logging.getLogger(__name__).error(
        "Excepción no controlada procesando una actualización",
        exc_info=context.error,
    )


def main():

    print("🚀 Iniciando bot...")

    prepare_directories()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.bot_data["allowed_users"] = ALLOWED_USERS
    application.add_error_handler(error_handler)

    # Comandos
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("series", series)
    )

    application.add_handler(
        CommandHandler("peliculas", movies)
    )

    application.add_handler(
        CommandHandler("3d", three_d)
    )

    application.add_handler(
        CommandHandler("canal", channel_start)
    )

    application.add_handler(CommandHandler("sesion", session_menu))

    # Botones
    application.add_handler(
        CallbackQueryHandler(action_callback, pattern=r"^action:files$")
    )
    application.add_handler(
        CallbackQueryHandler(channel_start, pattern=r"^channel:start$")
    )
    application.add_handler(
        CallbackQueryHandler(channel_callback, pattern=r"^channel:(?:run:|cancel)")
    )

    application.add_handler(CallbackQueryHandler(session_menu, pattern=r"^session:menu$"))
    application.add_handler(CallbackQueryHandler(session_callback, pattern=r"^session:(?:login|check|logout|cancel)$"))

    application.add_handler(
        CallbackQueryHandler(
            mode_callback,
            pattern=r"^mode:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            finish_batch,
            pattern=r"^batch:finish$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_batch,
            pattern=r"^batch:cancel$"
        )
    )

    # Referencia de canal (solo cuando /canal está esperando un enlace)
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, session_text),
        group=-1,
    )

    # Archivos
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_file
        )
    )

    # Grupo separado: registra cualquier mensaje del usuario incluso si otro
    # handler (comando/documento) ya lo procesó en el grupo principal.
    application.add_handler(
        MessageHandler(filters.ALL, track_user_message),
        group=1,
    )

    print("🤖 Bot iniciado.")

    application.run_polling()


if __name__ == "__main__":
    main()
