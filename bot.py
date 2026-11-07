"""Telegram digest of recent releases; manually selected torrents -> qBittorrent."""
import asyncio
import hashlib
import json
import logging
import os
import re
import unicodedata
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from html import escape
from zoneinfo import ZoneInfo

import qbittorrentapi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger(__name__)
# httpx logs Telegram API URLs containing the bot token; never log them.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
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
retry = Retry(total=3, connect=3, read=2, backoff_factor=1,
              status_forcelist=[429, 500, 502, 503, 504],
              allowed_methods=frozenset(['GET']), respect_retry_after_header=True)
SESSION.mount('https://', HTTPAdapter(max_retries=retry))
QUALITY_ORDER = {'4k': 1, 'bdremux-1080p': 2, 'bluray-1080p': 3,
                 'microhd-1080p': 4, 'hdtv-1080p': 5, 'hdrip': 6,
                 'hdtv': 7, 'dvdrip': 8}



def conn():
    c = sqlite3.connect(DB)
    c.execute('CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY)')
    c.execute('CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, title TEXT, kind TEXT, url TEXT, torrent_url TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS detail_cache (id TEXT PRIMARY KEY, torrent_url TEXT, updated_at TEXT)')
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


def quality_rank(value):
    q = (value or '').lower().strip()
    q = re.sub(r'[\s_]+', '-', q)
    q = q.replace('bd-remux', 'bdremux').replace('blu-ray', 'bluray')
    # Match explicit formats; avoid confusing 1080p MicroHD with generic 1080p.
    for quality in sorted(QUALITY_ORDER, key=len, reverse=True):
        if quality in q:
            return QUALITY_ORDER[quality]
    return 99


def alphabetical_key(value):
    value = unicodedata.normalize('NFKD', value or '')
    return ''.join(ch for ch in value if not unicodedata.combining(ch)).casefold()


def parse_latest():
    """Only actual movie detail URLs from the releases list, never menu/category links."""
    soup = get_html(SOURCE)
    found, seen = [], set()
    yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
    for a in soup.select('a[href]'):
        href = urljoin(SOURCE, a.get('href', ''))
        p = urlparse(href)
        # DonTorrent movie detail routes are /pelicula/<numeric-id>/<slug>.
        # This intentionally excludes /peliculas, /peliculas-hd, /documentales, etc.
        if p.hostname != DOMAIN or p.scheme != 'https' or not re.fullmatch(r'/pelicula/\d+/[^/]+/?', p.path, re.I):
            continue
        if href in seen:
            continue
        title = a.get_text(' ', strip=True) or (a.find('img').get('alt', '') if a.find('img') else '')
        if not title or len(title) > 180:
            continue
        seen.add(href)
        # The release list has the format immediately after the linked title.
        row = a.find_parent(['li', 'tr', 'p']) or a.parent
        row_text = row.get_text(' ', strip=True) if row else ''
        # The /ultimos list prints the publication date immediately before each title.
        # Never substitute the movie release year or the date of the detail page.
        prefix = row_text.split(title, 1)[0] if title in row_text else ''
        dates = re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', prefix)
        if not dates or dates[-1] != yesterday:
            continue
        tail = row_text.split(title, 1)[-1][:100] if title in row_text else '' 
        quality_match = re.search(r'\((4K|BDremux-1080p|BluRay-1080p|MicroHD-1080p|HDTV-1080p|HDRip|HDTV|DVDRip)\)', tail, re.I)
        quality = quality_match.group(1) if quality_match else (re.search(r'\[(4K)\]', title, re.I).group(1) if re.search(r'\[(4K)\]', title, re.I) else '')
        found.append({'id': hashlib.sha256(href.encode()).hexdigest()[:16], 'title': title, 'kind': 'movies', 'url': href, 'quality': quality})
    # Quality descending, title alphabetically ascending within each quality.
    found.sort(key=lambda item: (quality_rank(item['quality'] or item['title']), alphabetical_key(item['title']), item['url']))
    return found[:MAX_ITEMS]


def text_of(soup, selectors):
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get('content', '') if node.name == 'meta' else node.get_text(' ', strip=True)
            if value:
                return value.strip()
    return ''


def labeled_field(soup, label):
    # Fields on movie pages are rendered as "Género: Acción - Drama" etc.
    text = soup.get_text('\n', strip=True)
    match = re.search(r'(?im)^' + re.escape(label) + r'\s*:\s*([^\n]{0,180})', text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    match = re.search(r'(?im)^' + re.escape(label) + r'\s*:\s*\n([^\n]{1,180})', text)
    return match.group(1).strip() if match else ''


def detail(item):
    soup = get_html(item['url'])
    title = text_of(soup, ['h1', 'meta[property="og:title"]']) or item['title']
    desc = labeled_field(soup, 'Descripción') or text_of(soup, ['.sinopsis', '.synopsis', '.descripcion', '.description', '[itemprop="description"]'])
    genres = labeled_field(soup, 'Género') or text_of(soup, ['.generos', '.genres', '.genre', '[itemprop="genre"]'])
    quality = labeled_field(soup, 'Formato') or item.get('quality', '')
    if not quality:
        m = re.search(r'\b(2160p|1080p|720p|4k|bluray|web[- .]?dl|hdtv|remux)\b', item['title'], re.I)
        quality = m.group(0) if m else ''
    image = ''
    # DonTorrent commonly serves poster images through images.weserv.nl,
    # which the previous same-domain-only check incorrectly rejected.
    for selector in ['meta[property="og:image"]', 'img[alt]', '.poster img', '.cover img', '.caratula img', 'article img', 'main img']:
        for node in soup.select(selector):
            alt = (node.get('alt') or '').lower()
            if selector == 'img[alt]' and not (title.lower()[:12] in alt or alt[:12] in title.lower() and len(alt) > 5):
                continue
            raw = node.get('content') or node.get('data-src') or node.get('data-original') or node.get('src')
            if not raw:
                continue
            candidate = urljoin(item['url'], raw)
            parsed = urlparse(candidate)
            if parsed.scheme == 'https' and parsed.hostname in (DOMAIN, 'images.weserv.nl'):
                image = candidate
                break
        if image:
            break
    candidates = extract_torrent_links(soup, item['url'])
    if len(candidates) == 1:
        cache_torrent(item['id'], candidates[0])
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
            r = await asyncio.to_thread(lambda: SESSION.get(info['image'], timeout=20, allow_redirects=False) if urlparse(info['image']).hostname in (DOMAIN, 'images.weserv.nl') else None)
            if r is None:
                raise ValueError('Dominio de imagen no permitido')
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


def extract_torrent_links(soup, page_url):
    candidates = set()
    for tag in soup.select('a[href], [data-url], [data-href]'):
        raw = tag.get('href') or tag.get('data-url') or tag.get('data-href')
        if not raw:
            continue
        link = urljoin(page_url, raw)
        if urlparse(link).path.lower().endswith('.torrent'):
            candidates.add(safe_url(link))
    return sorted(candidates)


def cache_torrent(item_id, url):
    with conn() as c:
        c.execute('INSERT INTO detail_cache(id,torrent_url,updated_at) VALUES(?,?,?) '
                  'ON CONFLICT(id) DO UPDATE SET torrent_url=excluded.torrent_url,updated_at=excluded.updated_at',
                  (item_id, safe_url(url), datetime.now(TZ).isoformat()))
        c.execute('UPDATE items SET torrent_url=? WHERE id=?', (safe_url(url), item_id))


def pow_nonce(challenge, difficulty=3, max_attempts=2_000_000):
    """Implementa el desafío SHA-256 publicado por el propio sitio."""
    if not isinstance(challenge, str) or not challenge or len(challenge) > 2048:
        raise ValueError('Challenge de descarga no válido')
    prefix = '0' * difficulty
    for nonce in range(max_attempts):
        if hashlib.sha256(f'{challenge}{nonce}'.encode('utf-8')).hexdigest().startswith(prefix):
            return nonce
    raise RuntimeError('No se pudo resolver el desafío en el límite previsto')


def pow_request(payload):
    endpoint = f'https://{DOMAIN}/api_validate_pow.php'
    # No seguir redirecciones ni reintentar automáticamente POST: pueden consumir un desafío.
    try:
        response = SESSION.post(endpoint, json=payload, timeout=(10, 35), allow_redirects=False)
    except requests.Timeout as exc:
        raise RuntimeError('DonTorrent no respondió a la validación a tiempo') from exc
    if response.status_code == 429:
        raise RuntimeError('Límite de descargas de DonTorrent alcanzado; inténtalo más tarde')
    if response.status_code != 200:
        raise RuntimeError(f'Validación de DonTorrent: HTTP {response.status_code}')
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError('DonTorrent no devolvió una respuesta JSON válida') from exc
    if not isinstance(result, dict):
        raise RuntimeError('Respuesta de validación inesperada')
    if result.get('status') == 'limit_exceeded':
        raise RuntimeError('Límite de descargas de DonTorrent alcanzado')
    if result.get('status') == 'captcha_required':
        raise RuntimeError('DonTorrent solicita CAPTCHA: abre la ficha en el navegador para completar la verificación')
    if not result.get('success'):
        raise RuntimeError('DonTorrent rechazó la validación: ' + str(result.get('error', 'sin detalles'))[:120])
    return result


def find_torrent(item):
    with conn() as c:
        cached = c.execute('SELECT torrent_url FROM detail_cache WHERE id=?', (item['id'],)).fetchone()
    if cached and cached[0]:
        return safe_url(cached[0])
    if item.get('torrent_url'):
        return safe_url(item['torrent_url'])

    soup = get_html(item['url'])
    candidates = extract_torrent_links(soup, item['url'])
    if len(candidates) == 1:
        cache_torrent(item['id'], candidates[0])
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError('Hay varios enlaces .torrent en la ficha: descarga ambigua')

    button = soup.select_one('.protected-download[data-content-id][data-tabla]')
    if button is None:
        raise ValueError('No se encontró el botón de descarga protegido en la ficha')
    content_id = button.get('data-content-id', '')
    tabla = button.get('data-tabla', '')
    if not content_id.isdigit() or tabla != 'peliculas':
        raise ValueError('Identificador o tipo de contenido no válido')

    generated = pow_request({'action': 'generate', 'content_id': int(content_id), 'tabla': tabla})
    challenge = generated.get('challenge')
    nonce = pow_nonce(challenge)
    validated = pow_request({'action': 'validate', 'challenge': challenge, 'nonce': nonce})
    url = validated.get('download_url')
    if not isinstance(url, str) or not url:
        raise ValueError('La validación no devolvió un enlace de descarga')
    url = safe_url(urljoin(item['url'], url))
    if not urlparse(url).path.lower().endswith('.torrent'):
        raise ValueError('La URL de descarga no corresponde a un archivo .torrent')
    cache_torrent(item['id'], url)
    return url


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
            await app.bot.send_message(chat_id, 'No hay películas publicadas ayer en /ultimos (o no se pudo identificar su fecha).')
            return
        await app.bot.send_message(chat_id, f'🎬 Películas publicadas ayer ({(datetime.now(TZ).date() - timedelta(days=1)).strftime("%d/%m/%Y")}): {len(items)} fichas. Orden: calidad descendente y título alfabético.')
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
