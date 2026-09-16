from __future__ import annotations

import asyncio
import time
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, NetworkError, TimedOut
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


async def _send_control_message(context, chat_id: int, text: str, reply_markup=None):
    """Crea un mensaje de control. Un timeout no se considera prueba de fallo."""
    try:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
    except (TimedOut, NetworkError) as error:
        print(f"⚠️ Telegram no respondió al crear el mensaje de control: {error}")
        return None


async def _edit_or_recover_message(
    context,
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup=None,
) -> int | None:
    """
    Intenta editar el mensaje de control.

    - TimedOut/NetworkError: no crea otro mensaje porque Telegram podría haber
      procesado la edición aunque no recibamos la respuesta.
    - BadRequest indicando que el mensaje ya no puede editarse/no existe:
      crea un mensaje nuevo y devuelve su ID.
    """
    if message_id is None:
        created = await _send_control_message(
            context, chat_id, text, reply_markup
        )
        return created.message_id if created else None

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
        return message_id

    except BadRequest as error:
        description = str(error).lower()

        # No es un fallo real: Telegram devuelve esto si el contenido ya coincide.
        if "message is not modified" in description:
            return message_id

        # Para otros BadRequest el mensaje de control puede haber sido borrado,
        # ser inaccesible o haber dejado de ser editable. Creamos uno nuevo.
        print(
            f"⚠️ No se pudo editar el mensaje {message_id}: {error}. "
            "Creando uno nuevo."
        )
        created = await _send_control_message(
            context, chat_id, text, reply_markup
        )
        return created.message_id if created else message_id

    except (TimedOut, NetworkError) as error:
        # MUY IMPORTANTE: no crear otro mensaje aquí. Con un timeout no sabemos
        # si Telegram llegó a procesar la petición y podríamos duplicar mensajes.
        print(
            f"⚠️ Error temporal actualizando mensaje {message_id}: {error}. "
            "Se reintentará en la siguiente actualización."
        )
        return message_id


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
    new_id = await _edit_or_recover_message(
        context=context,
        chat_id=message.chat_id,
        message_id=control_id,
        text=text,
        reply_markup=keyboard,
    )

    if new_id is not None:
        context.user_data["control_message_id"] = new_id


def _format_mb(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"


def _bar(percentage: float, length: int = 18) -> str:
    percentage = max(0.0, min(100.0, percentage))
    filled = round(length * percentage / 100)
    return "█" * filled + "░" * (length - filled)


async def _progress_loop(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    state: dict,
    stop_event: asyncio.Event,
):
    """
    Mantiene un único mensaje para el progreso del lote.
    Los errores temporales de Telegram nunca detienen las descargas.
    Si Telegram confirma que el mensaje ya no es editable, se crea otro.
    """
    last_text = None

    while not stop_event.is_set():
        text = _build_status_text(state)

        if text != last_text:
            old_id = state.get("status_message_id")
            new_id = await _edit_or_recover_message(
                context=context,
                chat_id=chat_id,
                message_id=old_id,
                text=text,
            )
            if new_id is not None:
                state["status_message_id"] = new_id

            # Solo damos la actualización por mostrada si conservamos un ID.
            # En un timeout se reintentará con el siguiente cambio de progreso.
            if new_id is not None:
                last_text = text

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    text = _build_status_text(state)
    if text != last_text:
        new_id = await _edit_or_recover_message(
            context=context,
            chat_id=chat_id,
            message_id=state.get("status_message_id"),
            text=text,
        )
        if new_id is not None:
            state["status_message_id"] = new_id


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

    try:
        await query.answer()
    except (TimedOut, NetworkError) as error:
        print(f"⚠️ Timeout respondiendo al botón: {error}")

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
        "status_message_id": query.message.message_id,
    }

    initial_text = (
        "🚀 DESCARGANDO LOTE\n\n"
        f"📦 Progreso: 0/{total_files}\n"
        f"🔄 Descargas activas: 0/{TG_MAX_PARALLEL}\n"
        f"⏳ En cola: {total_files}"
    )
    recovered_id = await _edit_or_recover_message(
        context=context,
        chat_id=query.message.chat_id,
        message_id=state["status_message_id"],
        text=initial_text,
    )
    if recovered_id is not None:
        state["status_message_id"] = recovered_id

    stop_event = asyncio.Event()

    progress_task = asyncio.create_task(
        _progress_loop(
            context,
            query.message.chat_id,
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

        final_error_text = (
            f"⚠️ Lote descargado con errores.\n\n"
            f"✅ Descargados: {state['completed']}/{total_files}\n"
            f"❌ Errores: {len(errors)}\n\n"
            f"{error_text}\n\n"
            "Los archivos descargados permanecen en la carpeta temporal "
            "y NO se han movido al NAS."
        )
        await _edit_or_recover_message(
            context, query.message.chat_id, state.get("status_message_id"),
            final_error_text,
        )
        return

    organizing_text = (
        f"📦 DESCARGAS COMPLETADAS\n\n"
        f"✅ {state['completed']}/{total_files} archivos descargados.\n\n"
        f"💾 Organizando en {destination_name}..."
    )
    recovered_id = await _edit_or_recover_message(
        context, query.message.chat_id, state.get("status_message_id"),
        organizing_text,
    )
    if recovered_id is not None:
        state["status_message_id"] = recovered_id

    moved, move_errors = move_batch_to_destination(
        user.id,
        mode,
    )

    if move_errors:
        error_text = "\n".join(
            f"• {error}" for error in move_errors
        )

        await _edit_or_recover_message(
            context, query.message.chat_id, state.get("status_message_id"),
            (
                f"⚠️ LOTE PROCESADO CON ERRORES\n\n"
                f"✅ Archivos guardados: {moved}\n"
                f"❌ Errores: {len(move_errors)}\n\n"
                f"{error_text}"
            ),
        )
    else:
        await _edit_or_recover_message(
            context, query.message.chat_id, state.get("status_message_id"),
            (
                f"🎉 LOTE COMPLETADO\n\n"
                f"📦 {moved} archivos\n"
                f"📁 {destination_name}\n\n"
                "✅ Todo guardado correctamente."
            ),
        )
        context.user_data.clear()
