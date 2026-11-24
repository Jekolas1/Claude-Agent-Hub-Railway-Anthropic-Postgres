import base64
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import psycopg
import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv('DATABASE_URL', '')
HUB_TOKEN = os.getenv('HUB_TOKEN', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', '')
# WhatsApp via Whapi (optional: the webhook is disabled until these are set)
WHAPI_TOKEN = os.getenv('WHAPI_TOKEN', '')
WHAPI_URL = os.getenv('WHAPI_URL', 'https://gate.whapi.cloud')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
ALLOWED_NUMBERS = {re.sub(r'\D', '', n) for n in os.getenv('ALLOWED_NUMBERS', '').split(',') if re.sub(r'\D', '', n)}
TZ = ZoneInfo(os.getenv('TZ_NAME', 'Asia/Jerusalem'))

# Knowledge every agent can read. Writing to it requires shared_write on the agent.
SHARED = '_shared'
# Optional capabilities an agent can be given (PUT /agents/{id} -> "tools").
OPTIONAL_TOOLS = {
    'web_search': {'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 10},
    # Runs Python in Anthropic's sandbox (Pillow etc.). Files saved to $OUTPUT_DIR come back to us.
    'code_execution': {'type': 'code_execution_20250825', 'name': 'code_execution'},
}
# Optional tools that run on this server (client tools), enabled per agent like OPTIONAL_TOOLS.
SERVER_SIDE_TOOLS = {'fetch_image', 'remove_background'}
MAX_VALUE = 30000
MAX_FILE = 20 * 1024 * 1024
IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}


def db():
    return psycopg.connect(DATABASE_URL, autocommit=False)


def init_db():
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )''')
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tools JSONB NOT NULL DEFAULT '[]'")
        conn.execute('ALTER TABLE agents ADD COLUMN IF NOT EXISTS shared_write BOOLEAN NOT NULL DEFAULT false')
        conn.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS names JSONB NOT NULL DEFAULT '[]'")
        conn.execute('ALTER TABLE agents ADD COLUMN IF NOT EXISTS model TEXT')  # NULL = CLAUDE_MODEL
        conn.execute('''CREATE TABLE IF NOT EXISTS wa_sessions (
            chat_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS wa_seen (
            message_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS knowledge (
            agent_id TEXT NOT NULL REFERENCES agents(id),
            key TEXT NOT NULL, value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(agent_id, key)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
            conversation_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_msgs ON messages(agent_id,conversation_id,id DESC)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS files (
            agent_id TEXT NOT NULL REFERENCES agents(id),
            key TEXT NOT NULL, mime TEXT NOT NULL, data BYTEA NOT NULL,
            anthropic_file_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(agent_id, key)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS schedules (
            agent_id TEXT PRIMARY KEY REFERENCES agents(id), at_time TEXT NOT NULL,
            message TEXT NOT NULL, notify TEXT, last_run DATE
        )''')
        conn.execute('ALTER TABLE schedules ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ')
        conn.execute('ALTER TABLE schedules ADD COLUMN IF NOT EXISTS finished DATE')
        conn.execute('ALTER TABLE schedules ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0')
        conn.execute('''CREATE TABLE IF NOT EXISTS containers (
            agent_id TEXT NOT NULL, conversation_id TEXT NOT NULL, container_id TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(agent_id, conversation_id)
        )''')
        conn.execute('''INSERT INTO agents (id,instructions) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING''',
                     (SHARED, 'Shared knowledge namespace. Not a chat agent.'))
        for owner, key, mime in conn.execute('SELECT agent_id,key,mime FROM files').fetchall():
            fixed = clean_mime(mime, key)
            if fixed != mime:
                conn.execute('UPDATE files SET mime=%s WHERE agent_id=%s AND key=%s', (fixed, owner, key))
                print(f'files: fixed type of {key}: {mime[:60]!r} -> {fixed}', flush=True)


@asynccontextmanager
async def lifespan(app):
    for name, value in [('DATABASE_URL', DATABASE_URL), ('HUB_TOKEN', HUB_TOKEN), ('ANTHROPIC_API_KEY', ANTHROPIC_API_KEY), ('CLAUDE_MODEL', CLAUDE_MODEL)]:
        if not value:
            raise RuntimeError(f'Missing required environment variable: {name}')
    for attempt in range(12):
        try:
            init_db()
            break
        except psycopg.OperationalError:
            if attempt == 11:
                raise
            time.sleep(2)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title='Claude Agent Hub', lifespan=lifespan, docs_url=None, redoc_url=None)


def authorize(auth: str | None):
    expected = f'Bearer {HUB_TOKEN}'
    if not auth or not secrets.compare_digest(auth, expected):
        raise HTTPException(401, 'Unauthorized')


def valid_agent_id(agent_id: str) -> bool:
    return agent_id.replace('-', '').replace('_', '').isalnum() and len(agent_id) <= 64


def normalize_key(key: str) -> str:
    key = str(key).strip().lstrip('/')
    if key.startswith('flai-work/'):
        key = key[len('flai-work/'):]
    return key[:200]


class AgentConfig(BaseModel):
    instructions: str = Field(min_length=10, max_length=60000)
    tools: list[str] = Field(default_factory=list)
    shared_write: bool = False
    names: list[str] = Field(default_factory=list)  # names to call the agent by on WhatsApp, e.g. ["גרשון"]
    # Claude model for this agent, e.g. "claude-haiku-5-5". Empty = the CLAUDE_MODEL variable.
    model: str | None = Field(default=None, pattern=r'^$|^claude-[a-z0-9.-]+$', max_length=80)


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=16000)


@app.get('/health')
def health():
    try:
        with db() as conn:
            conn.execute('SELECT 1')
        return {'status': 'ok', 'database': 'connected'}
    except Exception:
        raise HTTPException(503, 'Database unavailable')


@app.get('/agents')
def list_agents(authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        rows = conn.execute('SELECT id,tools,shared_write,names,model FROM agents WHERE id<>%s ORDER BY id', (SHARED,)).fetchall()
    return {'agents': [{'id': i, 'tools': t, 'shared_write': w, 'names': n, 'model': m or CLAUDE_MODEL}
                       for i, t, w, n, m in rows]}


@app.put('/agents/{agent_id}')
def upsert_agent(agent_id: str, request: AgentConfig, authorization: str | None = Header(default=None)):
    authorize(authorization)
    if not valid_agent_id(agent_id) or agent_id == SHARED:
        raise HTTPException(400, 'Invalid agent_id')
    unknown = [t for t in request.tools if t not in OPTIONAL_TOOLS and t not in SERVER_SIDE_TOOLS]
    if unknown:
        raise HTTPException(400, f'Unknown tools: {unknown}. Available: {sorted([*OPTIONAL_TOOLS, *SERVER_SIDE_TOOLS])}')
    with db() as conn:
        conn.execute('''INSERT INTO agents (id,instructions,tools,shared_write,names,model) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET instructions=EXCLUDED.instructions,
            tools=EXCLUDED.tools, shared_write=EXCLUDED.shared_write, names=EXCLUDED.names, model=EXCLUDED.model''',
            (agent_id, request.instructions, json.dumps(request.tools), request.shared_write,
             json.dumps(request.names, ensure_ascii=False), request.model or None))
    return {'agent_id': agent_id, 'saved': True, 'tools': request.tools, 'shared_write': request.shared_write,
            'names': request.names, 'model': request.model or CLAUDE_MODEL}


@app.get('/agents/{agent_id}/knowledge')
def get_knowledge(agent_id: str, authorization: str | None = Header(default=None)):
    """agent_id may be '_shared' for the knowledge all agents can read."""
    authorize(authorization)
    with db() as conn:
        results = conn.execute('SELECT key,value,updated_at FROM knowledge WHERE agent_id=%s ORDER BY key LIMIT 1000', (agent_id,)).fetchall()
    return {'agent_id': agent_id, 'knowledge': [{'key': k, 'value': v, 'updated_at': t.isoformat()} for k, v, t in results]}


def write_knowledge(conn, agent_id, key, value):
    conn.execute('''INSERT INTO knowledge(agent_id,key,value) VALUES (%s,%s,%s)
        ON CONFLICT(agent_id,key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''', (agent_id, key, value))


@app.put('/agents/{agent_id}/knowledge/{key:path}')
def put_knowledge(agent_id: str, key: str, body: dict, authorization: str | None = Header(default=None)):
    """agent_id may be '_shared'. Keys may contain '/', e.g. clients/kulam.md."""
    authorize(authorization)
    key = normalize_key(key)
    value = body.get('value')
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_VALUE or not key:
        raise HTTPException(400, 'Invalid key or value')
    with db() as conn:
        if not conn.execute('SELECT 1 FROM agents WHERE id=%s', (agent_id,)).fetchone():
            raise HTTPException(404, 'Agent not found')
        write_knowledge(conn, agent_id, key, value)
    return {'saved': True, 'agent_id': agent_id, 'key': key}


def guess_mime(key: str, fallback: str = 'application/octet-stream') -> str:
    return mimetypes.guess_type(key)[0] or fallback


MIME_RE = re.compile(r'^[a-z0-9][\w.+-]*/[\w.+-]+$', re.IGNORECASE)


def clean_mime(mime: str | None, key: str) -> str:
    """A real type/subtype, else a guess from the file name. Generic types also give way to the file name."""
    m = (mime or '').split(';')[0].strip().lower()
    if not MIME_RE.match(m) or m in ('application/x-www-form-urlencoded', 'application/octet-stream', 'text/plain'):
        return guess_mime(key, m if MIME_RE.match(m) else 'application/octet-stream')
    return m


def write_file(conn, agent_id, key, mime, data: bytes):
    conn.execute('''INSERT INTO files(agent_id,key,mime,data) VALUES (%s,%s,%s,%s)
        ON CONFLICT(agent_id,key) DO UPDATE SET mime=EXCLUDED.mime, data=EXCLUDED.data,
        anthropic_file_id=NULL, updated_at=now()''', (agent_id, key, mime, data))


def read_file(conn, agent_id, key):
    """Own file first, then shared. Returns (owner, key, mime, data, anthropic_file_id) or None."""
    return conn.execute('''SELECT agent_id,key,mime,data,anthropic_file_id FROM files
        WHERE agent_id IN (%s,%s) AND key=%s ORDER BY (agent_id=%s) DESC LIMIT 1''',
        (agent_id, SHARED, key, agent_id)).fetchone()


@app.put('/agents/{agent_id}/files/{key:path}')
async def put_file(agent_id: str, key: str, request: Request, authorization: str | None = Header(default=None)):
    """Upload a binary file (logo, image, PDF). agent_id may be '_shared'. Body = raw bytes."""
    authorize(authorization)
    key, data = normalize_key(key), await request.body()
    if not key or not data or len(data) > MAX_FILE:
        raise HTTPException(400, 'Invalid key or file (max 20MB)')
    mime = clean_mime(request.headers.get('content-type'), key)
    with db() as conn:
        if not conn.execute('SELECT 1 FROM agents WHERE id=%s', (agent_id,)).fetchone():
            raise HTTPException(404, 'Agent not found')
        write_file(conn, agent_id, key, mime, data)
    return {'saved': True, 'agent_id': agent_id, 'key': key, 'mime': mime, 'bytes': len(data)}


@app.get('/agents/{agent_id}/files')
def list_files_api(agent_id: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        rows = conn.execute('''SELECT key,mime,length(data),updated_at FROM files WHERE agent_id=%s
            ORDER BY key LIMIT 1000''', (agent_id,)).fetchall()
    return {'agent_id': agent_id, 'files': [{'key': k, 'mime': m, 'bytes': n, 'updated_at': t.isoformat()}
                                            for k, m, n, t in rows]}


@app.get('/agents/{agent_id}/files/{key:path}')
def get_file(agent_id: str, key: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        row = read_file(conn, agent_id, normalize_key(key))
    if not row:
        raise HTTPException(404, 'File not found')
    return Response(content=bytes(row[3]), media_type=row[2])


BASE_TOOLS = [
    {'name': 'list_knowledge',
     'description': 'List knowledge keys (file paths) you can read: your own and the shared ones. Optional prefix filter, e.g. "shared/gefen/" or "clients/".',
     'input_schema': {'type': 'object', 'properties': {'prefix': {'type': 'string'}}}},
    {'name': 'read_knowledge',
     'description': 'Read one knowledge entry by key (a file path such as "Agents/gershon/misses.md" or "clients/kulam.md"). Looks in your own knowledge first, then shared.',
     'input_schema': {'type': 'object', 'properties': {'key': {'type': 'string'}}, 'required': ['key']}},
    {'name': 'save_knowledge',
     'description': 'Create or replace a knowledge entry (a file). Use the file path from your instructions as key. Keys under "Agents/<your id>/" are private to you; any other key is shared with all agents and only allowed if you have shared write permission. Save only stable facts, decisions or work products, never secrets.',
     'input_schema': {'type': 'object', 'properties': {'key': {'type': 'string'}, 'value': {'type': 'string'}}, 'required': ['key', 'value']}},
    {'name': 'search_knowledge',
     'description': 'Search your own and shared knowledge by keyword.',
     'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}},
    {'name': 'list_files',
     'description': 'List stored binary files (images, logos, PDFs, earlier designs): your own and shared. Optional prefix, e.g. "Agents/gershon/logos/".',
     'input_schema': {'type': 'object', 'properties': {'prefix': {'type': 'string'}}}},
    {'name': 'send_file',
     'description': 'Send a stored file (by key from list_files) to the user together with your reply. Use only for files the user should receive.',
     'input_schema': {'type': 'object', 'properties': {'key': {'type': 'string'}}, 'required': ['key']}},
]
FILE_TOOLS_CODE = [
    {'name': 'use_files',
     'description': 'Copy stored files (keys from list_files, e.g. a logo) into your code execution sandbox so your Python code can open them. After this call they are available in the sandbox input directory.',
     'input_schema': {'type': 'object', 'properties': {'keys': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['keys']}},
]


# ---------------- Web images (allowed sites only) and background removal ----------------

WEB_TOOLS = [
    {'name': 'find_images',
     'description': 'List image URLs on a web page (og:image first, then <img> tags). The page must be on one of the allowed sites listed in your current-state.md.',
     'input_schema': {'type': 'object', 'properties': {'page_url': {'type': 'string'}}, 'required': ['page_url']}},
    {'name': 'fetch_image',
     'description': 'Download an image and store it as a file under save_as (a path from your instructions, e.g. "shared/gefen/2026-11-24-gefen-original.jpg"). Allowed only if the image is hosted on an allowed site, or appears on page_url and page_url is on an allowed site.',
     'input_schema': {'type': 'object', 'properties': {'image_url': {'type': 'string'}, 'page_url': {'type': 'string'},
                                                       'save_as': {'type': 'string'}}, 'required': ['image_url', 'save_as']}},
]
BG_TOOLS = [
    {'name': 'remove_background',
     'description': 'Remove the background of a stored image (rembg, model isnet-general-use) and store the result as a PNG with transparency under save_as.',
     'input_schema': {'type': 'object', 'properties': {'key': {'type': 'string'}, 'save_as': {'type': 'string'}},
                      'required': ['key', 'save_as']}},
]
BROWSER_HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'}


def host_of(url: str) -> str:
    h = (urlparse(url).hostname or '').lower()
    return h[4:] if h.startswith('www.') else h


def allowed_hosts(conn, agent_id) -> set[str]:
    row = conn.execute('SELECT value FROM knowledge WHERE agent_id=%s AND key=%s',
                       (agent_id, f'Agents/{agent_id}/current-state.md')).fetchone()
    return {host_of(u) for u in re.findall(r'https?://[^\s)<>"\']+', row[0] if row else '')} - {''}


def is_allowed(url: str, hosts: set[str]) -> bool:
    h = host_of(url)
    return bool(h) and any(h == a or h.endswith('.' + a) for a in hosts)


class _ImgParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and (a.get('property') or a.get('name') or '').lower() in ('og:image', 'og:image:url', 'twitter:image'):
            self.found.insert(0, (a.get('content'), 'og:image'))
        elif tag == 'img':
            src = a.get('src') or a.get('data-src') or a.get('data-original') or (a.get('srcset') or '').split(' ')[0]
            self.found.append((src, a.get('alt') or ''))


def page_images(page_url: str) -> list[dict]:
    r = httpx.get(page_url, timeout=20, follow_redirects=True, headers=BROWSER_HEADERS)
    r.raise_for_status()
    parser = _ImgParser()
    parser.feed(r.text[:3_000_000])
    out, seen = [], set()
    for src, alt in parser.found:
        if not src or src.startswith('data:'):
            continue
        url = urljoin(str(r.url), src.strip())
        if url not in seen:
            seen.add(url)
            out.append({'url': url, 'alt': alt[:100]})
    return out[:40]


_rembg_lock = threading.Lock()
BG_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bg_worker.py')


def remove_bg(data: bytes) -> bytes:
    """Runs rembg in its own process (bg_worker.py). If it runs out of memory only that process dies,
    not the server, so the agent gets an error and finishes the rest of its task."""
    with _rembg_lock:  # one at a time: the model needs a lot of memory
        p = subprocess.run([sys.executable, BG_WORKER], input=data, capture_output=True, timeout=240)
    if p.returncode != 0 or not p.stdout:
        reason = 'killed (probably out of memory)' if p.returncode < 0 else f'exit {p.returncode}'
        raise RuntimeError(f"{reason}: {p.stderr.decode('utf-8', 'replace')[-300:]}")
    return p.stdout


def web_tool(agent, name, args):
    agent_id = agent['id']
    with db() as conn:
        hosts = allowed_hosts(conn, agent_id)
    if name == 'find_images':
        page = str(args.get('page_url', ''))
        if not is_allowed(page, hosts):
            return {'error': f'{host_of(page) or page} is not in the allowed sites list (Agents/{agent_id}/current-state.md).'}
        try:
            return {'page_url': page, 'images': page_images(page)}
        except httpx.HTTPError as e:
            return {'error': f'Could not load page: {type(e).__name__} {e}'[:300]}
    if name == 'fetch_image':
        img, page = str(args.get('image_url', '')), str(args.get('page_url', '') or '')
        save_as = normalize_key(args.get('save_as', ''))
        if not save_as:
            return {'error': 'Missing save_as'}
        if not save_as.startswith(f'Agents/{agent_id}/') and not agent['shared_write']:
            return {'error': f'No permission to write shared key "{save_as}".'}
        ok = is_allowed(img, hosts)
        if not ok and page and is_allowed(page, hosts):
            try:
                ok = img in {i['url'] for i in page_images(page)}
            except httpx.HTTPError:
                ok = False
        if not ok:
            return {'error': 'Not allowed: the image is not hosted on an allowed site and was not found on an allowed page_url.'}
        try:
            r = httpx.get(img, timeout=30, follow_redirects=True, headers={**BROWSER_HEADERS, 'Referer': page or img})
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {'error': f'Download failed: {type(e).__name__}'}
        mime = (r.headers.get('content-type') or '').split(';')[0].strip()
        if not mime.startswith('image/') or not r.content or len(r.content) > 15 * 1024 * 1024:
            return {'error': f'Not an image (content-type {mime or "?"}, {len(r.content)} bytes)'}
        with db() as conn:
            write_file(conn, agent_id if save_as.startswith(f'Agents/{agent_id}/') else SHARED, save_as, mime, r.content)
        return {'saved': True, 'key': save_as, 'mime': mime, 'bytes': len(r.content), 'source': img}
    if name == 'remove_background':
        key, save_as = normalize_key(args.get('key', '')), normalize_key(args.get('save_as', ''))
        if not save_as.lower().endswith('.png'):
            return {'error': 'save_as must end with .png'}
        if not save_as.startswith(f'Agents/{agent_id}/') and not agent['shared_write']:
            return {'error': f'No permission to write shared key "{save_as}".'}
        with db() as conn:
            row = read_file(conn, agent_id, key)
        if not row:
            return {'error': f'File not found: {key}'}
        try:
            out = remove_bg(bytes(row[3]))
        except Exception as e:
            print(f'remove_background failed: {type(e).__name__}: {e}', flush=True)
            return {'error': f'Background removal failed ({type(e).__name__}). Carry on without it and say so in your file.'}
        with db() as conn:
            write_file(conn, agent_id if save_as.startswith(f'Agents/{agent_id}/') else SHARED, save_as, 'image/png', out)
        return {'saved': True, 'key': save_as, 'bytes': len(out), 'model': 'isnet-general-use'}
    return {'error': 'Unknown tool'}


def anthropic_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=170.0)


def upload_to_anthropic(conn, owner, key, mime, data, cached_id=None):
    """Put a stored file into Anthropic's Files API (cached per file) so code execution can read it."""
    if cached_id:
        return cached_id
    meta = anthropic_client().files.upload(file=(key.rsplit('/', 1)[-1], bytes(data), mime))
    conn.execute('UPDATE files SET anthropic_file_id=%s WHERE agent_id=%s AND key=%s', (meta.id, owner, key))
    return meta.id


def call_tool(agent, name, args, ctx=None):
    agent_id, shared_write = agent['id'], agent['shared_write']
    ctx = ctx if ctx is not None else {'outgoing': [], 'uploads': []}
    if name in ('find_images', 'fetch_image', 'remove_background'):
        enabled = {'find_images': 'fetch_image', 'fetch_image': 'fetch_image', 'remove_background': 'remove_background'}[name]
        return web_tool(agent, name, args) if enabled in (agent.get('tools') or []) else {'error': 'Tool not enabled'}
    if name == 'list_files':
        prefix = normalize_key(args.get('prefix', '') or '')
        with db() as conn:
            rows = conn.execute('''SELECT agent_id,key,mime,length(data) FROM files
                WHERE agent_id IN (%s,%s) AND key LIKE %s ORDER BY key LIMIT 300''',
                (agent_id, SHARED, prefix.replace('%', r'\%').replace('_', r'\_') + '%')).fetchall()
        return {'files': [{'key': k, 'mime': m, 'bytes': n, 'scope': 'shared' if a == SHARED else 'private'}
                          for a, k, m, n in rows]}
    if name == 'send_file':
        key = normalize_key(args.get('key', ''))
        with db() as conn:
            row = read_file(conn, agent_id, key)
        if not row:
            return {'error': f'File not found: {key}'}
        ctx['outgoing'].append({'key': row[1], 'mime': row[2], 'filename': row[1].rsplit('/', 1)[-1], 'data': bytes(row[3])})
        return {'queued': True, 'key': key, 'note': 'Will be sent with your reply.'}
    if name == 'use_files':
        done, missing = [], []
        with db() as conn:
            for key in [normalize_key(k) for k in (args.get('keys') or [])][:10]:
                row = read_file(conn, agent_id, key)
                if not row:
                    missing.append(key)
                    continue
                ctx['uploads'].append(upload_to_anthropic(conn, row[0], row[1], row[2], row[3], row[4]))
                done.append(key)
        return {'added_to_sandbox': done, 'missing': missing,
                'note': 'Files arrive in the sandbox with the next turn; list the input directory to find them.'}
    if name == 'list_knowledge':
        prefix = normalize_key(args.get('prefix', '') or '')
        with db() as conn:
            rows = conn.execute('''SELECT agent_id,key,length(value),updated_at FROM knowledge
                WHERE agent_id IN (%s,%s) AND key LIKE %s ORDER BY key LIMIT 300''',
                (agent_id, SHARED, prefix.replace('%', r'\%').replace('_', r'\_') + '%')).fetchall()
        return {'keys': [{'key': k, 'scope': 'shared' if a == SHARED else 'private', 'chars': n,
                          'updated_at': t.isoformat()} for a, k, n, t in rows]}
    if name == 'read_knowledge':
        key = normalize_key(args.get('key', ''))
        with db() as conn:
            row = conn.execute('''SELECT value FROM knowledge WHERE agent_id IN (%s,%s) AND key=%s
                ORDER BY (agent_id=%s) DESC LIMIT 1''', (agent_id, SHARED, key, agent_id)).fetchone()
        return {'key': key, 'value': row[0]} if row else {'error': f'Not found: {key}'}
    if name == 'save_knowledge':
        key, value = normalize_key(args.get('key', '')), str(args.get('value', ''))
        if not key or not value.strip():
            return {'error': 'Missing key or value'}
        if len(value) > MAX_VALUE:
            return {'error': f'Value too long ({len(value)} > {MAX_VALUE} chars)'}
        private = key.startswith(f'Agents/{agent_id}/')
        if not private and not shared_write:
            return {'error': f'No permission to write shared key "{key}". You may only write under Agents/{agent_id}/.'}
        with db() as conn:
            write_knowledge(conn, agent_id if private else SHARED, key, value)
        return {'saved': True, 'key': key, 'scope': 'private' if private else 'shared'}
    if name == 'search_knowledge':
        term = str(args.get('query', ''))[:100]
        with db() as conn:
            rows = conn.execute('''SELECT key,value FROM knowledge
                WHERE agent_id IN (%s,%s) AND (key ILIKE %s OR value ILIKE %s)
                ORDER BY updated_at DESC LIMIT 12''', (agent_id, SHARED, '%' + term + '%', '%' + term + '%')).fetchall()
        return {'results': [{'key': k, 'value': v[:2000]} for k, v in rows]}
    return {'error': 'Unknown tool'}


def has_code(agent):
    return 'code_execution' in (agent.get('tools') or [])


def read_agent(conn, agent_id):
    row = conn.execute('SELECT id,instructions,tools,shared_write,model FROM agents WHERE id=%s', (agent_id,)).fetchone()
    if not row or agent_id == SHARED:
        raise HTTPException(404, 'Agent not found')
    return {'id': row[0], 'instructions': row[1], 'tools': row[2] or [], 'shared_write': row[3],
            'model': row[4] or CLAUDE_MODEL}


def build_system(conn, agent, channel='api'):
    keys = conn.execute('''SELECT agent_id,key FROM knowledge WHERE agent_id IN (%s,%s)
        ORDER BY key LIMIT 300''', (agent['id'], SHARED)).fetchall()
    index = '\n'.join(f"- {k} ({'shared' if a == SHARED else 'private'})" for a, k in keys) or '(empty)'
    files = conn.execute('''SELECT agent_id,key FROM files WHERE agent_id IN (%s,%s)
        ORDER BY key LIMIT 150''', (agent['id'], SHARED)).fetchall()
    files_index = '\n'.join(f"- {k} ({'shared' if a == SHARED else 'private'})" for a, k in files) or '(none)'
    perms = ('You may write shared keys.' if agent['shared_write']
             else f"You may write only under Agents/{agent['id']}/ (shared keys are read-only for you).")
    return (agent['instructions']
            + '\n\n---\nRUNTIME (cloud):\n'
            + 'Your files live in the knowledge store, not on a disk. Wherever your instructions mention a path '
            + 'like flai-work/<path>, use read_knowledge / save_knowledge with key "<path>" '
            + '(e.g. flai-work/clients/kulam.md -> key "clients/kulam.md"). ' + perms + '\n'
            + 'Never claim a tool ran or a file was saved unless the tool result says so. Do not reveal credentials.\n'
            + 'Now: ' + datetime.now(TZ).strftime('%Y-%m-%d %H:%M (%A)') + ' Israel time.\n'
            + ('Channel: WhatsApp with Yotam. Keep replies short and readable on a phone. No markdown tables or '
               'headings; use *bold* sparingly.\n' if channel == 'whatsapp' else '')
            + ('Channel: scheduled run. Nobody is chatting with you live: do the whole task without asking questions, '
               'then end with a short summary of what you did and what is missing.\n'
               'Save your main output file (save_knowledge) as soon as you have its first details, and save it again '
               'as you add more, so a later failure never loses the work. Never leave it to the very end. If a step '
               'fails (an image, background removal, a site), note it in the file and carry on with the rest.\n'
               if channel == 'schedule' else '')
            + 'FILES: images, logos and PDFs are stored separately from knowledge (see list_files). '
            + 'To give the user an existing file, call send_file. '
            + (f"You have code execution (Python with Pillow, matplotlib, etc.; no internet in the sandbox). "
               f"Canva is not connected in the cloud, so build images and documents with code. To use a stored "
               f"file (e.g. a logo) in code, call use_files first. Every file you save at the top level of "
               f"$OUTPUT_DIR is sent to the user with your reply and stored under Agents/{agent['id']}/outputs/ - "
               f"so put only finished deliverables there, and run `ls \"$OUTPUT_DIR\"` to confirm. A file saved anywhere "
               f"else is NOT delivered. Never write lines like '[files sent: ...]' yourself: the server reports deliveries.\n"
               if has_code(agent) else 'You cannot create new files with code.\n')
            + ('Web images: use find_images on a product page from an allowed site, then fetch_image with save_as set to '
               'the file path from your instructions (without flai-work/). This replaces downloading on a computer.\n'
               if 'fetch_image' in agent['tools'] else '')
            + ('Background removal: call remove_background (rembg, isnet-general-use) instead of running rembg yourself.\n'
               if 'remove_background' in agent['tools'] else '')
            + 'KNOWLEDGE INDEX:\n' + index + '\nFILES INDEX:\n' + files_index)


def collect_file_ids(block) -> list[str]:
    """file_ids of files code execution left in $OUTPUT_DIR (they sit inside *code_execution_tool_result blocks)."""
    if 'code_execution_tool_result' not in getattr(block, 'type', ''):
        return []
    found = []
    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get('file_id'), str):
                found.append(x['file_id'])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(block.model_dump())
    return found


def user_content(agent, message, attachments, conn):
    """Text plus attachments: images are shown to Claude; with code execution, every file also goes to the sandbox."""
    if not attachments:
        return message
    notes = ', '.join(a['key'] for a in attachments)
    content = [{'type': 'text', 'text': (message or '(נשלח קובץ בלי טקסט)') + f'\n\n[Attached and stored as: {notes}]'}]
    for a in attachments:
        if a['mime'] in IMAGE_TYPES and len(a['data']) <= 5 * 1024 * 1024:
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': a['mime'],
                                                        'data': base64.b64encode(a['data']).decode()}})
        elif a['mime'] == 'application/pdf' and len(a['data']) <= 20 * 1024 * 1024:
            content.append({'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf',
                                                           'data': base64.b64encode(a['data']).decode()}})
        if has_code(agent):
            content.append({'type': 'container_upload',
                            'file_id': upload_to_anthropic(conn, a['owner'], a['key'], a['mime'], a['data'])})
    return content


# ---------------- Prompt caching ----------------
# Every step of a run resends the tools, the system prompt and the whole conversation so far. With two cache
# breakpoints (end of the system prompt, end of the latest message) Anthropic stores that prefix for 5 minutes and
# later steps pay the cache-read price for it instead of the full input price.
CACHE = {'type': 'ephemeral'}
CACHEABLE_BLOCKS = {'text', 'tool_result', 'image', 'document'}


def cached_system(system: str) -> list:
    return [{'type': 'text', 'text': system, 'cache_control': CACHE}]


def with_cache_breakpoint(messages: list) -> list:
    """Copy of messages with one cache breakpoint on the last block that can carry it (older ones removed:
    the API allows at most 4 breakpoints per request)."""
    out = []
    for m in messages:
        content = m['content']
        if isinstance(content, list):
            content = [{k: v for k, v in b.items() if k != 'cache_control'} if isinstance(b, dict) else b for b in content]
        out.append({'role': m['role'], 'content': content})
    for m in reversed(out):
        if isinstance(m['content'], str):
            if m['content'].strip():
                m['content'] = [{'type': 'text', 'text': m['content'], 'cache_control': CACHE}]
                return out
            continue
        for b in reversed(m['content']):
            if isinstance(b, dict) and b.get('type') in CACHEABLE_BLOCKS and not (b.get('type') == 'text' and not b.get('text')):
                b['cache_control'] = CACHE
                return out
    return out


def usage_note(usage) -> str:
    g = lambda k: getattr(usage, k, 0) or 0
    return (f"in {g('input_tokens')} + cache write {g('cache_creation_input_tokens')} + "
            f"cache read {g('cache_read_input_tokens')}, out {g('output_tokens')}")


def run_agent(agent_id: str, conversation_id: str, message: str, channel: str = 'api',
              attachments: list | None = None) -> dict:
    """Runs one turn. attachments: [{'key','mime','data'}] already stored in the files table for this agent.
    Returns {'text': str, 'files': [{'key','mime','filename','data'}]}."""
    attachments = attachments or []
    client = anthropic_client()
    with db() as conn:
        agent = read_agent(conn, agent_id)
        system = build_system(conn, agent, channel)
        history = conn.execute('''SELECT role,content FROM (SELECT id,role,content FROM messages
            WHERE agent_id=%s AND conversation_id=%s ORDER BY id DESC LIMIT 16) t ORDER BY id''',
            (agent_id, conversation_id)).fetchall()
        row = conn.execute('SELECT container_id FROM containers WHERE agent_id=%s AND conversation_id=%s',
                           (agent_id, conversation_id)).fetchone() if has_code(agent) else None
        try:
            first = user_content(agent, message, [dict(a, owner=agent_id) for a in attachments], conn)
        except anthropic.APIError as e:
            raise HTTPException(502, f'Could not upload attachment to Claude: {e}')
    container = row[0] if row else None
    tools = BASE_TOOLS + (FILE_TOOLS_CODE if has_code(agent) else []) + \
        (WEB_TOOLS if 'fetch_image' in agent['tools'] else []) + \
        (BG_TOOLS if 'remove_background' in agent['tools'] else []) + \
        [OPTIONAL_TOOLS[t] for t in agent['tools'] if t in OPTIONAL_TOOLS]
    messages = [{'role': role, 'content': content} for role, content in history]
    messages.append({'role': 'user', 'content': first})
    ctx = {'outgoing': [], 'uploads': []}
    texts, generated, tools_used = [], [], []
    try:
        for _ in range(40 if channel == 'schedule' else 16):
            kwargs = dict(model=agent['model'], max_tokens=8000, system=cached_system(system),
                          messages=with_cache_breakpoint(messages), tools=tools)
            if container:
                kwargs['container'] = container
            try:
                result = client.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                if container and 'container' in str(e).lower():
                    container = None  # expired container: start a fresh one
                    kwargs.pop('container')
                    result = client.messages.create(**kwargs)
                else:
                    raise
            if getattr(result, 'container', None) and getattr(result.container, 'id', None):
                container = result.container.id
            blocks = result.content
            texts += [b.text for b in blocks if b.type == 'text']
            for b in blocks:
                generated += [f for f in collect_file_ids(b) if f not in generated]
            tool_calls = [b for b in blocks if b.type == 'tool_use']
            used = [getattr(b, 'name', b.type) for b in blocks if b.type in ('tool_use', 'server_tool_use')]
            print(f"agent {agent_id}: tokens {usage_note(result.usage)}", flush=True)
            if used:
                tools_used += used
                print(f"agent {agent_id}: tools {used}", flush=True)
            if not tool_calls and result.stop_reason != 'pause_turn':
                break
            messages.append({'role': 'assistant', 'content': [b.model_dump(exclude_none=True) for b in blocks]})
            if tool_calls:
                results = [{'type': 'tool_result', 'tool_use_id': b.id,
                            'content': json.dumps(call_tool(agent, b.name, b.input, ctx), ensure_ascii=False)}
                           for b in tool_calls]
                results += [{'type': 'container_upload', 'file_id': f} for f in ctx['uploads']]
                ctx['uploads'] = []
                messages.append({'role': 'user', 'content': results})
            # pause_turn (long server-side tool): send the conversation back as is to let Claude continue.
        else:
            raise HTTPException(502, 'Agent tool loop exceeded')

        # Files code execution produced: download, store under Agents/<id>/outputs/<date>/, send with the reply.
        day = datetime.now(TZ).strftime('%Y-%m-%d')
        for fid in generated:
            meta = client.files.retrieve_metadata(fid)
            data = client.files.download(fid).read()
            name = re.sub(r'[^\w.\-]+', '_', getattr(meta, 'filename', None) or fid)
            key = f'Agents/{agent_id}/outputs/{day}/{name}'
            mime = getattr(meta, 'mime_type', None) or guess_mime(name)
            with db() as conn:
                write_file(conn, agent_id, key, mime, data)
            ctx['outgoing'].append({'key': key, 'mime': mime, 'filename': name, 'data': data})
    except anthropic.APIStatusError as e:
        print(f'Claude API error {e.status_code}: {e.message}', flush=True)
        raise HTTPException(502, f'Claude API error {e.status_code}: {e.message} (model={agent['model']})')
    except anthropic.APIError as e:
        print(f'Claude API error: {type(e).__name__}: {e}', flush=True)
        raise HTTPException(502, f'Claude API error: {type(e).__name__} (model={agent['model']})')

    # The model must not announce deliveries itself (it copies the history note format); the server does it.
    answer = re.sub(r'^\s*[\[(⟦]\s*(files sent|system: files delivered)[^\n]*$', '', '\n'.join(texts),
                    flags=re.IGNORECASE | re.MULTILINE).strip()
    sent = [f['key'] for f in ctx['outgoing']]
    if has_code(agent) and not generated and re.search(r'(הנה|מצורפ|שלחתי|attached|here is)', answer, re.IGNORECASE):
        print(f'agent {agent_id}: reply sounds like a file was delivered, but no file reached $OUTPUT_DIR', flush=True)
    user_log = message + (f"\n[attached: {', '.join(a['key'] for a in attachments)}]" if attachments else '')
    bot_log = (answer or '(no text)') + (f"\n(system: files delivered by the server: {', '.join(sent)})" if sent
                                         else ('\n(system: no file was delivered in this turn)' if has_code(agent) else ''))
    with db() as conn:
        conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, conversation_id, 'user', user_log))
        conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, conversation_id, 'assistant', bot_log))
        if container:
            conn.execute('''INSERT INTO containers(agent_id,conversation_id,container_id) VALUES (%s,%s,%s)
                ON CONFLICT(agent_id,conversation_id) DO UPDATE SET container_id=EXCLUDED.container_id, updated_at=now()''',
                (agent_id, conversation_id, container))
    return {'text': answer, 'files': ctx['outgoing'], 'tools': tools_used}


@app.post('/agents/{agent_id}/chat')
def chat(agent_id: str, request: ChatRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    result = run_agent(agent_id, request.conversation_id, request.message)
    return {'agent_id': agent_id, 'conversation_id': request.conversation_id, 'reply': result['text'],
            'files': [{'key': f['key'], 'mime': f['mime'], 'url': f"/agents/{agent_id}/files/{f['key']}"}
                      for f in result['files']]}


# ---------------- WhatsApp (Whapi) ----------------

def whapi_send_text(to: str, body: str):
    body = re.sub(r'\*\*(.+?)\*\*', r'*\1*', body or '').strip() or '(אין תשובה)'
    for i in range(0, len(body), 4000):
        r = httpx.post(f'{WHAPI_URL}/messages/text', timeout=30,
                       headers={'Authorization': f'Bearer {WHAPI_TOKEN}'},
                       json={'to': to, 'body': body[i:i + 4000]})
        if r.status_code >= 300:
            print(f'Whapi send failed {r.status_code}: {r.text[:300]}', flush=True)


def whapi_send_file(to: str, f: dict, caption: str = ''):
    """Image endpoint for images, document endpoint for everything else. Tries a base64 data URI first,
    then multipart upload, and logs Whapi's answer if both fail."""
    f = dict(f, mime=clean_mime(f['mime'], f['filename']))
    kind = 'image' if f['mime'] in IMAGE_TYPES else 'document'
    url, headers = f'{WHAPI_URL}/messages/{kind}', {'Authorization': f'Bearer {WHAPI_TOKEN}'}
    extra = {'caption': caption} if caption else {}
    if kind == 'document':
        extra['filename'] = f['filename']
    data_uri = f"data:{f['mime']};name={f['filename']};base64," + base64.b64encode(f['data']).decode()
    r = httpx.post(url, timeout=120, headers=headers, json={'to': to, 'media': data_uri, **extra})
    if r.status_code < 300:
        print(f"WA file sent: {f['filename']} ({len(f['data'])} bytes)", flush=True)
        return True
    first = f'{r.status_code}: {r.text[:200]}'
    r = httpx.post(url, timeout=120, headers=headers, data={'to': to, **extra},
                   files={'media': (f['filename'], f['data'], f['mime'])})
    if r.status_code < 300:
        return True
    print(f"Whapi file send failed for {f['key']} (json {first}) (multipart {r.status_code}: {r.text[:200]})", flush=True)
    return False


def route(conn, chat_id: str, text: str):
    """Pick the agent: a message that starts with an agent's name switches to it; otherwise stay with the last one."""
    agents = conn.execute('SELECT id,names FROM agents WHERE id<>%s', (SHARED,)).fetchall()
    first, _, rest = text.strip().partition(' ')
    word = first.strip(',:.!?;-').lower()
    for agent_id, names in agents:
        if word and word in {agent_id.lower(), *[n.lower() for n in (names or [])]}:
            conn.execute('''INSERT INTO wa_sessions(chat_id,agent_id) VALUES (%s,%s)
                ON CONFLICT(chat_id) DO UPDATE SET agent_id=EXCLUDED.agent_id, updated_at=now()''', (chat_id, agent_id))
            return agent_id, rest.strip()
    row = conn.execute('SELECT agent_id FROM wa_sessions WHERE chat_id=%s', (chat_id,)).fetchone()
    return (row[0] if row else None), text.strip()


def agents_menu(conn) -> str:
    rows = conn.execute("SELECT id,names FROM agents WHERE id NOT IN (%s,'ping') ORDER BY id", (SHARED,)).fetchall()
    names = [(n[0] if n else i) for i, n in rows]
    return 'עם מי לדבר? התחל את ההודעה בשם הסוכן: ' + ', '.join(names) + '.'


def fetch_media(m: dict):
    """Download an incoming image/document: first the direct link (Auto Download), then Whapi's GET /media/{id}."""
    media = m.get(m.get('type')) or {}
    attempts = []
    if media.get('link'):
        attempts.append(('link', media['link'], {}))
    if media.get('id'):
        attempts.append(('media-api', f"{WHAPI_URL}/media/{media['id']}", {'Authorization': f'Bearer {WHAPI_TOKEN}'}))
    for label, url, headers in attempts:
        t = time.time()
        try:
            r = httpx.get(url, timeout=httpx.Timeout(20, read=45), follow_redirects=True, headers=headers)
            if r.status_code < 300 and r.content:
                if len(r.content) > MAX_FILE:
                    raise ValueError('file larger than 20MB')
                print(f'WA media: got {len(r.content)} bytes via {label} in {time.time() - t:.1f}s', flush=True)
                return r.content, media
            print(f'WA media: {label} -> {r.status_code} {r.text[:150]}', flush=True)
        except httpx.HTTPError as e:
            print(f'WA media: {label} failed after {time.time() - t:.1f}s: {type(e).__name__}', flush=True)
    return None, media


def handle_whatsapp(chat_id: str, text: str, media_msg: dict | None = None):
    try:
        with db() as conn:
            agent_id, message = route(conn, chat_id, text)
            menu = agents_menu(conn)
        if not agent_id:
            return whapi_send_text(chat_id, menu + (' (את הקובץ שלח שוב אחרי שבחרת)' if media_msg else ''))
        attachments = []
        if media_msg:
            data, media = fetch_media(media_msg)
            if data is None:
                return whapi_send_text(chat_id, 'לא הצלחתי להוריד את הקובץ מ-Whapi. נסה לשלוח שוב.')
            mime = (media.get('mime_type') or '').split(';')[0] or 'application/octet-stream'
            ext = mimetypes.guess_extension(mime) or ''
            name = re.sub(r'[^\w.\-]+', '_', media.get('filename') or f"{media_msg.get('type')}-{str(media_msg.get('id'))[-8:]}{ext}")
            key = f"Agents/{agent_id}/inbox/{datetime.now(TZ).strftime('%Y-%m-%d')}/{name}"
            with db() as conn:
                write_file(conn, agent_id, key, mime, data)
            attachments.append({'key': key, 'mime': mime, 'data': data})
        elif not message:
            return whapi_send_text(chat_id, 'כאן. מה צריך?')
        print(f'WA: -> {agent_id}, text {len(message)} chars, attachments {len(attachments)}', flush=True)
        result = run_agent(agent_id, 'wa:' + chat_id.split('@')[0], message, channel='whatsapp', attachments=attachments)
        print(f"WA: <- {agent_id}, reply {len(result['text'])} chars, files {[f['filename'] for f in result['files']]}", flush=True)
        if result['text'] or not result['files']:
            whapi_send_text(chat_id, result['text'])
        failed = [f['filename'] for f in result['files'] if not whapi_send_file(chat_id, f)]
        if failed:
            whapi_send_text(chat_id, 'לא הצלחתי לשלוח: ' + ', '.join(failed) + '. הקבצים נשמרו בשרת.')
    except HTTPException as e:
        print(f'WhatsApp handling failed: {e.detail}', flush=True)
        whapi_send_text(chat_id, f'משהו נכשל בשרת: {e.detail}')
    except Exception as e:
        print(f'WhatsApp handling crashed: {type(e).__name__}: {e}', flush=True)
        whapi_send_text(chat_id, f'משהו נכשל בשרת ({type(e).__name__}). נסה שוב בעוד רגע.')


@app.post('/whapi/{secret}')
async def whapi_webhook(secret: str, request: Request, background: BackgroundTasks):
    if not WEBHOOK_SECRET or not WHAPI_TOKEN or not secrets.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(404, 'Not Found')
    payload = await request.json()
    for m in payload.get('messages') or []:
        if m.get('from_me') or str(m.get('chat_id', '')).endswith('@g.us'):
            continue  # our own messages and groups are ignored
        sender = re.sub(r'\D', '', str(m.get('from', '')))
        if sender not in ALLOWED_NUMBERS:
            print(f'WhatsApp: ignored message from unapproved number ending {sender[-4:]}', flush=True)
            continue
        with db() as conn:
            fresh = conn.execute('INSERT INTO wa_seen(message_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING 1',
                                 (str(m.get('id')),)).fetchone()
        if not fresh:
            continue  # Whapi retry of a message we already handled
        if m.get('type') == 'text':
            text = (m.get('text') or {}).get('body', '')
        elif m.get('type') == 'link_preview':
            text = (m.get('link_preview') or {}).get('body', '')
        elif m.get('type') in ('image', 'document'):
            caption = (m.get(m['type']) or {}).get('caption', '') or ''
            background.add_task(handle_whatsapp, m['chat_id'], caption, m)
            continue
        else:
            background.add_task(whapi_send_text, m['chat_id'], 'כרגע אני מבין טקסט, תמונות ומסמכים. הודעות קוליות יגיעו בשלב הבא.')
            continue
        if text.strip():
            background.add_task(handle_whatsapp, m['chat_id'], text)
    return {'ok': True}


# ---------------- Schedules (e.g. Gefen every morning at 08:30) ----------------

class ScheduleConfig(BaseModel):
    at: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')  # HH:MM Israel time
    message: str = Field(min_length=1, max_length=2000)
    notify: str | None = None  # WhatsApp number to send the result to, e.g. 9725XXXXXXXX


@app.put('/agents/{agent_id}/schedule')
def put_schedule(agent_id: str, cfg: ScheduleConfig, authorization: str | None = Header(default=None)):
    authorize(authorization)
    notify = re.sub(r'\D', '', cfg.notify or '') or None
    with db() as conn:
        read_agent(conn, agent_id)
        conn.execute('''INSERT INTO schedules(agent_id,at_time,message,notify) VALUES (%s,%s,%s,%s)
            ON CONFLICT(agent_id) DO UPDATE SET at_time=EXCLUDED.at_time, message=EXCLUDED.message,
            notify=EXCLUDED.notify''', (agent_id, cfg.at, cfg.message, notify))
    return {'agent_id': agent_id, 'at': cfg.at, 'message': cfg.message, 'notify': notify}


@app.delete('/agents/{agent_id}/schedule')
def delete_schedule(agent_id: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        conn.execute('DELETE FROM schedules WHERE agent_id=%s', (agent_id,))
    return {'deleted': True}


@app.get('/schedules')
def list_schedules(authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        rows = conn.execute('SELECT agent_id,at_time,message,notify,last_run FROM schedules ORDER BY at_time').fetchall()
    return {'schedules': [{'agent_id': a, 'at': t, 'message': m, 'notify': n, 'last_run': str(l) if l else None}
                          for a, t, m, n, l in rows]}


def run_log(agent_id: str, day: str, text: str):
    """Agents/<id>/runs/<day>.md: what the scheduled run did. Synced to flai-work like any private file."""
    try:
        with db() as conn:
            write_knowledge(conn, agent_id, f'Agents/{agent_id}/runs/{day}.md', text[:MAX_VALUE])
    except Exception as e:
        print(f'schedule: could not write run log: {type(e).__name__}: {e}', flush=True)


def run_scheduled(agent_id: str, message: str, notify: str | None):
    now = datetime.now(TZ)
    day, started = now.strftime('%Y-%m-%d'), now.strftime('%H:%M')
    print(f'schedule: running {agent_id}', flush=True)
    run_log(agent_id, day, f'# {agent_id}: scheduled run {day}\n\n- started: {started}\n- status: running '
            '(if it stays like this, the server stopped in the middle of the run)\n')
    try:
        result = run_agent(agent_id, f'schedule:{day}', message, channel='schedule')
        print(f"schedule: {agent_id} done, reply {len(result['text'])} chars", flush=True)
        with db() as conn:
            conn.execute('UPDATE schedules SET finished=%s WHERE agent_id=%s', (now.date(), agent_id))
        run_log(agent_id, day, f"# {agent_id}: scheduled run {day}\n\n- started: {started}\n"
                f"- finished: {datetime.now(TZ).strftime('%H:%M')}\n- status: done\n"
                f"- tools: {', '.join(result.get('tools') or []) or '(none)'}\n\n## Agent summary\n\n{result['text'] or '(no text)'}\n")
        if notify and WHAPI_TOKEN:
            chat = notify + '@s.whatsapp.net'
            whapi_send_text(chat, result['text'])
            for f in result['files']:
                whapi_send_file(chat, f)
    except Exception as e:
        print(f'schedule: {agent_id} failed: {type(e).__name__}: {getattr(e, "detail", e)}', flush=True)
        run_log(agent_id, day, f"# {agent_id}: scheduled run {day}\n\n- started: {started}\n"
                f"- failed: {datetime.now(TZ).strftime('%H:%M')}\n- error: {type(e).__name__}: {getattr(e, 'detail', e)}\n")
        if notify and WHAPI_TOKEN:
            whapi_send_text(notify + '@s.whatsapp.net', f'הריצה המתוזמנת של {agent_id} נכשלה: {getattr(e, "detail", type(e).__name__)}')


@app.post('/agents/{agent_id}/schedule/run')
def run_schedule_now(agent_id: str, background: BackgroundTasks, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        row = conn.execute('SELECT message,notify FROM schedules WHERE agent_id=%s', (agent_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'No schedule for this agent')
    background.add_task(run_scheduled, agent_id, row[0], row[1])
    return {'started': True, 'agent_id': agent_id}


def scheduler_tick(now=None) -> list:
    """Start every schedule that is due: once a day, at or after its time (up to 3 hours late, e.g. after a restart)."""
    now = now or datetime.now(TZ)
    started = []
    with db() as conn:
        # Due: not started today, or started today but never finished (the server restarted in the middle)
        # and the last attempt is over 30 minutes old: one retry, two attempts a day at most.
        stuck = now - timedelta(minutes=30)
        due = conn.execute('''SELECT agent_id,at_time,message,notify FROM schedules
            WHERE last_run IS NULL OR last_run < %s
               OR (last_run = %s AND (finished IS NULL OR finished < %s) AND attempts < 2 AND started_at < %s)''',
            (now.date(), now.date(), now.date(), stuck)).fetchall()
    for agent_id, at, message, notify in due:
        hh, mm = map(int, at.split(':'))
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not (start <= now <= start + timedelta(hours=3)):
            continue
        with db() as conn:  # claim atomically so a second replica would not run it too
            claimed = conn.execute('''UPDATE schedules
                SET attempts = CASE WHEN last_run = %s THEN attempts + 1 ELSE 1 END, last_run=%s, started_at=%s
                WHERE agent_id=%s AND (last_run IS NULL OR last_run < %s
                    OR (last_run = %s AND (finished IS NULL OR finished < %s) AND attempts < 2 AND started_at < %s))
                RETURNING 1''', (now.date(), now.date(), now, agent_id, now.date(), now.date(), now.date(), stuck)).fetchone()
        if claimed:
            threading.Thread(target=run_scheduled, args=(agent_id, message, notify), daemon=True).start()
            started.append(agent_id)
    return started


def scheduler_loop():
    while True:
        try:
            scheduler_tick()
        except Exception as e:
            print(f'scheduler error: {type(e).__name__}: {e}', flush=True)
        time.sleep(30)


# ---------------- Two-way sync with flai-work ----------------

@app.get('/sync/manifest')
def sync_manifest(authorization: str | None = Header(default=None)):
    """Every knowledge entry and file with its sha256, so the sync script can tell who changed what."""
    authorize(authorization)
    with db() as conn:
        k = conn.execute('''SELECT agent_id,key,encode(sha256(convert_to(value,'UTF8')),'hex'),updated_at
            FROM knowledge ORDER BY key''').fetchall()
        f = conn.execute('''SELECT agent_id,key,encode(sha256(data),'hex'),updated_at,mime FROM files ORDER BY key''').fetchall()
    return {'knowledge': [{'scope': a, 'key': key, 'sha256': h, 'updated_at': t.isoformat()} for a, key, h, t in k],
            'files': [{'scope': a, 'key': key, 'sha256': h, 'updated_at': t.isoformat(), 'mime': m} for a, key, h, t, m in f]}


@app.get('/agents/{agent_id}/knowledge/{key:path}')
def get_knowledge_entry(agent_id: str, key: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        row = conn.execute('SELECT value FROM knowledge WHERE agent_id=%s AND key=%s', (agent_id, normalize_key(key))).fetchone()
    if not row:
        raise HTTPException(404, 'Not found')
    return Response(content=row[0].encode('utf-8'), media_type='text/plain; charset=utf-8')
