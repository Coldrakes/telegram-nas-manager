"""Vinculación de la sesión de usuario de Telethon mediante chat privado.

Los códigos y contraseñas se reciben en el chat: modo experimental.
"""
import asyncio
import logging

from telethon.errors import (
    FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    SessionPasswordNeededError, PasswordHashInvalidError,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from services.channel_downloader import channel_downloader

log = logging.getLogger(__name__)
SESSION_LOCK = asyncio.Lock()


def allowed(update, context):
    return bool(update.effective_user and update.effective_user.id in context.bot_data['allowed_users']
                and update.effective_chat and update.effective_chat.type == 'private')


def keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔗 Vincular cuenta', callback_data='session:login')],
        [InlineKeyboardButton('🔎 Comprobar acceso', callback_data='session:check')],
        [InlineKeyboardButton('🚪 Cerrar sesión', callback_data='session:logout')],
        [InlineKeyboardButton('❌ Cancelar', callback_data='session:cancel')],
    ])


async def session_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update, context):
        return
    if update.callback_query:
        await update.callback_query.answer()
    state = '🟢 Conectada' if channel_downloader.available else '🔴 Sin vincular'
    await update.effective_message.reply_text(
        f'⚙️ Sesión de usuario Telegram\nEstado: {state}\n\n'
        'Solo en chat privado. Durante las pruebas los códigos y la contraseña se envían al bot.',
        reply_markup=keyboard(),
    )


async def session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not allowed(update, context):
        return
    await q.answer()
    action = q.data.split(':', 1)[1]
    if action == 'cancel':
        context.user_data.pop('session_step', None)
        context.user_data.pop('session_phone', None)
        context.user_data.pop('session_hash', None)
        await q.message.reply_text('Operación cancelada.')
    elif action == 'login':
        if SESSION_LOCK.locked():
            await q.message.reply_text('Ya hay una operación de sesión en curso.')
            return
        if channel_downloader.available:
            await q.message.reply_text('Ya hay una sesión vinculada. Cierra la sesión antes de vincular otra.')
            return
        context.user_data['session_step'] = 'phone'
        await q.message.reply_text('Introduce el teléfono con prefijo internacional (+34...). /cancelar para salir.')
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
                await q.message.reply_text(f"❌ {item['name']}: sin acceso. Únete al canal con la cuenta vinculada.")
    elif action == 'logout':
        if SESSION_LOCK.locked():
            await q.message.reply_text('Espera a que termine la autenticación actual.')
            return
        try:
            await channel_downloader.logout()
            await q.message.reply_text('Sesión de usuario cerrada. La cuenta deberá vincularse de nuevo.')
        except Exception:
            log.exception('Fallo cerrando sesión')
            await q.message.reply_text('No se pudo cerrar la sesión. Consulta los logs.')


async def session_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('session_step')
    if not step or not allowed(update, context) or not update.message or not update.message.text:
        return
    message = update.message
    value = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    if value.lower() in ('/cancelar', '/cancel'):
        context.user_data.pop('session_step', None)
        await context.bot.send_message(update.effective_chat.id, 'Vinculación cancelada.')
        return
    async with SESSION_LOCK:
        try:
            client = channel_downloader.client
            if not client.is_connected():
                await client.connect()
            if step == 'phone':
                if not value.startswith('+') or not value[1:].isdigit():
                    await message.chat.send_message('Teléfono inválido. Introduce +34...')
                    return
                sent = await client.send_code_request(value)
                context.user_data.update(session_step='code', session_phone=value, session_hash=sent.phone_code_hash)
                await message.chat.send_message('Introduce el código de Telegram. El mensaje se intentará borrar.')
            elif step == 'code':
                try:
                    await client.sign_in(phone=context.user_data['session_phone'], code=value.replace(' ', ''),
                                         phone_code_hash=context.user_data['session_hash'])
                except SessionPasswordNeededError:
                    context.user_data['session_step'] = 'password'
                    await message.chat.send_message('Introduce la contraseña de verificación en dos pasos.')
                    return
                await _finish(context, message)
            elif step == 'password':
                await client.sign_in(password=value)
                await _finish(context, message)
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            await message.chat.send_message('Código inválido o caducado. Cancela y vuelve a iniciar la vinculación.')
        except PasswordHashInvalidError:
            await message.chat.send_message('Contraseña incorrecta. Inténtalo otra vez.')
        except FloodWaitError as exc:
            context.user_data.pop('session_step', None)
            await message.chat.send_message(f'Telegram exige esperar {exc.seconds} segundos.')
        except Exception:
            log.exception('Error durante la vinculación (sin registrar credenciales)')
            context.user_data.pop('session_step', None)
            await message.chat.send_message('Error de autenticación. Consulta los logs e inténtalo de nuevo.')


async def _finish(context, message):
    channel_downloader._started = await channel_downloader.client.is_user_authorized()
    for key in ('session_step', 'session_phone', 'session_hash'):
        context.user_data.pop(key, None)
    await message.chat.send_message('✅ Cuenta vinculada correctamente. Usa Configuración → Comprobar acceso.')
