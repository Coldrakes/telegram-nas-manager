from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from services.batch_db import batch_db as db
from handlers.files import show, launch, title, authorized

def home_markup():return K([[B('➕ Nuevo lote',callback_data='nav:new'),B('📋 Mis lotes',callback_data='nav:lots')],[B('📊 Actividad',callback_data='nav:activity'),B('⚙️ Ajustes',callback_data='nav:settings')]])

async def start(update,context):
    if not authorized(update,context):return
    await update.message.reply_text('🤖 ColdNas Manager\n\nGestor de descargas al NAS',reply_markup=home_markup())

async def create_mode(context,user,chat,mode):
    existing=db.active(user)
    if existing:return existing['id'],False
    return db.create(user,chat,mode),True

async def mode_callback(update,context):
    q=update.callback_query
    if not authorized(update,context):await q.answer('No autorizado',show_alert=True);return
    mode=q.data.split(':')[1]
    if mode not in ('movies','series','3d'):await q.answer('Modo inválido');return
    bid,created=await create_mode(context,q.from_user.id,q.message.chat_id,mode)
    await q.answer('Lote creado' if created else 'Ya tienes un lote abierto')
    await show(context,bid,True)

async def command_mode(update,context,mode):
    if not authorized(update,context):return
    bid,_=await create_mode(context,update.effective_user.id,update.effective_chat.id,mode)
    await show(context,bid,True)

async def movies(u,c):await command_mode(u,c,'movies')
async def series(u,c):await command_mode(u,c,'series')
async def three_d(u,c):await command_mode(u,c,'3d')
async def nuevo(u,c):
    if authorized(u,c):await u.message.reply_text('Selecciona destino:',reply_markup=mode_markup())

def mode_markup():return K([[B('🎬 Películas',callback_data='mode:movies'),B('📺 Series',callback_data='mode:series')],[B('🧊 3D',callback_data='mode:3d')],[B('🏠 Panel',callback_data='nav:home')]])

async def navigation(update,context):
    q=update.callback_query
    if not authorized(update,context):await q.answer('No autorizado',show_alert=True);return
    await q.answer();action=q.data.split(':')[1]
    if action=='home':
        batches=db.list(q.from_user.id)
        active=[b for b in batches if b['status'] not in ('done','cancelled')]
        text=f'🤖 COLDNAS MANAGER\n\n📦 Lotes activos: {len(active)}\n'
        text+='\n'.join(f"• #{b['id']} {title(b['mode'])}: {b['status']}" for b in active[:8])
        await q.edit_message_text(text,reply_markup=home_markup())
    elif action=='new':await q.edit_message_text('➕ Nuevo lote\nSelecciona destino:',reply_markup=mode_markup())
    elif action=='lots':
        batches=db.list(q.from_user.id)
        rows=[[B(f"#{b['id']} {title(b['mode'])} · {b['status']}",callback_data=f"b:view:{b['id']}")] for b in batches[:15]]
        rows.append([B('🏠 Panel',callback_data='nav:home')])
        await q.edit_message_text('📋 Mis lotes',reply_markup=K(rows))
    elif action=='activity':
        batches=db.list(q.from_user.id)
        text='📊 Actividad reciente\n\n'+'\n'.join(f"#{b['id']} {title(b['mode'])}: {b['status']}" for b in batches[:12])
        await q.edit_message_text(text,reply_markup=K([[B('🏠 Panel',callback_data='nav:home')]]))
    elif action=='settings':
        from config import TG_PROGRESS_INTERVAL,TG_MAX_PARALLEL
        await q.edit_message_text(f'⚙️ Configuración\n\nRefresco: {TG_PROGRESS_INTERVAL:.0f} s\nDescargas paralelas: {TG_MAX_PARALLEL}\nDescompresión: activada\nSobrescritura: siempre\nBorrar comprimidos tras verificar: sí\n\nCambiar refresco con TG_PROGRESS_INTERVAL=30 o 60 en .env y reiniciar.',reply_markup=K([[B('🏠 Panel',callback_data='nav:home')]]))
