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
    c.execute('CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, title TEXT, kind TEXT, url TEXT, torrent_url TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS detail_cache (id TEXT PRIMARY KEY, torrent_url TEXT, updated_at TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS selections (chat_id INTEGER, id TEXT, PRIMARY KEY(chat_id,id))')
    c.execute('CREATE TABLE IF NOT EXISTS downloaded (chat_id INTEGER, id TEXT, PRIMARY KEY(chat_id,id))')
    c.execute('CREATE TABLE IF NOT EXISTS series_groups (id TEXT PRIMARY KEY, title TEXT, season INTEGER, quality TEXT, url TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS series_episodes (id TEXT PRIMARY KEY, group_id TEXT, episode TEXT, published TEXT, content_id TEXT, tabla TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS series_selections (chat_id INTEGER, group_id TEXT, episode_id TEXT, PRIMARY KEY(chat_id,group_id,episode_id))')
    c.execute('CREATE TABLE IF NOT EXISTS series_downloaded (chat_id INTEGER, episode_id TEXT, PRIMARY KEY(chat_id,episode_id))')
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
    q = unicodedata.normalize('NFKD', str(value or '')).casefold()
    q = re.sub(r'[^a-z0-9]', '', q)
    for quality, priority in QUALITY_ORDER.items():
        if re.sub(r'[^a-z0-9]', '', quality) in q:
            return priority
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


async def send_card(app, chat_id, item, info=None):
    try:
        if info is None:
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
    # qbittorrent-api >= 2025 devuelve TorrentsAddedMetadata en vez de 'Ok.'.
    if hasattr(result, 'success_count'):
        if result.success_count < 1 or result.failure_count > 0:
            raise RuntimeError(f'qBittorrent no confirmó la descarga: {result}')
    elif str(result).strip().lower() not in ('ok.', 'ok', 'true'):
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


async def digest_movies(app, chat_id, refresh_first=True):
    try:
        items = await asyncio.to_thread(refresh) if refresh_first else parse_latest()
        if not items:
            await app.bot.send_message(chat_id, 'No hay películas publicadas ayer en /ultimos (o no se pudo identificar su fecha).')
            return
        # El formato fiable está en la ficha, no siempre en /ultimos.
        # Consultar primero todas las fichas para ordenar por calidad real.
        cards = []
        for item in items:
            try:
                info = await asyncio.to_thread(detail, item)
            except Exception:
                LOG.exception('No se pudo leer la ficha %s', item['url'])
                info = {'title': item['title'], 'description': '', 'genres': '', 'quality': item.get('quality', ''), 'image': ''}
            cards.append((item, info))
        cards.sort(key=lambda pair: (
            quality_rank(pair[1].get('quality') or pair[0].get('quality') or pair[0]['title']),
            alphabetical_key(pair[1].get('title') or pair[0]['title']),
            pair[0]['url'],
        ))
        await app.bot.send_message(chat_id, f'🎬 Películas publicadas ayer ({(datetime.now(TZ).date() - timedelta(days=1)).strftime("%d/%m/%Y")}): {len(cards)} fichas. Orden: calidad descendente y título alfabético.')
        for item, info in cards:
            await send_card(app, chat_id, item, info)
            await asyncio.sleep(0.12)
    except Exception as exc:
        LOG.exception('Error al consultar novedades')
        await app.bot.send_message(chat_id, f'Error al consultar novedades: {str(exc)[:300]}')

async def digest_series(app, chat_id):
    try:
        prepared = await asyncio.to_thread(refresh_series)
        date = (datetime.now(TZ).date() - timedelta(days=1)).strftime('%d/%m/%Y')
        if not prepared:
            await app.bot.send_message(chat_id, f'📺 No se encontraron series publicadas ayer ({date}).')
            return
        await app.bot.send_message(chat_id, f'📺 Series con novedades del {date}: {len(prepared)} fichas (serie + temporada + calidad).')
        for group, meta in prepared:
            await send_series_card(app, chat_id, group, meta)
            await asyncio.sleep(0.12)
    except Exception as exc:
        LOG.exception('Error al consultar series')
        await app.bot.send_message(chat_id, f'❌ Error al consultar series: {str(exc)[:250]}')


async def digest_all(app, chat_id):
    # Los errores de películas no impiden el envío de las series.
    await digest_movies(app, chat_id)
    await digest_series(app, chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    chat_id = update.effective_chat.id
    with conn() as c:
        is_new = c.execute('SELECT 1 FROM chats WHERE chat_id=?', (chat_id,)).fetchone() is None
        c.execute('INSERT OR IGNORE INTO chats(chat_id) VALUES(?)', (chat_id,))
    if is_new:
        await update.message.reply_text(
            '👋 ¡Bienvenido a Telegram NAS Manager!\n'
            '🎬 /peliculas: películas publicadas ayer\n'
            '📺 /series: series publicadas ayer\n'
            '🆕 /novedades: películas y series\n'
            '⏰ Recibirás las novedades todos los días a las 07:00 (Europe/Madrid).\n'
            '🚀 Te envío las novedades disponibles ahora mismo.')
        await digest_all(context.application, chat_id)
    else:
        await update.message.reply_text('✅ Ya estás registrado. Usa /novedades, /peliculas o /series. El envío diario es a las 07:00.')


async def novedades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    await update.message.reply_text('🆕 Consultando películas y series publicadas ayer…')
    await digest_all(context.application, update.effective_chat.id)


async def peliculas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    await update.message.reply_text('🎬 Buscando películas publicadas ayer…')
    await digest_movies(context.application, update.effective_chat.id)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    q = update.callback_query
    if q.data == 'noop':
        await q.answer()
        return
    if q.data.startswith(('tvopen:', 'tvpage:', 'tvtoggle:', 'tvall:', 'tvclear:', 'tvdownload:')):
        return await series_callback(q)
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
        try:
            await digest_all(context.application, chat)
        except Exception:
            LOG.exception('Error en resumen diario para chat %s', chat)


async def startup_digest(context: ContextTypes.DEFAULT_TYPE):
    # En cada arranque del contenedor se envían novedades, sin esperar a las 07:00.
    # El saludo solo se manda una vez por chat, persistiendo en SQLite.
    with conn() as c:
        chats = [r[0] for r in c.execute('SELECT chat_id FROM chats')]
    for chat in chats:
        try:
            key = f'welcome_sent:{chat}'
            with conn() as c:
                welcomed = c.execute('SELECT 1 FROM bot_meta WHERE key=?', (key,)).fetchone() is not None
            if not welcomed:
                await context.application.bot.send_message(
                    chat, '👋 ¡Telegram NAS Manager está en marcha!\n'
                          '🆕 Te envío las novedades de películas y series.\n'
                          '⏰ El próximo resumen automático será a las 07:00 (Europe/Madrid).')
                with conn() as c:
                    c.execute('INSERT OR IGNORE INTO bot_meta(key,value) VALUES(?,?)', (key, '1'))
            await digest_all(context.application, chat)
        except Exception:
            LOG.exception('Error en el envío de arranque para chat %s', chat)


async def post_init(app):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with conn():
        pass
    app.job_queue.run_daily(
        daily,
        time=time(int(os.getenv('DAILY_HOUR','7')), int(os.getenv('DAILY_MINUTE','0')), tzinfo=TZ),
        name='daily-digest',
    )
    app.job_queue.run_once(startup_digest, when=3, name='startup-digest')
    await app.bot.set_my_commands([
        ('start','Registrar chat y recibir novedades'),
        ('novedades','Películas y series de ayer'),
        ('peliculas','Solo películas de ayer'),
        ('series','Solo series de ayer'),
    ])


# --- Series: una ficha por URL (serie + temporada + calidad) ---
def parse_latest_series():
    soup = get_html(SOURCE)
    yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
    groups = {}
    for a in soup.select('a[href]'):
        url = urljoin(SOURCE, a.get('href', ''))
        p = urlparse(url)
        if p.scheme != 'https' or p.hostname != DOMAIN or not re.fullmatch(r'/serie/\d+/\d+/[^/]+/?', p.path, re.I):
            continue
        title = a.get_text(' ', strip=True)
        if not title or len(title) > 180:
            continue
        row = a.find_parent(['li', 'tr', 'p']) or a.parent
        row_text = row.get_text(' ', strip=True) if row else ''
        prefix = row_text.split(title, 1)[0] if title in row_text else ''
        dates = re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', prefix)
        if not dates or dates[-1] != yesterday:
            continue
        group_id = hashlib.sha256(('tv:' + url).encode()).hexdigest()[:16]
        group = groups.setdefault(url, {'id': group_id, 'title': title.split(':', 1)[0].strip(), 'url': url, 'new_episodes': set()})
        match = re.search(r'(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)', title, re.I)
        if match:
            group['new_episodes'].add(f'{int(match.group(1))}x{int(match.group(2)):02d}')
    return list(groups.values())[:MAX_ITEMS]


def series_metadata(soup, group):
    heading = text_of(soup, ['h1', 'meta[property="og:title"]']) or group['title']
    season_match = re.search(r'(\d+)\s*[ªºa]?\s*temporada', heading, re.I)
    season = int(season_match.group(1)) if season_match else 0
    series_name = re.split(r'\s*[-–]\s*\d+\s*[ªºa]?\s*temporada', heading, maxsplit=1, flags=re.I)[0].strip()
    # La calidad real de la ficha tiene prioridad; si falta, leer la etiqueta del título.
    quality = labeled_field(soup, 'Formato')
    if not quality:
        bracket = re.search(r'\[([^]]+)\]', heading)
        quality = bracket.group(1) if bracket else ''
    description = labeled_field(soup, 'Descripción')
    image = ''
    for node in soup.select('meta[property="og:image"], img[alt], .poster img, .cover img'):
        raw = node.get('content') or node.get('data-src') or node.get('src')
        if not raw:
            continue
        url = urljoin(group['url'], raw)
        if urlparse(url).scheme == 'https' and urlparse(url).hostname in (DOMAIN, 'images.weserv.nl'):
            image = url
            break
    episodes = []
    for tr in soup.select('tr'):
        text = tr.get_text(' ', strip=True)
        match = re.search(r'(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)', text, re.I)
        if not match:
            continue
        button = tr.select_one('.protected-download[data-content-id][data-tabla]')
        if button is None:
            continue
        content_id = button.get('data-content-id', '')
        tabla = button.get('data-tabla', '')
        if not content_id.isdigit() or tabla not in ('series', 'serie', 'episodios', 'series_hd', 'series_4k'):
            # Keep the original table identifier if it is a safe simple string;
            # the site may use a different name for series variants.
            if not content_id.isdigit() or not re.fullmatch(r'[a-z_]{1,40}', tabla):
                continue
        ep = f'{int(match.group(1))}x{int(match.group(2)):02d}'
        date_match = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', text)
        date = date_match.group(1) if date_match else ''
        episode_id = hashlib.sha256(f'{group["url"]}:{content_id}:{tabla}'.encode()).hexdigest()[:20]
        episodes.append({'id': episode_id, 'episode': ep, 'date': date, 'content_id': content_id, 'tabla': tabla})
    # Avoid repeated entries in the table and sort naturally.
    episodes = list({e['id']: e for e in episodes}.values())
    episodes.sort(key=lambda e: tuple(map(int, e['episode'].split('x'))))
    return {'name': series_name, 'season': season, 'quality': quality or 'Sin especificar',
            'description': description, 'image': image, 'episodes': episodes}


def refresh_series():
    groups = parse_latest_series()
    prepared = []
    for group in groups:
        try:
            meta = series_metadata(get_html(group['url']), group)
            if not meta['episodes']:
                LOG.warning('Sin botones de episodios detectados: %s', group['url'])
                continue
            prepared.append((group, meta))
        except Exception:
            LOG.exception('Error leyendo episodios de %s', group['url'])
    # Orden de publicación en Telegram: calidad descendente, nombre A-Z y temporada ascendente.
    # Se ordena después de leer las fichas y antes del envío secuencial.
    prepared.sort(key=lambda pair: (
        quality_rank(pair[1]['quality']),
        alphabetical_key(pair[1]['name']),
        int(pair[1]['season']),
        pair[0]['url'],
    ))
    with conn() as c:
        for group, meta in prepared:
            c.execute('INSERT INTO series_groups(id,title,season,quality,url) VALUES(?,?,?,?,?) '
                      'ON CONFLICT(id) DO UPDATE SET title=excluded.title,season=excluded.season,quality=excluded.quality,url=excluded.url',
                      (group['id'], meta['name'], meta['season'], meta['quality'], group['url']))
            for ep in meta['episodes']:
                c.execute('INSERT INTO series_episodes(id,group_id,episode,published,content_id,tabla) VALUES(?,?,?,?,?,?) '
                          'ON CONFLICT(id) DO UPDATE SET episode=excluded.episode,published=excluded.published,content_id=excluded.content_id,tabla=excluded.tabla',
                          (ep['id'], group['id'], ep['episode'], ep['date'], ep['content_id'], ep['tabla']))
    return prepared


def series_caption(group, meta):
    new = {ep['episode'] for ep in meta['episodes'] if ep['episode'] in group['new_episodes']}
    lines = [f'📺 <b>{escape(meta["name"][:150])}</b>',
             f'📚 <b>Temporada:</b> {meta["season"] or "Sin identificar"}',
             f'🎞 <b>Formato:</b> {escape(meta["quality"][:80])}',
             f'📦 <b>Capítulos:</b> {len(meta["episodes"])} · 🆕 {len(new)} publicados ayer']
    if meta['description']:
        lines.append('\n📖 ' + escape(meta['description'][:300]))
    lines.append('\n<b>Episodios (fecha de publicación):</b>')
    for ep in meta['episodes'][:18]:
        lines.append(f'{"🆕 " if ep["episode"] in new else "• "}{ep["episode"]} — {ep["date"] or "Sin fecha"}')
    if len(meta['episodes']) > 18:
        lines.append(f'… y {len(meta["episodes"])-18} más en el selector')
    lines.append('\n<a href="' + escape(group['url'], quote=True) + '">Ver ficha en DonTorrent</a>')
    return '\n'.join(lines)[:1000]


def series_keyboard(group_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton('📥 Seleccionar capítulos', callback_data='tvopen:' + group_id)]])


async def send_series_card(app, chat_id, group, meta):
    body = series_caption(group, meta)
    if meta['image']:
        try:
            import io
            def get_image():
                r = SESSION.get(meta['image'], timeout=20, allow_redirects=False)
                r.raise_for_status()
                if not r.headers.get('content-type', '').lower().startswith('image/') or len(r.content) > 9_000_000:
                    raise ValueError('Imagen inválida')
                return r.content
            content = await asyncio.to_thread(get_image)
            await app.bot.send_photo(chat_id, photo=io.BytesIO(content), caption=body,
                                     parse_mode='HTML', reply_markup=series_keyboard(group['id']))
            return
        except Exception:
            LOG.warning('No se pudo enviar portada de serie %s', group['url'], exc_info=True)
    await app.bot.send_message(chat_id, body, parse_mode='HTML',
                               reply_markup=series_keyboard(group['id']), disable_web_page_preview=True)


async def series_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not permitted(update):
        return await deny(update)
    await update.message.reply_text('📺 Buscando series publicadas ayer y leyendo sus capítulos…')
    await digest_series(context.application, update.effective_chat.id)


def episode_picker(chat_id, group_id, page=0):
    with conn() as c:
        c.row_factory = sqlite3.Row
        group = c.execute('SELECT * FROM series_groups WHERE id=?', (group_id,)).fetchone()
        if not group:
            return None
        episodes = c.execute('SELECT * FROM series_episodes WHERE group_id=? ORDER BY episode', (group_id,)).fetchall()
        selected = {r[0] for r in c.execute('SELECT episode_id FROM series_selections WHERE chat_id=? AND group_id=?', (chat_id, group_id))}
        done = {r[0] for r in c.execute('SELECT episode_id FROM series_downloaded WHERE chat_id=?', (chat_id,))}
    page = max(0, min(page, max(0, (len(episodes)-1)//8)))
    rows = []
    for ep in episodes[page*8:(page+1)*8]:
        prefix = '✅' if ep['id'] in done else '☑️' if ep['id'] in selected else '☐'
        label = f'{prefix} {ep["episode"]} · {ep["published"] or "Sin fecha"}'
        rows.append([InlineKeyboardButton(label, callback_data=f'tvtoggle:{group_id}:{ep["id"]}:{page}')])
    rows.append([InlineKeyboardButton('◀', callback_data=f'tvpage:{group_id}:{max(0,page-1)}'),
                 InlineKeyboardButton(f'{page+1}/{max(1,(len(episodes)+7)//8)}', callback_data='noop'),
                 InlineKeyboardButton('▶', callback_data=f'tvpage:{group_id}:{min((len(episodes)-1)//8,page+1)}')])
    rows.append([InlineKeyboardButton('☑️ Seleccionar todos los pendientes', callback_data=f'tvall:{group_id}')])
    rows.append([InlineKeyboardButton(f'📥 Descargar seleccionados ({len(selected)})', callback_data=f'tvdownload:{group_id}')])
    rows.append([InlineKeyboardButton('🗑 Limpiar selección', callback_data=f'tvclear:{group_id}')])
    title = f'📺 {group["title"]} · T{group["season"]} · {group["quality"]}\nSelecciona capítulos (los marcados ✅ ya fueron añadidos):'
    return title, InlineKeyboardMarkup(rows)


def find_episode_torrent(ep, group_url):
    # Reuse the same PoW flow as films, with the unique button ID for this episode.
    with conn() as c:
        cached = c.execute('SELECT torrent_url FROM detail_cache WHERE id=?', (ep['id'],)).fetchone()
    if cached and cached[0]:
        return safe_url(cached[0])
    generated = pow_request({'action': 'generate', 'content_id': int(ep['content_id']), 'tabla': ep['tabla']})
    nonce = pow_nonce(generated.get('challenge'))
    validated = pow_request({'action': 'validate', 'challenge': generated['challenge'], 'nonce': nonce})
    url = validated.get('download_url')
    if not isinstance(url, str) or not url:
        raise ValueError('DonTorrent no devolvió enlace para el episodio')
    url = safe_url(urljoin(group_url, url))
    if not urlparse(url).path.lower().endswith('.torrent'):
        raise ValueError('La descarga del episodio no es un archivo .torrent')
    cache_torrent(ep['id'], url)
    return url


def submit_episode(ep, group_url):
    url = find_episode_torrent(ep, group_url)
    content = torrent_bytes(url)
    qb = qbittorrentapi.Client(host=os.environ['QBIT_URL'], username=os.environ['QBIT_USER'],
                               password=os.environ['QBIT_PASSWORD'], REQUESTS_ARGS={'timeout': 20})
    qb.auth_log_in()
    if 'tv' not in qb.torrents_categories():
        raise ValueError('No existe la categoría tv en qBittorrent')
    result = qb.torrents_add(torrent_files=content, category='tv')
    if hasattr(result, 'success_count'):
        if result.success_count < 1 or result.failure_count > 0:
            raise RuntimeError(f'qBittorrent no confirmó el episodio: {result}')
    elif str(result).strip().lower() not in ('ok.', 'ok', 'true'):
        raise RuntimeError(f'qBittorrent respondió: {result}')


SERIES_BUSY = set()

async def series_callback(q):
    data = q.data
    chat_id = q.message.chat_id
    if data.startswith('tvopen:') or data.startswith('tvpage:'):
        parts = data.split(':')
        group_id = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        picker = episode_picker(chat_id, group_id, page)
        if not picker:
            await q.answer('Ficha no encontrada; ejecuta /series', show_alert=True)
            return
        await q.answer()
        if data.startswith('tvopen:'):
            await q.message.reply_text(picker[0], reply_markup=picker[1])
        else:
            await q.edit_message_text(picker[0], reply_markup=picker[1])
        return
    parts = data.split(':')
    action, group_id = parts[:2]
    if action == 'tvtoggle':
        episode_id, page = parts[2], int(parts[3])
        with conn() as c:
            exists = c.execute('SELECT 1 FROM series_episodes WHERE id=? AND group_id=?', (episode_id, group_id)).fetchone()
            if not exists:
                await q.answer('Episodio no encontrado', show_alert=True)
                return
            already = c.execute('SELECT 1 FROM series_downloaded WHERE chat_id=? AND episode_id=?', (chat_id, episode_id)).fetchone()
            if already:
                await q.answer('Este capítulo ya se añadió', show_alert=True)
                return
            chosen = c.execute('SELECT 1 FROM series_selections WHERE chat_id=? AND group_id=? AND episode_id=?', (chat_id, group_id, episode_id)).fetchone()
            if chosen:
                c.execute('DELETE FROM series_selections WHERE chat_id=? AND group_id=? AND episode_id=?', (chat_id, group_id, episode_id))
            else:
                c.execute('INSERT OR IGNORE INTO series_selections VALUES(?,?,?)', (chat_id, group_id, episode_id))
    elif action in ('tvall', 'tvclear'):
        with conn() as c:
            c.execute('DELETE FROM series_selections WHERE chat_id=? AND group_id=?', (chat_id, group_id))
            if action == 'tvall':
                c.execute('INSERT OR IGNORE INTO series_selections(chat_id,group_id,episode_id) '
                          'SELECT ?,group_id,id FROM series_episodes WHERE group_id=? AND id NOT IN '
                          '(SELECT episode_id FROM series_downloaded WHERE chat_id=?)', (chat_id, group_id, chat_id))
        page = 0
    elif action == 'tvdownload':
        key = (chat_id, group_id)
        if key in SERIES_BUSY:
            await q.answer('Ya hay una descarga en curso para esta ficha', show_alert=True)
            return
        with conn() as c:
            c.row_factory = sqlite3.Row
            group = c.execute('SELECT * FROM series_groups WHERE id=?', (group_id,)).fetchone()
            selected = c.execute('SELECT e.* FROM series_episodes e JOIN series_selections s ON e.id=s.episode_id '
                                 'WHERE s.chat_id=? AND s.group_id=? ORDER BY e.episode', (chat_id, group_id)).fetchall()
        if not group or not selected:
            await q.answer('Selecciona primero algún episodio', show_alert=True)
            return
        SERIES_BUSY.add(key)
        await q.answer('Añadiendo capítulos a qBittorrent…')
        await q.message.reply_text(f'⏳ Añadiendo {len(selected)} capítulo(s) a qBittorrent…')
        results = []
        try:
            for ep in selected:
                try:
                    await asyncio.to_thread(submit_episode, dict(ep), group['url'])
                    with conn() as c:
                        c.execute('INSERT OR IGNORE INTO series_downloaded(chat_id,episode_id) VALUES(?,?)', (chat_id, ep['id']))
                        c.execute('DELETE FROM series_selections WHERE chat_id=? AND group_id=? AND episode_id=?', (chat_id, group_id, ep['id']))
                    results.append(f'✅ {ep["episode"]} — Añadido')
                except Exception as exc:
                    LOG.exception('Error descargando serie %s episodio %s', group['title'], ep['episode'])
                    results.append(f'❌ {ep["episode"]} — {str(exc)[:130]}')
            await q.message.reply_text(f'📺 {group["title"]} · T{group["season"]} · {group["quality"]}\n' + '\n'.join(results) + '\n📁 Categoría: tv')
            picker = episode_picker(chat_id, group_id)
            if picker:
                await q.edit_message_text(picker[0], reply_markup=picker[1])
        finally:
            SERIES_BUSY.discard(key)
        return
    else:
        await q.answer('Acción no reconocida', show_alert=True)
        return
    picker = episode_picker(chat_id, group_id, page if action == 'tvtoggle' else 0)
    await q.answer()
    if picker:
        await q.edit_message_text(picker[0], reply_markup=picker[1])


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('novedades', novedades))
    app.add_handler(CommandHandler('peliculas', peliculas_command))
    app.add_handler(CommandHandler('series', series_command))
    app.add_handler(CallbackQueryHandler(callback))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
