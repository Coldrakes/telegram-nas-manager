"""Whitelisted channel synchronization: immediate per-file commit and live status."""
import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes
from config import (TG_MAX_PARALLEL, CHANNEL_BATCH_SIZE, CHANNEL_MAX_RETRIES,
                    CHANNEL_MIN_TEMP_FREE_GB, CHANNEL_MIN_NAS_FREE_GB, TEMP_PATH)
from services.allowed_channels import CHANNELS
from services.channel_db import channel_db
from services.channel_downloader import channel_downloader
from services.storage import get_destination, _get_file_destination

logger = logging.getLogger(__name__)

def _authorized(update, context):
    return bool(update.effective_user and update.effective_user.id in context.bot_data['allowed_users'])

def _free_gb(path):
    return shutil.disk_usage(path).free / (1024 ** 3)

def _target(item, dest, mode):
    """Stable path for a message; no collisions between messages with same name."""
    filename = Path(item['filename']).name
    folder = _get_file_destination(dest, mode, filename)
    p = Path(filename)
    return folder / f'{p.stem} [tg-{item["channel_id"]}-{item["message_id"]}]{p.suffix}'

def _valid_file(path, size):
    return path.is_file() and (not size or path.stat().st_size == size)

async def channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if not m or not _authorized(update, context): return
    if update.callback_query: await update.callback_query.answer()
    if not channel_downloader.available:
        await m.reply_text('⚠️ Vincula primero Telegram desde Configuración.'); return
    buttons = [[InlineKeyboardButton(f"🎬 {v['name']}", callback_data=f'channel:run:{k}')] for k,v in CHANNELS.items()]
    buttons.append([InlineKeyboardButton('❌ Cancelar',callback_data='channel:cancel')])
    if update.callback_query:
        await update.callback_query.edit_message_text('📡 Canal autorizado:',reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await m.reply_text('📡 Canal autorizado:',reply_markup=InlineKeyboardMarkup(buttons))

class Progress:
    def __init__(self, message, cid):
        self.message=message; self.cid=cid; self.last=0; self.active=''; self.current=0; self.current_size=0
        self.lock=asyncio.Lock()
    async def show(self, force=False):
        async with self.lock:
            if not force and time.monotonic()-self.last < 5: return
            self.last=time.monotonic()
            c=channel_db.counts(self.cid)
            done=c.get('completed',0); total=c['total']; pct=done/total*100 if total else 100
            width=15; filled=round(width*pct/100)
            bar='█'*filled+'░'*(width-filled)
            name=self.active[:70] if self.active else 'En espera'
            current_pct=round(self.current/self.current_size*100,1) if self.current_size else 0
            msg=(f'📡 Sincronización · Cajon Peliculas HD\n'
                 f'{bar} {pct:.1f}%\n'
                 f'✅ Guardados en NAS: {done:,}/{total:,}\n'
                 f'📦 Datos guardados: {c["bytes_completed"]/(1024**3):.2f} GB\n'
                 f'⬇️ Archivo: {name}\n'
                 f'📊 Archivo actual: {current_pct:.1f}%\n'
                 f'⚠️ Errores: {c.get("error",0):,} · Pendientes: {c.get("pending",0):,}')
            try: await self.message.edit_text(msg)
            except BadRequest as exc:
                if 'not modified' not in str(exc).lower(): logger.warning('No se pudo editar progreso: %s',exc)
            except TelegramError as exc: logger.warning('Error actualizando progreso: %s',exc)
    def callback(self, current, total):
        self.current=current; self.current_size=total
        # Telethon calls callbacks frequently; no Telegram API call per chunk.
        if time.monotonic()-self.last >= 5:
            asyncio.create_task(self.show())

async def _save_one(entity, item, mode, progress):
    cid=item['channel_id']; mid=item['message_id']; size=item['size']
    dest=get_destination(mode)
    target=Path(item.get('saved_path') or _target(item,dest,mode))
    target.parent.mkdir(parents=True,exist_ok=True)
    # The database records the intended path BEFORE moving the file. After a crash,
    # the next run can reconcile the completed move without downloading again.
    channel_db.set_status(cid,mid,'downloading',saved_path=str(target))
    if _valid_file(target,size):
        channel_db.set_status(cid,mid,'completed'); await progress.show(force=True); return
    tempdir=Path(TEMP_PATH)/'channel_sync'/str(cid)
    tempdir.mkdir(parents=True,exist_ok=True)
    temp=tempdir/f'{mid}.download'
    staged=target.with_name(target.name+f'.{mid}.incoming')
    try:
        if _free_gb(tempdir)<CHANNEL_MIN_TEMP_FREE_GB or _free_gb(dest)<CHANNEL_MIN_NAS_FREE_GB:
            raise RuntimeError('Espacio libre insuficiente en temporal o NAS')
        progress.active=item['filename']; progress.current=0; progress.current_size=size
        message=await channel_downloader.client.get_messages(entity,ids=mid)
        if not message or not message.file: raise RuntimeError('El mensaje ya no tiene archivo')
        temp.unlink(missing_ok=True)
        result=await channel_downloader.client.download_media(message,file=str(temp),progress_callback=progress.callback)
        if not result or not _valid_file(temp,size): raise RuntimeError('Descarga incompleta o tamaño incorrecto')
        channel_db.set_status(cid,mid,'downloaded')
        # Copy onto the destination filesystem, then atomically publish via rename.
        # Never expose a partially copied file as a completed download.
        staged.unlink(missing_ok=True)
        with temp.open('rb') as src, staged.open('wb') as dst:
            shutil.copyfileobj(src,dst,1024*1024)
            dst.flush(); os.fsync(dst.fileno())
        if not _valid_file(staged,size): raise RuntimeError('Copia al NAS incompleta')
        os.replace(staged,target)
        channel_db.set_status(cid,mid,'completed')
        temp.unlink(missing_ok=True)
        await progress.show(force=True)
    except Exception as exc:
        logger.exception('Error descargando canal %s mensaje %s',cid,mid)
        channel_db.set_status(cid,mid,'error',str(exc)[:500],retry=True)
        await progress.show(force=True)
    finally:
        staged.unlink(missing_ok=True)

async def process_channel(update, context, entity, mode, progress):
    cid=int(entity.id)
    channel_db.reset_interrupted(cid)
    # Sequential downloads are intentional: one clear live file progress and immediate commit.
    # The batch size only bounds the SQLite query, not a staging batch.
    while True:
        batch=channel_db.next_batch(cid,CHANNEL_BATCH_SIZE,CHANNEL_MAX_RETRIES)
        if not batch: break
        for item in batch:
            await _save_one(entity,item,mode,progress)
        await asyncio.sleep(0)
    progress.active='Finalizado';progress.current=0;progress.current_size=0
    await progress.show(force=True)

async def channel_callback(update,context):
    q=update.callback_query
    if not q or not _authorized(update,context):return
    await q.answer()
    d=q.data or ''
    if d=='channel:cancel': await q.edit_message_text('❌ Cancelado.'); return
    if not d.startswith('channel:run:'):return
    key=d.split(':',2)[2]; item=CHANNELS.get(key)
    if not item:return
    if not channel_downloader.available:
        await q.message.reply_text('⚠️ La sesión no está vinculada.');return
    if context.application.bot_data.get('channel_sync_running'):
        await q.message.reply_text('⏳ Ya hay una sincronización en curso.');return
    context.application.bot_data['channel_sync_running']=True
    try:
        await q.edit_message_text(f"🔎 Comprobando acceso a {item['name']}...")
        entity=await channel_downloader.resolve_allowed(item)
        status=await q.message.reply_text('🔎 Indexando el canal en SQLite...')
        async def scan_progress(n):
            try: await status.edit_text(f'🔎 Indexados: {n:,} archivos...')
            except TelegramError: pass
        entity,n=await channel_downloader.scan_to_db(entity,item['destination'],progress=scan_progress)
        await status.edit_text(f'✅ {n:,} archivos encontrados. Preparando descarga...')
        live=await q.message.reply_text('📡 Iniciando sincronización...')
        progress=Progress(live,int(entity.id))
        await progress.show(force=True)
        await process_channel(update,context,entity,item['destination'],progress)
    except Exception as exc:
        logger.exception('Error sincronizando %s',key)
        await q.message.reply_text(f'❌ Error de sincronización: {type(exc).__name__}. Revisa los logs.')
    finally:
        context.application.bot_data['channel_sync_running']=False

async def receive_channel_reference(update,context): return
