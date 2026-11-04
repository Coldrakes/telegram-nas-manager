"""Telegram digest of recent releases; manually selected torrents -> qBittorrent."""
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from zoneinfo import ZoneInfo

import qbittorrentapi
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger(__name__)
TOKEN = os.environ['BOT_TOKEN']
USERS = {int(v.strip()) for v in os.environ['ALLOWED_USERS'].split(',') if v.strip()}
if not USERS:
    raise RuntimeError('ALLOWED_USERS obligatorio: no se permite acceso público')
SOURCE = os.getenv('SOURCE_URL', 'https://dontorrent.tips/ultimos')
DOMAIN = urlparse(SOURCE).hostname
TZ = ZoneInfo(os.getenv('TZ', 'Europe/Madrid'))
MAX_ITEMS = max(1, min(300, int(os.getenv('MAX_ITEMS', '100'))))
DB = Path('/app/data/state.sqlite3')
PAGE_SIZE = 8
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; TelegramNovedades/1.0)'})


def conn():
    c = sqlite3.connect(DB)
    c.execute('CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, title TEXT, kind TEXT, url TEXT, torrent_url TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS selections (chat_id INTEGER, id TEXT, PRIMARY KEY(chat_id,id))')
    return c


def safe_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname != DOMAIN or p.username or p.password:
        raise ValueError('URL fuera del dominio permitido')
    return url


def get_html(url):
    response = SESSION.get(safe_url(url), timeout=25, allow_redirects=False)
    response.raise_for_status()
    if 'html' not in response.headers.get('content-type', '').lower():
        raise ValueError('La respuesta no es HTML')
    return BeautifulSoup(response.text, 'html.parser')


def kind_from(text, href, context=''):
    s = f'{text} {href} {context}'.lower()
    if re.search(r'(/series/|temporada|\b\d{1,2}x\d{2}\b|\bs\d{2}e\d{2}\b)', s):
        return 'tv'
    if re.search(r'(/peliculas/|/pelicula/|\bpelículas\b|\bpeliculas\b)', s):
        return 'movies'
    return None


def parse_latest():
    soup = get_html(SOURCE)
    found = []
    seen = set()
    # The site can change markup. Collect links to release detail pages, not navigation.
    for a in soup.select('a[href]'):
        href = urljoin(SOURCE, a.get('href', ''))
        p = urlparse(href)
        if p.hostname != DOMAIN or p.scheme != 'https' or href in seen:
            continue
        if href.rstrip('/') == SOURCE.rstrip('/') or '/torrents/' in p.path or p.path.endswith('.torrent'):
            continue
        parent = a.find_parent(['article', 'tr', 'li', 'div'])
        context = parent.get_text(' ', strip=True)[:250] if parent else ''
        title = a.get_text(' ', strip=True) or (a.find('img').get('alt', '') if a.find('img') else '')
        kind = kind_from(title, href, context)
        if not kind or len(title) < 4 or len(title) > 180:
            continue
        # Avoid category/menu links and prioritize actual release entries.
        if p.path.rstrip('/').lower() in ('/series', '/peliculas', '/peliculas-estrenos', '/ultimos'):
            continue
        seen.add(href)
        found.append({'id': hashlib.sha256(href.encode()).hexdigest()[:16], 'title': title, 'kind': kind, 'url': href})
        if len(found) >= MAX_ITEMS:
            break
    return found


def refresh():
    items = parse_latest()
    with conn() as c:
        for x in items:
            c.execute('INSERT INTO items(id,title,kind,url) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,kind=excluded.kind,url=excluded.url',
                      (x['id'], x['title'], x['kind'], x['url']))
    return items


def find_torrent(item):
    soup = get_html(item['url'])
    candidates = []
    for tag in soup.select('a[href], [data-url], [data-href]'):
        raw = tag.get('href') or tag.get('data-url') or tag.get('data-href')
        if not raw:
            continue
        link = urljoin(item['url'], raw)
        if urlparse(link).path.lower().endswith('.torrent'):
            candidates.append(safe_url(link))
    if not candidates:
        raise ValueError('No se encontró enlace .torrent en el HTML de la ficha; requiere revisar el JavaScript de la web')
    # Do not guess which torrent is intended when several releases exist.
    if len(set(candidates)) != 1:
        raise ValueError(f'La ficha contiene {len(set(candidates))} torrents distintos; selección ambigua')
    return candidates[0]


def torrent_bytes(url):
    r = SESSION.get(safe_url(url), timeout=35, allow_redirects=False)
    r.raise_for_status()
    if len(r.content) > 5_000_000:
        raise ValueError('Torrent demasiado grande')
    if not r.content.startswith(b'd') or b'4:info' not in r.content:
        raise ValueError('La respuesta no parece un torrent válido')
    return r.content


def submit(item):
    url = find_torrent(item)
    content = torrent_bytes(url)
    qb = qbittorrentapi.Client(host=os.environ['QBIT_URL'], username=os.environ['QBIT_USER'], password=os.environ['QBIT_PASSWORD'], REQUESTS_ARGS={'timeout': 20})
    qb.auth_log_in()
    # Existing categories should already be configured in qBittorrent.
    if item['kind'] not in qb.torrents_categories():
        raise ValueError(f'No existe la categoría {item["kind"]} en qBittorrent')
    result = qb.torrents_add(torrent_files=content, category=item['kind'])
    if str(result).strip().lower() != 'ok.':
        raise RuntimeError(f'qBittorrent respondió: {result}')
    return url


def permitted(update):
    return bool(update.effective_user and update.effective_user.id in USERS)


async def deny(update):
    if update.callback_query:
        await update.callback_query.answer('No autorizado', show_alert=True)
    elif update.message:
        await update.message.reply_text('No autorizado')


def view(chat_id, kind='all', page=0):
    with conn() as c:
        c.row_factory = sqlite3.Row
        query = 'SELECT * FROM items WHERE kind IN (\'tv\',\'movies\')'
        args = []
        if kind in ('tv', 'movies'):
            query += ' AND kind=?'
            args.append(kind)
        # Items are inserted in website order, newest first on initial scrape.
        query += ' ORDER BY rowid DESC'
        rows = c.execute(query, args).fetchall()
        chosen = {r[0] for r in c.execute('SELECT id FROM selections WHERE chat_id=?', (chat_id,))}
    page = max(0, min(page, max(0, (len(rows)-1)//PAGE_SIZE)))
    subset = rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    buttons = [[InlineKeyboardButton(('☑ ' if r['id'] in chosen else '☐ ') + r['title'][:48], callback_data=f's:{r["id"]}:{kind}:{page}')] for r in subset]
    buttons.append([InlineKeyboardButton('Películas', callback_data='v:movies:0'), InlineKeyboardButton('Series', callback_data='v:tv:0'), InlineKeyboardButton('Todas', callback_data='v:all:0')])
    buttons.append([InlineKeyboardButton('◀', callback_data=f'v:{kind}:{max(0,page-1)}'), InlineKeyboardButton(f'{page+1}/{max(1,(len(rows)+PAGE_SIZE-1)//PAGE_SIZE)}', callback_data='noop'), InlineKeyboardButton('▶', callback_data=f'v:{kind}:{min(page+1,max(0,(len(rows)-1)//PAGE_SIZE))}')])
    buttons.append([InlineKeyboardButton(f'Descargar seleccionados ({len(chosen)})', callback_data='download')])
    return f'Novedades · {"Películas" if kind=="movies" else "Series" if kind=="tv" else "Todas"} · {len(rows)} elementos\nPulsa para seleccionar varios:', InlineKeyboardMarkup(buttons)


async def digest(app, chat_id, refresh_first=True):
    try:
        items = await asyncio.to_thread(refresh) if refresh_first else None
        if items is not None and not items:
            await app.bot.send_message(chat_id, 'No se han podido identificar novedades en /ultimos. Puede haber cambiado el HTML; no se enviarán títulos inventados.')
            return
        with conn() as c:
            rows = c.execute('SELECT title,kind FROM items ORDER BY rowid DESC LIMIT ?', (MAX_ITEMS,)).fetchall()
        movies = [r[0] for r in rows if r[1] == 'movies']
        series = [r[0] for r in rows if r[1] == 'tv']
        # Telegram message length limit; split long digests.
        for heading, titles in [('🎬 PELÍCULAS', movies), ('📺 SERIES', series)]:
            lines = [heading] + [f'• {x}' for x in titles]
            chunk = ''
            for line in lines:
                if len(chunk) + len(line) + 1 > 3500:
                    await app.bot.send_message(chat_id, chunk)
                    chunk = ''
                chunk += line + '\n'
            await app.bot.send_message(chat_id, chunk or heading + '\nSin resultados detectados')
        message, markup = view(chat_id)
        await app.bot.send_message(chat_id, message, reply_markup=markup)
    except Exception as exc:
        LOG.exception('Error al consultar novedades')
        await app.bot.send_message(chat_id, f'Error al consultar novedades: {str(exc)[:300]}')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    with conn() as c:
        c.execute('INSERT OR IGNORE INTO chats(chat_id) VALUES(?)', (update.effective_chat.id,))
    await update.message.reply_text('Bot activado. Recibirás todas las novedades cada día a las 07:00 (Europe/Madrid). Usa /novedades para consultarlas ahora.')


async def novedades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    await digest(context.application, update.effective_chat.id)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    q = update.callback_query
    await q.answer()
    data = q.data
    chat = q.message.chat_id
    if data == 'noop':
        return
    if data == 'download':
        with conn() as c:
            c.row_factory = sqlite3.Row
            items = [dict(x) for x in c.execute('SELECT i.* FROM items i JOIN selections s ON s.id=i.id WHERE s.chat_id=?', (chat,))]
        if not items:
            return await q.message.reply_text('No hay novedades seleccionadas.')
        await q.message.reply_text(f'Procesando {len(items)} selecciones…')
        results = []
        for item in items:
            try:
                await asyncio.to_thread(submit, item)
                results.append(f'✅ {item["title"][:80]} ({item["kind"]})')
                with conn() as c:
                    c.execute('DELETE FROM selections WHERE chat_id=? AND id=?', (chat, item['id']))
            except Exception as exc:
                LOG.exception('Error añadiendo %s', item['title'])
                results.append(f'❌ {item["title"][:65]}: {str(exc)[:110]}')
        for offset in range(0, len(results), 15):
            await q.message.reply_text('\n'.join(results[offset:offset+15])[:3900])
        return
    if data.startswith('v:'):
        _, kind, page = data.split(':')
    elif data.startswith('s:'):
        _, item_id, kind, page = data.split(':')
        with conn() as c:
            exists = c.execute('SELECT 1 FROM selections WHERE chat_id=? AND id=?', (chat,item_id)).fetchone()
            if exists:
                c.execute('DELETE FROM selections WHERE chat_id=? AND id=?', (chat,item_id))
            else:
                c.execute('INSERT OR IGNORE INTO selections(chat_id,id) VALUES(?,?)', (chat,item_id))
    else:
        return
    msg, keyboard = view(chat, kind, int(page))
    await q.edit_message_text(msg, reply_markup=keyboard)


async def daily(context: ContextTypes.DEFAULT_TYPE):
    with conn() as c:
        chats = [r[0] for r in c.execute('SELECT chat_id FROM chats')]
    # Refresh once, then send the same day's list to all subscribers.
    try:
        await asyncio.to_thread(refresh)
    except Exception:
        LOG.exception('No se pudo actualizar el listado diario')
    for chat in chats:
        await digest(context.application, chat, refresh_first=False)


async def post_init(app):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with conn():
        pass
    app.job_queue.run_daily(daily, time=time(int(os.getenv('DAILY_HOUR','7')), int(os.getenv('DAILY_MINUTE','0')), tzinfo=TZ), name='daily-digest')
    await app.bot.set_my_commands([('start','Activar resumen diario'),('novedades','Ver novedades ahora')])


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('novedades', novedades))
    app.add_handler(CallbackQueryHandler(callback))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
