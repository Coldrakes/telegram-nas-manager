"""Manual batches: persistent queue, progress panel, restart recovery."""
import asyncio
import logging
import time
from pathlib import Path
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.error import BadRequest, RetryAfter, NetworkError, TimedOut
from config import TG_MAX_PARALLEL, TG_PROGRESS_INTERVAL
from services.batch_db import batch_db as db
from services.storage import get_temp_batch_dir, move_batch_to_destination
from services.archive_manager import extract_archives
from services.telethon_downloader import telethon_downloader

log=logging.getLogger(__name__)
RUNNING={}
PAUSED=set()

def authorized(update,context):
    return bool(update.effective_user and update.effective_user.id in context.bot_data['allowed_users'])

def title(mode):return {'movies':'🎬 Películas','series':'📺 Series','3d':'🧊 3D'}.get(mode,mode)

def kb(bid,status):
    rows=[]
    if status=='open':rows.append([B('✅ Cerrar y descargar',callback_data=f'b:finish:{bid}'),B('❌ Cancelar',callback_data=f'b:cancel:{bid}')])
    elif status in ('queued','downloading','paused'):
        rows.append([B('▶️ Reanudar' if status=='paused' else '⏸ Pausar',callback_data=f'b:toggle:{bid}'),B('📄 Archivos',callback_data=f'b:files:{bid}:0:all')])
    else:rows.append([B('📄 Archivos',callback_data=f'b:files:{bid}:0:all')])
    rows.append([B('🏠 Panel',callback_data='nav:home'),B('📋 Mis lotes',callback_data='nav:lots')])
    return K(rows)

def status_text(batch,active=None):
    items=db.items(batch['id']);done=sum(i['status']=='done' for i in items);fail=sum(i['status']=='error' for i in items)
    total=len(items);bytes_done=sum(i['size'] for i in items if i['status']=='done');size=sum(i['size'] for i in items)
    icons={'open':'📝 Preparando','queued':'⏳ En cola','downloading':'⬇️ Descargando','paused':'⏸ Pausado','extracting':'📦 Descomprimiendo','organizing':'💾 Guardando en NAS','done':'✅ Completado','error':'⚠️ Con errores','cancelled':'❌ Cancelado'}
    lines=[f"{title(batch['mode'])} · Lote #{batch['id']}",f"{icons.get(batch['status'],batch['status'])}",'',f'📦 {done}/{total} archivos · {fail} errores',f'💾 {bytes_done/1073741824:.2f}/{size/1073741824:.2f} GB',f'🔄 Paralelas: {TG_MAX_PARALLEL}']
    if active:
        lines.append('');lines.append('Descargas activas:')
        for name,(cur,total_bytes) in list(active.items())[:TG_MAX_PARALLEL]:
            pct=100*cur/total_bytes if total_bytes else 0
            lines.append(f'• {name[:65]}: {pct:.1f}%')
    if batch['status']=='open':lines.extend(['','Envía archivos y pulsa «Cerrar y descargar».'])
    if fail:lines.extend(['','Consulta «Archivos» para ver los errores.'])
    return '\n'.join(lines)[:3900]

async def show(context,bid,force=False,active=None):
    batch=db.get(bid)
    if not batch:return
    last=context.application.bot_data.setdefault('last_ui',{})
    if not force and time.monotonic()-last.get(bid,0)<TG_PROGRESS_INTERVAL:return
    last[bid]=time.monotonic()
    text=status_text(batch,active)
    try:
        if batch['message_id']:
            await context.bot.edit_message_text(chat_id=batch['chat_id'],message_id=batch['message_id'],text=text,reply_markup=kb(bid,batch['status']))
        else:
            msg=await context.bot.send_message(batch['chat_id'],text,reply_markup=kb(bid,batch['status']))
            db.message(bid,msg.message_id)
    except BadRequest as e:
        if 'not modified' in str(e).lower():return
        log.warning('Panel not editable: %s',e)
        try:
            msg=await context.bot.send_message(batch['chat_id'],text,reply_markup=kb(bid,batch['status']))
            db.message(bid,msg.message_id)
        except Exception:log.exception('Could not replace panel')
    except (TimedOut,NetworkError,RetryAfter):log.warning('Transient Telegram UI failure; will retry')

async def receive_file(update,context):
    if not authorized(update,context):return
    msg=update.message
    if not msg or not msg.document:return
    batch=db.active(update.effective_user.id)
    if not batch:
        await msg.reply_text('Primero crea un lote con /nuevo, /series, /peliculas o /3d.')
        return
    name=Path(msg.document.file_name or f'archivo_{msg.message_id}').name
    if name in ('.','..'):return
    db.add(batch['id'],msg.chat_id,msg.message_id,name,msg.document.file_size or 0)
    await show(context,batch['id'],force=True)

async def run_batch(application,bid):
    if bid in RUNNING:return
    batch=db.get(bid)
    if not batch or batch['status'] not in ('queued','downloading','organizing','extracting'):return
    RUNNING[bid]=asyncio.current_task()
    class C:pass
    context=C();context.bot=application.bot;context.application=application
    active={};temp=get_temp_batch_dir(bid)
    try:
        db.batch_status(bid,'downloading');await show(context,bid,True)
        sem=asyncio.Semaphore(TG_MAX_PARALLEL)
        async def one(item):
            async with sem:
                while bid in PAUSED:await asyncio.sleep(1)
                dest=temp/item['filename']
                if item['status']=='done' and dest.is_file() and dest.stat().st_size==item['size']:return
                if dest.exists() and dest.stat().st_size==item['size'] and item['size']>0:
                    db.item_status(item['id'],'done');return
                db.item_status(item['id'],'downloading')
                async def progress(current,total):
                    active[item['filename']]=(current,total)
                try:
                    # Telethon download_media doesn't guarantee byte-level resume.
                    # An interrupted file restarts, but completed files are reused.
                    if dest.exists():dest.unlink()
                    await telethon_downloader.download_message(item['chat_id'],item['message_id'],dest,progress)
                    if not dest.is_file() or (item['size'] and dest.stat().st_size!=item['size']):
                        raise RuntimeError('Tamaño descargado incorrecto')
                    db.item_status(item['id'],'done')
                except asyncio.CancelledError:
                    db.item_status(item['id'],'pending');raise
                except Exception as exc:
                    db.item_status(item['id'],'error',str(exc)[:500]);log.exception('Download failed: %s',item['filename'])
                finally:active.pop(item['filename'],None)
        jobs=[asyncio.create_task(one(i)) for i in db.items(bid) if i['status']!='done' or not (temp/i['filename']).is_file()]
        while any(not j.done() for j in jobs):
            await show(context,bid,active=active)
            await asyncio.sleep(2)
        if jobs:await asyncio.gather(*jobs)
        if any(i['status']=='error' for i in db.items(bid)):
            db.batch_status(bid,'error');await show(context,bid,True);return
        db.batch_status(bid,'extracting');await show(context,bid,True)
        report=await extract_archives(temp)
        if report.errors:
            db.batch_status(bid,'error')
            log.error('Extraction errors for batch %s: %s',bid,report.errors)
            await show(context,bid,True);return
        db.batch_status(bid,'organizing');await show(context,bid,True)
        moved,errors=move_batch_to_destination(bid,batch['mode'])
        db.batch_status(bid,'error' if errors else 'done')
        if errors:log.error('Move errors batch %s: %s',bid,errors)
        await show(context,bid,True)
    except asyncio.CancelledError:
        db.batch_status(bid,'queued');raise
    except Exception:
        log.exception('Batch %s interrupted',bid)
        db.batch_status(bid,'queued')
    finally:RUNNING.pop(bid,None)

def launch(application,bid):
    if bid not in RUNNING:application.create_task(run_batch(application,bid))

async def batch_callback(update,context):
    q=update.callback_query
    if not authorized(update,context):await q.answer('No autorizado',show_alert=True);return
    _,action,id_str,*rest=q.data.split(':');bid=int(id_str);batch=db.get(bid)
    if not batch or batch['user_id']!=q.from_user.id:await q.answer('Lote no encontrado',show_alert=True);return
    await q.answer()
    if action=='finish' and batch['status']=='open':
        if not db.items(bid):await q.answer('Lote vacío',show_alert=True);return
        db.batch_status(bid,'queued');launch(context.application,bid)
    elif action=='cancel' and batch['status']=='open':db.batch_status(bid,'cancelled')
    elif action=='toggle':
        if bid in PAUSED or batch['status']=='paused':
            PAUSED.discard(bid);db.batch_status(bid,'downloading');launch(context.application,bid)
        else:
            PAUSED.add(bid);db.batch_status(bid,'paused')
    elif action=='files':
        page=int(rest[0]) if rest else 0;filt=rest[1] if len(rest)>1 else 'all'
        items=[i for i in db.items(bid) if filt=='all' or i['status']==filt];start=page*10
        lines=[f'📄 Lote #{bid} · Archivos ({len(items)})','']
        for i in items[start:start+10]:lines.append(f"{'✅' if i['status']=='done' else '❌' if i['status']=='error' else '⏳'} {i['filename'][:75]}"+(f"\n   {i['error'][:120]}" if i['error'] else ''))
        rows=[[B('Todos',callback_data=f'b:files:{bid}:0:all'),B('Errores',callback_data=f'b:files:{bid}:0:error')]]
        paging=[]
        if page:paging.append(B('⬅️',callback_data=f'b:files:{bid}:{page-1}:{filt}'))
        if start+10<len(items):paging.append(B('➡️',callback_data=f'b:files:{bid}:{page+1}:{filt}'))
        if paging:rows.append(paging)
        rows.append([B('↩️ Volver al lote',callback_data=f'b:view:{bid}')])
        await q.edit_message_text('\n'.join(lines)[:3900],reply_markup=K(rows));return
    elif action=='view':
        await q.edit_message_text(status_text(db.get(bid)),reply_markup=kb(bid,db.get(bid)['status']));return
    await show(context,bid,True)

async def finish_batch(update,context):
    batch=db.active(update.effective_user.id)
    if not batch:return
    update.callback_query.data=f'b:finish:{batch["id"]}'
    await batch_callback(update,context)

async def track_user_message(update,context):pass
