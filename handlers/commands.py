from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from services.storage import clear_temp_batch


def is_authorized(user_id: int, allowed_users: set[int]) -> bool:
    return user_id in allowed_users


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_authorized(user.id, context.bot_data["allowed_users"]):
        await update.message.reply_text("⛔ No estás autorizado para utilizar este bot.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Películas", callback_data="mode:movies"),
            InlineKeyboardButton("📺 Series", callback_data="mode:series"),
        ],
        [
            InlineKeyboardButton("🧊 3D", callback_data="mode:3d"),
        ],
    ])

    await update.message.reply_text(
        "👋 Hola.\n\nSelecciona qué quieres enviar:",
        reply_markup=keyboard,
    )


async def series(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command_mode(update, context, "series")


async def movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command_mode(update, context, "movies")


async def three_d(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command_mode(update, context, "3d")


async def _command_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_authorized(user.id, context.bot_data["allowed_users"]):
        await update.message.reply_text("⛔ No estás autorizado.")
        return

    await activate_mode(update, context, mode)


async def activate_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    user = update.effective_user
    if not user or not update.message:
        return

    clear_temp_batch(user.id)
    context.user_data["mode"] = mode
    context.user_data["files"] = {}
    context.user_data["control_message_id"] = None

    titles = {
        "movies": "🎬 Modo PELÍCULAS activado",
        "series": "📺 Modo SERIES activado",
        "3d": "🧊 Modo 3D activado",
    }

    await update.message.reply_text(
        f"{titles[mode]}\n\n"
        "Envíame los archivos que quieras añadir al lote.\n\n"
        "Los archivos NO se descargarán hasta que pulses «Finalizar lote»."
    )


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user

    if not is_authorized(user.id, context.bot_data["allowed_users"]):
        await query.edit_message_text("⛔ No estás autorizado.")
        return

    mode = query.data.split(":", 1)[1]
    if mode not in {"movies", "series", "3d"}:
        await query.edit_message_text("⛔ Modo no válido.")
        return

    clear_temp_batch(user.id)
    context.user_data["mode"] = mode
    context.user_data["files"] = {}
    context.user_data["control_message_id"] = None

    titles = {
        "movies": "🎬 Modo PELÍCULAS activado",
        "series": "📺 Modo SERIES activado",
        "3d": "🧊 Modo 3D activado",
    }

    await query.edit_message_text(
        f"{titles[mode]}.\n\n"
        "Envíame los archivos que quieras añadir al lote.\n\n"
        "Los archivos NO se descargarán hasta que pulses «Finalizar lote»."
    )


async def cancel_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user

    if not is_authorized(user.id, context.bot_data["allowed_users"]):
        await query.edit_message_text("⛔ No estás autorizado.")
        return

    clear_temp_batch(user.id)
    context.user_data.clear()
    await query.edit_message_text("❌ Lote cancelado.")
