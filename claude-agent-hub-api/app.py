import base64
import json
import mimetypes
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
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
        conn.execute('''CREATE TABLE IF NOT EXISTS containers (
            agent_id TEXT NOT NULL, conversation_id TEXT NOT NULL, container_id TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(agent_id, conversation_id)
        )''')
        conn.execute('''INSERT INTO agents (id,instructions) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING''',
                     (SHARED, 'Shared knowledge namespace. Not a chat agent.'))


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
        rows = conn.execute('SELECT id,tools,shared_write,names FROM agents WHERE id<>%s ORDER BY id', (SHARED,)).fetchall()
    return {'agents': [{'id': i, 'tools': t, 'shared_write': w, 'names': n} for i, t, w, n in rows]}


@app.put('/agents/{agent_id}')
def upsert_agent(agent_id: str, request: AgentConfig, authorization: str | None = Header(default=None)):
    authorize(authorization)
    if not valid_agent_id(agent_id) or agent_id == SHARED:
        raise HTTPException(400, 'Invalid agent_id')
    unknown = [t for t in request.tools if t not in OPTIONAL_TOOLS]
    if unknown:
        raise HTTPException(400, f'Unknown tools: {unknown}. Available: {sorted(OPTIONAL_TOOLS)}')
    with db() as conn:
        conn.execute('''INSERT INTO agents (id,instructions,tools,shared_write,names) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET instructions=EXCLUDED.instructions,
            tools=EXCLUDED.tools, shared_write=EXCLUDED.shared_write, names=EXCLUDED.names''',
            (agent_id, request.instructions, json.dumps(request.tools), request.shared_write,
             json.dumps(request.names, ensure_ascii=False)))
    return {'agent_id': agent_id, 'saved': True, 'tools': request.tools, 'shared_write': request.shared_write,
            'names': request.names}


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
    mime = request.headers.get('content-type') or guess_mime(key)
    if mime in ('application/x-www-form-urlencoded', 'application/octet-stream'):
        mime = guess_mime(key)
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
    row = conn.execute('SELECT id,instructions,tools,shared_write FROM agents WHERE id=%s', (agent_id,)).fetchone()
    if not row or agent_id == SHARED:
        raise HTTPException(404, 'Agent not found')
    return {'id': row[0], 'instructions': row[1], 'tools': row[2] or [], 'shared_write': row[3]}


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
            + 'FILES: images, logos and PDFs are stored separately from knowledge (see list_files). '
            + 'To give the user an existing file, call send_file. '
            + (f"You have code execution (Python with Pillow, matplotlib, etc.; no internet in the sandbox). "
               f"Canva is not connected in the cloud, so build images and documents with code. To use a stored "
               f"file (e.g. a logo) in code, call use_files first. Every file you save at the top level of "
               f"$OUTPUT_DIR is sent to the user with your reply and stored under Agents/{agent['id']}/outputs/ - "
               f"so put only finished deliverables there, and run `ls \"$OUTPUT_DIR\"` to confirm.\n"
               if has_code(agent) else 'You cannot create new files.\n')
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
        [OPTIONAL_TOOLS[t] for t in agent['tools'] if t in OPTIONAL_TOOLS]
    messages = [{'role': role, 'content': content} for role, content in history]
    messages.append({'role': 'user', 'content': first})
    ctx = {'outgoing': [], 'uploads': []}
    texts, generated = [], []
    try:
        for _ in range(16):
            kwargs = dict(model=CLAUDE_MODEL, max_tokens=8000, system=system, messages=messages, tools=tools)
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
        raise HTTPException(502, f'Claude API error {e.status_code}: {e.message} (model={CLAUDE_MODEL})')
    except anthropic.APIError as e:
        print(f'Claude API error: {type(e).__name__}: {e}', flush=True)
        raise HTTPException(502, f'Claude API error: {type(e).__name__} (model={CLAUDE_MODEL})')

    answer = '\n'.join(texts).strip()
    sent = [f['key'] for f in ctx['outgoing']]
    user_log = message + (f"\n[attached: {', '.join(a['key'] for a in attachments)}]" if attachments else '')
    bot_log = (answer or '(no text)') + (f"\n[files sent: {', '.join(sent)}]" if sent else '')
    with db() as conn:
        conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, conversation_id, 'user', user_log))
        conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, conversation_id, 'assistant', bot_log))
        if container:
            conn.execute('''INSERT INTO containers(agent_id,conversation_id,container_id) VALUES (%s,%s,%s)
                ON CONFLICT(agent_id,conversation_id) DO UPDATE SET container_id=EXCLUDED.container_id, updated_at=now()''',
                (agent_id, conversation_id, container))
    return {'text': answer, 'files': ctx['outgoing']}


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
    kind = 'image' if f['mime'] in IMAGE_TYPES else 'document'
    url, headers = f'{WHAPI_URL}/messages/{kind}', {'Authorization': f'Bearer {WHAPI_TOKEN}'}
    extra = {'caption': caption} if caption else {}
    if kind == 'document':
        extra['filename'] = f['filename']
    data_uri = f"data:{f['mime']};name={f['filename']};base64," + base64.b64encode(f['data']).decode()
    r = httpx.post(url, timeout=120, headers=headers, json={'to': to, 'media': data_uri, **extra})
    if r.status_code < 300:
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
    """Download an incoming image/document from Whapi. Needs 'Auto Download' on in the Whapi channel settings."""
    media = m.get(m.get('type')) or {}
    link = media.get('link')
    if not link:
        return None, media
    r = httpx.get(link, timeout=60, follow_redirects=True, headers={'Authorization': f'Bearer {WHAPI_TOKEN}'})
    r.raise_for_status()
    if len(r.content) > MAX_FILE:
        raise ValueError('file larger than 20MB')
    return r.content, media


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
                return whapi_send_text(chat_id, 'לא הצלחתי להוריד את הקובץ. צריך להפעיל Auto Download בהגדרות הערוץ ב-Whapi.')
            mime = (media.get('mime_type') or '').split(';')[0] or 'application/octet-stream'
            ext = mimetypes.guess_extension(mime) or ''
            name = re.sub(r'[^\w.\-]+', '_', media.get('filename') or f"{media_msg.get('type')}-{str(media_msg.get('id'))[-8:]}{ext}")
            key = f"Agents/{agent_id}/inbox/{datetime.now(TZ).strftime('%Y-%m-%d')}/{name}"
            with db() as conn:
                write_file(conn, agent_id, key, mime, data)
            attachments.append({'key': key, 'mime': mime, 'data': data})
        elif not message:
            return whapi_send_text(chat_id, 'כאן. מה צריך?')
        result = run_agent(agent_id, 'wa:' + chat_id.split('@')[0], message, channel='whatsapp', attachments=attachments)
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
        whapi_send_text(chat_id, 'משהו נכשל בשרת. נסה שוב בעוד רגע.')


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
