from __future__ import annotations

import asyncio
import time
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from config import TG_MAX_PARALLEL
from services.storage import (
    get_temp_batch_dir,
    move_batch_to_destination,
)
from services.telethon_downloader import telethon_downloader


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    return name if name and name not in {".", ".."} else "archivo"


async def receive_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message
    if not message:
        return

    user = update.effective_user
    if not user:
        return

    if user.id not in context.bot_data["allowed_users"]:
        await message.reply_text("⛔ No estás autorizado.")
        return

    mode = context.user_data.get("mode")
    if not mode:
        await message.reply_text(
            "ℹ️ Primero selecciona un modo con /series, /peliculas o /3d."
        )
        return

    document = message.document
    if not document:
        return

    filename = _safe_filename(
        document.file_name or f"archivo_{message.message_id}"
    )

    files = context.user_data.setdefault("files", {})

    files[filename] = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "filename": filename,
        "size": document.file_size or 0,
    }

    await update_control_message(message, context)


async def update_control_message(message, context):
    files = context.user_data.get("files", {})
    mode = context.user_data.get("mode")

    title = {
        "series": "📺 SERIES",
        "movies": "🎬 PELÍCULAS",
        "3d": "🧊 3D",
    }.get(mode, "📦 LOTE")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Finalizar lote",
                callback_data="batch:finish",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancelar",
                callback_data="batch:cancel",
            )
        ],
    ])

    text = (
        f"{title}\n\n"
        f"📦 Archivos en cola: {len(files)}\n\n"
        "Los archivos están en espera.\n"
        "⚠️ Todavía NO se están descargando.\n\n"
        "Puedes seguir enviando archivos o pulsar "
        "«Finalizar lote»."
    )

    control_id = context.user_data.get("control_message_id")

    if control_id:
        try:
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=control_id,
                text=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    control = await message.reply_text(
        text,
        reply_markup=keyboard,
    )
    context.user_data["control_message_id"] = control.message_id


def _format_mb(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"


def _bar(percentage: float, length: int = 18) -> str:
    percentage = max(0.0, min(100.0, percentage))
    filled = round(length * percentage / 100)
    return "█" * filled + "░" * (length - filled)


async def _progress_loop(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    state: dict,
    stop_event: asyncio.Event,
):
    """
    Un único mensaje de Telegram para todo el lote.
    Se edita como máximo cada 2 segundos.
    """
    last_text = None

    while not stop_event.is_set():
        text = _build_status_text(state)

        if text != last_text:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                )
                last_text = text
            except Exception:
                pass

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass

    text = _build_status_text(state)

    if text != last_text:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except Exception:
            pass


def _build_status_text(state: dict) -> str:
    total = state["total"]
    completed = state["completed"]
    failed = state["failed"]
    active = state["active"]
    queued = max(0, total - completed - failed - len(active))

    lines = [
        "🚀 DESCARGANDO LOTE",
        "",
        f"📦 Progreso: {completed + failed}/{total}",
        f"🔄 Descargas activas: {len(active)}/{TG_MAX_PARALLEL}",
        f"⏳ En cola: {queued}",
        "",
    ]

    if not active:
        lines.append("⏳ Preparando descargas...")
        return "\n".join(lines)

    for item in list(active.values())[:TG_MAX_PARALLEL]:
        current = item["current"]
        total_bytes = item["total"]
        percentage = (
            current * 100 / total_bytes
            if total_bytes
            else 0
        )

        lines.extend([
            f"📄 {item['filename']}",
            f"{_bar(percentage)} {percentage:.1f}%",
            f"{_format_mb(current)} / {_format_mb(total_bytes)}",
            "",
        ])

    if len(lines) > 18:
        lines = lines[:18]

    return "\n".join(lines).rstrip()


async def finish_batch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    user = query.from_user

    if user.id not in context.bot_data["allowed_users"]:
        await query.edit_message_text("⛔ No estás autorizado.")
        return

    mode = context.user_data.get("mode")
    files = context.user_data.get("files", {})

    if not mode:
        await query.edit_message_text("ℹ️ No hay ningún lote activo.")
        return

    if not files:
        await query.edit_message_text("ℹ️ El lote está vacío.")
        return

    temp_dir = get_temp_batch_dir(user.id)
    total_files = len(files)
    destination_name = {
        "series": "Series",
        "movies": "Películas",
        "3d": "3D",
    }.get(mode, mode)

    state = {
        "total": total_files,
        "completed": 0,
        "failed": 0,
        "active": {},
    }

    # El mensaje del botón pasa a ser el mensaje único de estado.
    status_message_id = query.message.message_id

    await query.edit_message_text(
        "🚀 DESCARGANDO LOTE\n\n"
        f"📦 Progreso: 0/{total_files}\n"
        f"🔄 Descargas activas: 0/{TG_MAX_PARALLEL}\n"
        f"⏳ En cola: {total_files}"
    )

    stop_event = asyncio.Event()

    progress_task = asyncio.create_task(
        _progress_loop(
            context,
            query.message.chat_id,
            status_message_id,
            state,
            stop_event,
        )
    )

    semaphore = asyncio.Semaphore(TG_MAX_PARALLEL)
    errors: list[str] = []

    async def download_one(item: dict):
        filename = item["filename"]

        async with semaphore:
            destination = (temp_dir / filename).resolve()

            if temp_dir.resolve() not in destination.parents:
                state["failed"] += 1
                errors.append(
                    f"{filename}: nombre de archivo no válido"
                )
                return

            state["active"][filename] = {
                "filename": filename,
                "current": 0,
                "total": item.get("size", 0),
            }

            async def progress_callback(current: int, total: int):
                if filename not in state["active"]:
                    return

                state["active"][filename]["current"] = current
                state["active"][filename]["total"] = total

            try:
                await telethon_downloader.download_message(
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    destination=destination,
                    progress_callback=progress_callback,
                )

                if not destination.exists():
                    raise RuntimeError(
                        "La descarga terminó pero el archivo no existe."
                    )

                state["completed"] += 1

            except Exception as error:
                state["failed"] += 1
                errors.append(f"{filename}: {error}")

                if destination.exists():
                    try:
                        destination.unlink()
                    except Exception:
                        pass

            finally:
                state["active"].pop(filename, None)

    try:
        await asyncio.gather(
            *(download_one(item) for item in files.values())
        )
    finally:
        stop_event.set()
        await progress_task

    if errors:
        error_text = "\n".join(
            f"• {error}" for error in errors
        )

        await query.message.reply_text(
            f"⚠️ Lote descargado con errores.\n\n"
            f"✅ Descargados: {state['completed']}/{total_files}\n"
            f"❌ Errores: {len(errors)}\n\n"
            f"{error_text}\n\n"
            "Los archivos descargados permanecen en la carpeta temporal "
            "y NO se han movido al NAS."
        )
        return

    await query.edit_message_text(
        f"📦 DESCARGAS COMPLETADAS\n\n"
        f"✅ {state['completed']}/{total_files} archivos descargados.\n\n"
        f"💾 Organizando en {destination_name}..."
    )

    moved, move_errors = move_batch_to_destination(
        user.id,
        mode,
    )

    if move_errors:
        error_text = "\n".join(
            f"• {error}" for error in move_errors
        )

        await query.edit_message_text(
            f"⚠️ LOTE PROCESADO CON ERRORES\n\n"
            f"✅ Archivos guardados: {moved}\n"
            f"❌ Errores: {len(move_errors)}\n\n"
            f"{error_text}"
        )
    else:
        await query.edit_message_text(
            f"🎉 LOTE COMPLETADO\n\n"
            f"📦 {moved} archivos\n"
            f"📁 {destination_name}\n\n"
            "✅ Todo guardado correctamente."
        )
        context.user_data.clear()
