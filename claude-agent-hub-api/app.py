import json
import os
import secrets
import time
from contextlib import asynccontextmanager

import anthropic
import psycopg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv('DATABASE_URL', '')
HUB_TOKEN = os.getenv('HUB_TOKEN', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', '')

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
        rows = conn.execute('SELECT id,tools,shared_write FROM agents WHERE id<>%s ORDER BY id', (SHARED,)).fetchall()
    return {'agents': [{'id': i, 'tools': t, 'shared_write': w} for i, t, w in rows]}


@app.put('/agents/{agent_id}')
def upsert_agent(agent_id: str, request: AgentConfig, authorization: str | None = Header(default=None)):
    authorize(authorization)
    if not valid_agent_id(agent_id) or agent_id == SHARED:
        raise HTTPException(400, 'Invalid agent_id')
    unknown = [t for t in request.tools if t not in OPTIONAL_TOOLS]
    if unknown:
        raise HTTPException(400, f'Unknown tools: {unknown}. Available: {sorted(OPTIONAL_TOOLS)}')
    with db() as conn:
        conn.execute('''INSERT INTO agents (id,instructions,tools,shared_write) VALUES (%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET instructions=EXCLUDED.instructions,
            tools=EXCLUDED.tools, shared_write=EXCLUDED.shared_write''',
            (agent_id, request.instructions, json.dumps(request.tools), request.shared_write))
    return {'agent_id': agent_id, 'saved': True, 'tools': request.tools, 'shared_write': request.shared_write}


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


def build_system(conn, agent):
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
            + 'KNOWLEDGE INDEX:\n' + index)


@app.post('/agents/{agent_id}/chat')
def chat(agent_id: str, request: ChatRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        agent = read_agent(conn, agent_id)
        system = build_system(conn, agent)
        history = conn.execute('''SELECT role,content FROM (SELECT id,role,content FROM messages
            WHERE agent_id=%s AND conversation_id=%s ORDER BY id DESC LIMIT 16) t ORDER BY id''',
            (agent_id, request.conversation_id)).fetchall()
    tools = BASE_TOOLS + [OPTIONAL_TOOLS[t] for t in agent['tools'] if t in OPTIONAL_TOOLS]
    messages = [{'role': role, 'content': content} for role, content in history]
    messages.append({'role': 'user', 'content': request.message})
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
                    conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, request.conversation_id, 'user', request.message))
                    conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id, request.conversation_id, 'assistant', answer or '(no text)'))
                return {'agent_id': agent_id, 'conversation_id': request.conversation_id, 'reply': answer}
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
