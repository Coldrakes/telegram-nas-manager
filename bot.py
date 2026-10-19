import logging
from telegram import BotCommand,MenuButtonCommands
from telegram.ext import Application,CallbackQueryHandler,CommandHandler,MessageHandler,filters
from config import ALLOWED_USERS,BOT_TOKEN
from handlers.commands import start,movies,series,three_d,nuevo,mode_callback,navigation
from handlers.files import receive_file,batch_callback,launch
from services.batch_db import batch_db
from services.storage import prepare_directories
from services.telethon_downloader import telethon_downloader

logging.basicConfig(level=logging.INFO)
async def post_init(app):
    batch_db.init();batch_db.interrupted()
    await telethon_downloader.start()
    await app.bot.set_my_commands([BotCommand('start','Panel principal'),BotCommand('nuevo','Nuevo lote'),BotCommand('peliculas','Lote películas'),BotCommand('series','Lote series'),BotCommand('3d','Lote 3D')])
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    for batch in batch_db.unfinished():launch(app,batch['id'])
async def post_shutdown(app):await telethon_downloader.stop()
async def on_error(update,context):logging.exception('Error en actualización',exc_info=context.error)
def main():
    prepare_directories();batch_db.init()
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.bot_data['allowed_users']=ALLOWED_USERS
    for name,fn in [('start',start),('nuevo',nuevo),('peliculas',movies),('series',series),('3d',three_d)]:app.add_handler(CommandHandler(name,fn))
    app.add_handler(CallbackQueryHandler(mode_callback,pattern=r'^mode:'))
    app.add_handler(CallbackQueryHandler(navigation,pattern=r'^nav:'))
    app.add_handler(CallbackQueryHandler(batch_callback,pattern=r'^b:'))
    app.add_handler(MessageHandler(filters.Document.ALL,receive_file))
    app.add_error_handler(on_error)
    app.run_polling()
if __name__=='__main__':main()
