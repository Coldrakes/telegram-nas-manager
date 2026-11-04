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
from html import escape
from zoneinfo import ZoneInfo

import qbittorrentapi
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
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
    c.execute('CREATE TABLE IF NOT EXISTS downloaded (chat_id INTEGER, id TEXT, PRIMARY KEY(chat_id,id))')
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
    """Read the current /ultimos page, retaining only movie release links."""
    soup = get_html(SOURCE)
    found, seen = [], set()
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
        if kind != 'movies' or len(title) < 4 or len(title) > 180:
            continue
        if p.path.rstrip('/').lower() in ('/series', '/peliculas', '/peliculas-estrenos', '/ultimos'):
            continue
        seen.add(href)
        found.append({'id': hashlib.sha256(href.encode()).hexdigest()[:16], 'title': title, 'kind': 'movies', 'url': href})
        if len(found) >= MAX_ITEMS:
            break
    return found


def text_of(soup, selectors):
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get('content', '') if node.name == 'meta' else node.get_text(' ', strip=True)
            if value:
                return value.strip()
    return ''


def detail(item):
    soup = get_html(item['url'])
    title = text_of(soup, ['h1', 'meta[property="og:title"]']) or item['title']
    desc = text_of(soup, ['.sinopsis', '.synopsis', '.descripcion', '.description', '[itemprop="description"]', 'meta[name="description"]', 'meta[property="og:description"]'])
    genres = text_of(soup, ['.generos', '.genres', '.genre', '[itemprop="genre"]'])
    quality = text_of(soup, ['.calidad', '.quality', '.formato', '.format'])
    if not quality:
        m = re.search(r'\b(2160p|1080p|720p|4k|bluray|web[- .]?dl|hdtv|remux)\b', item['title'], re.I)
        quality = m.group(0) if m else ''
    image = ''
    for selector in ['meta[property="og:image"]', '.poster img', '.cover img', '.caratula img', 'article img', 'main img']:
        node = soup.select_one(selector)
        if node:
            raw = node.get('content') or node.get('data-src') or node.get('src')
            if raw:
                candidate = urljoin(item['url'], raw)
                if urlparse(candidate).scheme == 'https' and urlparse(candidate).hostname == DOMAIN:
                    image = candidate
                    break
    return {'title': title, 'description': desc, 'genres': genres, 'quality': quality, 'image': image}


def caption(item, info):
    lines = [f'<b>🎬 {escape(info["title"][:180])}</b>']
    if info['genres']:
        lines.append('🎭 <b>Géneros:</b> ' + escape(info['genres'][:180]))
    if info['quality']:
        lines.append('🎞 <b>Formato:</b> ' + escape(info['quality'][:120]))
    if info['description']:
        lines.append('\n📖 ' + escape(info['description'][:550]))
    lines.append('\n<a href="' + escape(item['url'], quote=True) + '">Ver ficha en DonTorrent</a>')
    return '\n'.join(lines)[:1000]


def keyboard(item, downloaded=False):
    return InlineKeyboardMarkup([[InlineKeyboardButton('✅ Añadida a qBittorrent' if downloaded else '📥 Descargar', callback_data='done' if downloaded else 'get:' + item['id'])]])


async def send_card(app, chat_id, item):
    try:
        info = await asyncio.to_thread(detail, item)
    except Exception:
        LOG.exception('No se pudo leer la ficha %s', item['url'])
        info = {'title': item['title'], 'description': '', 'genres': '', 'quality': '', 'image': ''}
    with conn() as c:
        done = bool(c.execute('SELECT 1 FROM downloaded WHERE chat_id=? AND id=?', (chat_id, item['id'])).fetchone())
    body = caption(item, info)
    if info['image']:
        try:
            r = await asyncio.to_thread(lambda: SESSION.get(safe_url(info['image']), timeout=20, allow_redirects=False))
            r.raise_for_status()
            if r.headers.get('content-type', '').lower().startswith('image/') and len(r.content) < 9_000_000:
                import io
                await app.bot.send_photo(chat_id, photo=io.BytesIO(r.content), caption=body, parse_mode='HTML', reply_markup=keyboard(item, done))
                return
        except Exception:
            LOG.warning('No se pudo enviar portada %s', info['image'], exc_info=True)
    await app.bot.send_message(chat_id, body, parse_mode='HTML', reply_markup=keyboard(item, done), disable_web_page_preview=True)

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
        items = await asyncio.to_thread(refresh) if refresh_first else parse_latest()
        if not items:
            await app.bot.send_message(chat_id, 'No se han identificado películas en /ultimos. Comprueba si ha cambiado el HTML.')
            return
        await app.bot.send_message(chat_id, f'🎬 Novedades de películas: {len(items)} fichas. Pulsa Descargar en las que quieras añadir a qBittorrent.')
        for item in items:
            await send_card(app, chat_id, item)
            await asyncio.sleep(0.12)
    except Exception as exc:
        LOG.exception('Error al consultar novedades')
        await app.bot.send_message(chat_id, f'Error al consultar novedades: {str(exc)[:300]}')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    with conn() as c:
        c.execute('INSERT OR IGNORE INTO chats(chat_id) VALUES(?)', (update.effective_chat.id,))
    await update.message.reply_text('Bot activado. Recibirás las fichas de películas cada día a las 07:00 (Europe/Madrid). Usa /novedades para consultarlas ahora.')


async def novedades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    await digest(context.application, update.effective_chat.id)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    q = update.callback_query
    if q.data == 'done':
        await q.answer('Ya se añadió desde este chat.')
        return
    if not q.data.startswith('get:'):
        await q.answer('Botón de una versión anterior; usa /novedades.', show_alert=True)
        return
    item_id = q.data[4:]
    with conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute('SELECT * FROM items WHERE id=? AND kind=?', (item_id, 'movies')).fetchone()
        done = bool(c.execute('SELECT 1 FROM downloaded WHERE chat_id=? AND id=?', (q.message.chat_id, item_id)).fetchone())
    if not row:
        await q.answer('Película no encontrada. Actualiza /novedades.', show_alert=True)
        return
    if done:
        await q.answer('Ya fue añadida.', show_alert=True)
        return
    await q.answer('Enviando a qBittorrent…')
    try:
        await asyncio.to_thread(submit, dict(row))
        with conn() as c:
            c.execute('INSERT OR IGNORE INTO downloaded(chat_id,id) VALUES(?,?)', (q.message.chat_id, item_id))
        await q.edit_message_reply_markup(reply_markup=keyboard(dict(row), True))
        await q.message.reply_text('✅ Añadida a qBittorrent (movies): ' + row['title'][:140])
    except Exception as exc:
        LOG.exception('Error descargando %s', row['title'])
        await q.message.reply_text('❌ No se pudo añadir: ' + str(exc)[:250])

async def daily(context: ContextTypes.DEFAULT_TYPE):
    with conn() as c:
        chats = [r[0] for r in c.execute('SELECT chat_id FROM chats')]
    for chat in chats:
        await digest(context.application, chat, refresh_first=True)


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
