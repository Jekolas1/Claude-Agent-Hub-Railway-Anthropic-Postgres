import json
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
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
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
}
MAX_VALUE = 30000


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
]


def call_tool(agent, name, args):
    agent_id, shared_write = agent['id'], agent['shared_write']
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


def read_agent(conn, agent_id):
    row = conn.execute('SELECT id,instructions,tools,shared_write FROM agents WHERE id=%s', (agent_id,)).fetchone()
    if not row or agent_id == SHARED:
        raise HTTPException(404, 'Agent not found')
    return {'id': row[0], 'instructions': row[1], 'tools': row[2] or [], 'shared_write': row[3]}


def build_system(conn, agent, channel='api'):
    keys = conn.execute('''SELECT agent_id,key FROM knowledge WHERE agent_id IN (%s,%s)
        ORDER BY key LIMIT 300''', (agent['id'], SHARED)).fetchall()
    index = '\n'.join(f"- {k} ({'shared' if a == SHARED else 'private'})" for a, k in keys) or '(empty)'
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
               'headings; use *bold* sparingly. You cannot send files or images yet: if your work produces one, '
               'say so instead of pretending you sent it.\n' if channel == 'whatsapp' else '')
            + 'KNOWLEDGE INDEX:\n' + index)


def run_agent(agent_id: str, conversation_id: str, message: str, channel: str = 'api') -> str:
    with db() as conn:
        agent = read_agent(conn, agent_id)
        system = build_system(conn, agent, channel)
        history = conn.execute('''SELECT role,content FROM (SELECT id,role,content FROM messages
            WHERE agent_id=%s AND conversation_id=%s ORDER BY id DESC LIMIT 16) t ORDER BY id''',
            (agent_id, conversation_id)).fetchall()
    tools = BASE_TOOLS + [OPTIONAL_TOOLS[t] for t in agent['tools'] if t in OPTIONAL_TOOLS]
    messages = [{'role': role, 'content': content} for role, content in history]
    messages.append({'role': 'user', 'content': message})
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=170.0)
    texts = []
    try:
        for _ in range(12):
            result = client.messages.create(model=CLAUDE_MODEL, max_tokens=8000, system=system,
                                            messages=messages, tools=tools)
            blocks = result.content
            texts += [b.text for b in blocks if b.type == 'text']
            tool_calls = [b for b in blocks if b.type == 'tool_use']
            if not tool_calls and result.stop_reason != 'pause_turn':
                answer = '\n'.join(texts).strip()
                with db() as conn:
                    conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, conversation_id, 'user', message))
                    conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, conversation_id, 'assistant', answer or '(no text)'))
                return answer
            messages.append({'role': 'assistant', 'content': [b.model_dump(exclude_none=True) for b in blocks]})
            if tool_calls:
                messages.append({'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': b.id,
                     'content': json.dumps(call_tool(agent, b.name, b.input), ensure_ascii=False)}
                    for b in tool_calls]})
            # pause_turn (long server-side web search): send the conversation back as is to let Claude continue.
    except anthropic.APIStatusError as e:
        print(f'Claude API error {e.status_code}: {e.message}', flush=True)
        raise HTTPException(502, f'Claude API error {e.status_code}: {e.message} (model={CLAUDE_MODEL})')
    except anthropic.APIError as e:
        print(f'Claude API error: {type(e).__name__}: {e}', flush=True)
        raise HTTPException(502, f'Claude API error: {type(e).__name__} (model={CLAUDE_MODEL})')
    raise HTTPException(502, 'Agent tool loop exceeded')


@app.post('/agents/{agent_id}/chat')
def chat(agent_id: str, request: ChatRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    reply = run_agent(agent_id, request.conversation_id, request.message)
    return {'agent_id': agent_id, 'conversation_id': request.conversation_id, 'reply': reply}


# ---------------- WhatsApp (Whapi) ----------------

def whapi_send_text(to: str, body: str):
    body = re.sub(r'\*\*(.+?)\*\*', r'*\1*', body or '').strip() or '(אין תשובה)'
    for i in range(0, len(body), 4000):
        r = httpx.post(f'{WHAPI_URL}/messages/text', timeout=30,
                       headers={'Authorization': f'Bearer {WHAPI_TOKEN}'},
                       json={'to': to, 'body': body[i:i + 4000]})
        if r.status_code >= 300:
            print(f'Whapi send failed {r.status_code}: {r.text[:300]}', flush=True)


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


def handle_whatsapp(chat_id: str, text: str):
    try:
        with db() as conn:
            agent_id, message = route(conn, chat_id, text)
            menu = agents_menu(conn)
        if not agent_id:
            return whapi_send_text(chat_id, menu)
        if not message:
            return whapi_send_text(chat_id, 'כאן. מה צריך?')
        reply = run_agent(agent_id, 'wa:' + chat_id.split('@')[0], message, channel='whatsapp')
        whapi_send_text(chat_id, reply)
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
        else:
            background.add_task(whapi_send_text, m['chat_id'], 'כרגע אני מבין רק הודעות טקסט. קול ותמונות יגיעו בשלב הבא.')
            continue
        if text.strip():
            background.add_task(handle_whatsapp, m['chat_id'], text)
    return {'ok': True}
