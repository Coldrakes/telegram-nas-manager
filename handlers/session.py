"""Vinculación de cuenta Telethon mediante QR desde un chat privado autorizado."""
import asyncio
import io
import logging

import qrcode
from telethon.errors import FloodWaitError, SessionPasswordNeededError, PasswordHashInvalidError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from services.channel_downloader import channel_downloader

log = logging.getLogger(__name__)
AUTH_TASK = None
AUTH_OWNER = None


def allowed(update, context):
    return bool(update.effective_user and update.effective_user.id in context.bot_data['allowed_users']
                and update.effective_chat and update.effective_chat.type == 'private')


def keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📱 Vincular mediante QR', callback_data='session:login')],
        [InlineKeyboardButton('🔎 Comprobar acceso', callback_data='session:check')],
        [InlineKeyboardButton('🚪 Cerrar sesión', callback_data='session:logout')],
        [InlineKeyboardButton('❌ Cancelar vinculación', callback_data='session:cancel')],
    ])


async def session_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update, context):
        return
    if update.callback_query:
        await update.callback_query.answer()
    state = '🟢 Conectada' if channel_downloader.available else '🔴 Sin vincular'
    await update.effective_message.reply_text(
        f'⚙️ Sesión de usuario Telegram\nEstado: {state}\n\n'
        'Vincula tu cuenta escaneando un QR desde Telegram → Ajustes → Dispositivos → Vincular dispositivo.',
        reply_markup=keyboard(),
    )


async def session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTH_TASK, AUTH_OWNER
    q = update.callback_query
    if not q or not allowed(update, context):
        return
    await q.answer()
    action = q.data.split(':', 1)[1]
    if action == 'cancel':
        context.user_data.pop('session_step', None)
        if AUTH_TASK and not AUTH_TASK.done() and AUTH_OWNER == update.effective_user.id:
            AUTH_TASK.cancel()
            await q.message.reply_text('Vinculación cancelada.')
        else:
            await q.message.reply_text('No hay ninguna vinculación QR en curso.')
    elif action == 'login':
        if AUTH_TASK and not AUTH_TASK.done():
            await q.message.reply_text('Ya hay una vinculación en curso. Cancélala antes de iniciar otra.')
            return
        if channel_downloader.available:
            await q.message.reply_text('Ya hay una sesión vinculada. Cierra sesión para vincular otra.')
            return
        AUTH_OWNER = update.effective_user.id
        AUTH_TASK = context.application.create_task(_qr_login(context.application, q.message.chat_id, AUTH_OWNER))
        await q.message.reply_text('Preparando QR de Telegram…')
    elif action == 'check':
        if not channel_downloader.available:
            await q.message.reply_text('Primero vincula la cuenta.')
            return
        from services.allowed_channels import CHANNELS
        for item in CHANNELS.values():
            try:
                entity = await channel_downloader.resolve_allowed(item)
                await q.message.reply_text(f"✅ {item['name']}: acceso confirmado ({entity.id}).")
            except Exception:
                log.exception('No se pudo comprobar acceso al canal %s', item['name'])
                await q.message.reply_text(f"❌ {item['name']}: no se pudo confirmar el acceso.")
    elif action == 'logout':
        if AUTH_TASK and not AUTH_TASK.done():
            await q.message.reply_text('Cancela primero la vinculación QR.')
            return
        if context.application.bot_data.get('channel_sync_running'):
            await q.message.reply_text('No se puede cerrar sesión durante una sincronización.')
            return
        try:
            await channel_downloader.logout()
            await q.message.reply_text('Sesión de usuario cerrada.')
        except Exception:
            log.exception('Fallo cerrando sesión')
            await q.message.reply_text('No se pudo cerrar la sesión. Consulta los logs.')


async def _qr_login(application, chat_id, owner_id):
    global AUTH_TASK, AUTH_OWNER
    client = channel_downloader.client
    qr_message = None
    try:
        if not client.is_connected():
            await client.connect()
        qr = await client.qr_login()
        # El token QR caduca; se renueva automáticamente un número limitado de veces.
        for attempt in range(5):
            picture = qrcode.make(qr.url)
            buf = io.BytesIO()
            picture.save(buf, format='PNG')
            buf.seek(0)
            buf.name = 'telegram-login.png'
            if qr_message:
                try:
                    await qr_message.delete()
                except Exception:
                    pass
            qr_message = await application.bot.send_photo(
                chat_id, photo=buf,
                caption='📱 Escanea este QR con Telegram → Ajustes → Dispositivos → Vincular dispositivo.\n'
                        'No compartas esta imagen. Caduca aproximadamente en un minuto.',
            )
            try:
                await asyncio.wait_for(qr.wait(), timeout=55)
                break
            except asyncio.TimeoutError:
                if attempt == 4:
                    await application.bot.send_message(chat_id, '⌛ Tiempo agotado. Pulsa Vincular mediante QR para reintentarlo.')
                    return
                await qr.recreate()
            except SessionPasswordNeededError:
                application.bot_data['qr_password_owner'] = owner_id
                await application.bot.send_message(
                    chat_id, '🔐 Tu cuenta tiene verificación en dos pasos. Introduce la contraseña aquí '
                             'para terminar la vinculación (se intentará borrar el mensaje). '
                             'Es una solución temporal de pruebas; /cancelar para salir.'
                )
                return
        channel_downloader._started = await client.is_user_authorized()
        if channel_downloader.available:
            await application.bot.send_message(chat_id, '✅ Cuenta vinculada correctamente. Usa Comprobar acceso para verificar los canales.')
        else:
            await application.bot.send_message(chat_id, '❌ Telegram no confirmó la autorización. Inténtalo de nuevo.')
    except asyncio.CancelledError:
        raise
    except FloodWaitError as exc:
        await application.bot.send_message(chat_id, f'⏳ Telegram exige esperar {exc.seconds} segundos.')
    except Exception:
        log.exception('Error vinculando la sesión mediante QR')
        await application.bot.send_message(chat_id, '❌ Error durante la vinculación QR. Consulta los logs.')
    finally:
        if qr_message:
            try:
                await qr_message.delete()
            except Exception:
                pass
        if not channel_downloader.available and application.bot_data.get('qr_password_owner') != owner_id:
            try:
                await client.disconnect()
            except Exception:
                pass
        AUTH_OWNER = None


async def session_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update, context) or not update.message or not update.message.text:
        return
    if context.application.bot_data.get('qr_password_owner') != update.effective_user.id:
        return
    value = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if value.lower() in ('/cancel', '/cancelar'):
        context.application.bot_data.pop('qr_password_owner', None)
        await channel_downloader.client.disconnect()
        await update.effective_chat.send_message('Vinculación cancelada.')
        return
    try:
        await channel_downloader.client.sign_in(password=value)
        channel_downloader._started = await channel_downloader.client.is_user_authorized()
        context.application.bot_data.pop('qr_password_owner', None)
        await update.effective_chat.send_message('✅ Cuenta vinculada correctamente.' if channel_downloader.available else '❌ No se pudo autorizar la cuenta.')
    except PasswordHashInvalidError:
        await update.effective_chat.send_message('Contraseña incorrecta. Inténtalo de nuevo o escribe /cancelar.')
    except Exception:
        log.exception('Error completando autenticación 2FA')
        context.application.bot_data.pop('qr_password_owner', None)
        await update.effective_chat.send_message('❌ Error de autenticación. Consulta los logs.')
