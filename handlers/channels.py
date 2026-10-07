from __future__ import annotations
import asyncio, shutil
from pathlib import Path
from telegram import InlineKeyboardButton,InlineKeyboardMarkup,Update
from telegram.ext import ContextTypes
from config import (TG_MAX_PARALLEL,CHANNEL_BATCH_SIZE,CHANNEL_MAX_RETRIES,CHANNEL_RETRY_DELAY,CHANNEL_MIN_TEMP_FREE_GB,CHANNEL_MIN_NAS_FREE_GB)
from services.archive_manager import extract_archives
from services.channel_db import channel_db
from services.channel_downloader import channel_downloader
from services.storage import clear_temp_batch,get_temp_batch_dir,get_destination,move_batch_to_destination

def _authorized(u,c):return bool(u.effective_user and u.effective_user.id in c.bot_data['allowed_users'])
def _free_gb(p):return shutil.disk_usage(p).free/(1024**3)

async def channel_start(update:Update,context:ContextTypes.DEFAULT_TYPE):
 m=update.effective_message
 if not m or not _authorized(update,context):return
 if update.callback_query:await update.callback_query.answer()
 if not channel_downloader.available:
  await m.reply_text('⚠️ La sesión de usuario para canales no está autorizada.\nEjecuta: python tools/create_channel_session.py');return
 context.user_data.clear();kb=InlineKeyboardMarkup([[InlineKeyboardButton('🎬 Películas',callback_data='channel:dest:movies'),InlineKeyboardButton('📺 Series',callback_data='channel:dest:series')],[InlineKeyboardButton('🧊 3D',callback_data='channel:dest:3d'),InlineKeyboardButton('❌ Cancelar',callback_data='channel:cancel')]])
 text='📡 SINCRONIZAR CANAL\n\nSelecciona dónde quieres guardar los archivos:'
 if update.callback_query:await update.callback_query.edit_message_text(text,reply_markup=kb)
 else:await m.reply_text(text,reply_markup=kb)
async def channel_callback(update,context):
 q=update.callback_query
 if not q or not _authorized(update,context):return
 await q.answer();d=q.data or ''
 if d=='channel:cancel':context.user_data.clear();await q.edit_message_text('❌ Sincronización cancelada.');return
 if not d.startswith('channel:dest:'):return
 mode=d.rsplit(':',1)[1];context.user_data.clear();context.user_data.update(channel_mode=mode,awaiting_channel=True)
 await q.edit_message_text('📡 SINCRONIZAR CANAL\n\nEnvíame el enlace o @usuario del canal.\nEl historial se indexará directamente en SQLite sin cargarlo entero en memoria.')
async def receive_channel_reference(update,context):
 m=update.effective_message
 if not m or not m.text or not context.user_data.get('awaiting_channel') or not _authorized(update,context):return
 ref=m.text.strip();mode=context.user_data.get('channel_mode');context.user_data['awaiting_channel']=False
 status=await m.reply_text('🔎 Indexando canal en SQLite... 0 archivos')
 async def progress(n):
  try:await status.edit_text(f'🔎 Indexando canal en SQLite... {n:,} archivos encontrados')
  except Exception:pass
 try:e,n=await channel_downloader.scan_to_db(ref,mode,progress)
 except Exception as ex:await status.edit_text(f'❌ No se pudo leer el canal: {ex}');return
 counts=channel_db.counts(int(e.id));await status.edit_text(f"✅ Índice preparado: {counts['total']:,} archivos conocidos.\n🚀 Iniciando cola persistente por bloques de {CHANNEL_BATCH_SIZE}...")
 await process_channel(update,context,e,mode)
async def process_channel(update,context,e,mode):
 m=update.effective_message;uid=update.effective_user.id;cid=int(e.id);channel_db.reset_interrupted(cid);sem=asyncio.Semaphore(TG_MAX_PARALLEL)
 while True:
  batch=channel_db.next_batch(cid,CHANNEL_BATCH_SIZE,CHANNEL_MAX_RETRIES)
  if not batch:break
  clear_temp_batch(uid);td=get_temp_batch_dir(uid);dest=get_destination(mode)
  if _free_gb(td)<CHANNEL_MIN_TEMP_FREE_GB or _free_gb(dest)<CHANNEL_MIN_NAS_FREE_GB:
   await m.reply_text(f'⏸ Sincronización pausada por espacio libre.\nTEMP: {_free_gb(td):.1f} GB · NAS: {_free_gb(dest):.1f} GB');return
  async def one(item):
   async with sem:
    channel_db.set_status(cid,item['message_id'],'downloading')
    try:
     await channel_downloader.download(e,item,td/Path(item['filename']).name);channel_db.set_status(cid,item['message_id'],'downloaded')
    except Exception as ex:
     channel_db.set_status(cid,item['message_id'],'error',str(ex),retry=True)
  await asyncio.gather(*(one(x) for x in batch))
  downloaded=[x for x in batch if channel_db.counts(cid)] # status is checked below from DB lifecycle
  extraction=await extract_archives(td)
  if extraction.errors:
   # Conservador: no marcar completado ni mover si el bloque tiene una extracción inválida.
   for x in batch:
    channel_db.set_status(cid,x['message_id'],'error','Error de extracción/multipartes; se reintentará el bloque',retry=True)
   await asyncio.sleep(CHANNEL_RETRY_DELAY);continue
  moved,errs=move_batch_to_destination(uid,mode)
  if errs:
   for x in batch:channel_db.set_status(cid,x['message_id'],'error','Error moviendo al NAS',retry=True)
   await asyncio.sleep(CHANNEL_RETRY_DELAY);continue
  for x in batch:channel_db.set_status(cid,x['message_id'],'completed')
  c=channel_db.counts(cid);await m.reply_text(f"📡 Progreso: {c.get('completed',0):,}/{c['total']:,} · errores: {c.get('error',0):,}")
 c=channel_db.counts(cid);await m.reply_text(f"🎉 Sincronización finalizada.\n✅ {c.get('completed',0):,}/{c['total']:,}\n❌ Errores agotados: {c.get('error',0):,}")
 context.user_data.clear()
