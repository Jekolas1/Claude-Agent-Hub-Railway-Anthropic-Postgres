import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import anthropic
import psycopg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv('DATABASE_URL', '')
HUB_TOKEN = os.getenv('HUB_TOKEN', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', '')


def db():
    return psycopg.connect(DATABASE_URL, autocommit=False)


def init_db():
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, instructions TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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


class AgentConfig(BaseModel):
    instructions: str = Field(min_length=10, max_length=30000)


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


@app.put('/agents/{agent_id}')
def upsert_agent(agent_id: str, request: AgentConfig, authorization: str | None = Header(default=None)):
    authorize(authorization)
    if not agent_id.replace('-', '').replace('_', '').isalnum() or len(agent_id) > 64:
        raise HTTPException(400, 'Invalid agent_id')
    with db() as conn:
        conn.execute('''INSERT INTO agents (id,instructions) VALUES (%s,%s)
            ON CONFLICT(id) DO UPDATE SET instructions=EXCLUDED.instructions''',
            (agent_id, request.instructions))
    return {'agent_id': agent_id, 'saved': True}


@app.get('/agents/{agent_id}/knowledge')
def get_knowledge(agent_id: str, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        results = conn.execute('SELECT key,value,updated_at FROM knowledge WHERE agent_id=%s ORDER BY key LIMIT 500', (agent_id,)).fetchall()
    return {'agent_id': agent_id, 'knowledge': [{'key': k, 'value': v, 'updated_at': t.isoformat()} for k, v, t in results]}


@app.put('/agents/{agent_id}/knowledge/{key}')
def put_knowledge(agent_id: str, key: str, body: dict, authorization: str | None = Header(default=None)):
    authorize(authorization)
    value = body.get('value')
    if not isinstance(value, str) or not value.strip() or len(value) > 12000 or len(key) > 120:
        raise HTTPException(400, 'Invalid key or value')
    with db() as conn:
        if not conn.execute('SELECT 1 FROM agents WHERE id=%s', (agent_id,)).fetchone():
            raise HTTPException(404, 'Agent not found')
        conn.execute('''INSERT INTO knowledge(agent_id,key,value) VALUES (%s,%s,%s)
            ON CONFLICT(agent_id,key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''', (agent_id,key,value))
    return {'saved': True, 'key': key}


def read_agent_context(conn, agent_id, conversation_id):
    agent = conn.execute('SELECT instructions FROM agents WHERE id=%s', (agent_id,)).fetchone()
    if not agent:
        raise HTTPException(404, 'Agent not found')
    facts = conn.execute('''SELECT key,value FROM knowledge WHERE agent_id=%s
                               ORDER BY updated_at DESC LIMIT 60''', (agent_id,)).fetchall()
    history = conn.execute('''SELECT role,content FROM (SELECT id,role,content FROM messages
        WHERE agent_id=%s AND conversation_id=%s ORDER BY id DESC LIMIT 16) t ORDER BY id''',
        (agent_id, conversation_id)).fetchall()
    return agent[0], facts, history


TOOLS = [
    {'name': 'save_knowledge', 'description': 'Save important stable facts or explicit decisions to shared knowledge. Do not save casual conversation or secrets.',
     'input_schema': {'type': 'object', 'properties': {'key': {'type': 'string'}, 'value': {'type': 'string'}}, 'required': ['key','value']}},
    {'name': 'search_knowledge', 'description': 'Search saved shared knowledge by keyword.',
     'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}},
]


def call_tool(agent_id, name, args):
    if name == 'save_knowledge':
        key, value = str(args.get('key',''))[:120], str(args.get('value',''))[:12000]
        if not key or not value:
            return {'error': 'Missing key or value'}
        with db() as conn:
            conn.execute('''INSERT INTO knowledge (agent_id,key,value) VALUES (%s,%s,%s)
                ON CONFLICT(agent_id,key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''', (agent_id,key,value))
        return {'saved': True, 'key': key}
    if name == 'search_knowledge':
        term = str(args.get('query',''))[:100]
        with db() as conn:
            rows = conn.execute('''SELECT key,value FROM knowledge
                WHERE agent_id=%s AND (key ILIKE %s OR value ILIKE %s)
                ORDER BY updated_at DESC LIMIT 12''', (agent_id,'%'+term+'%','%'+term+'%')).fetchall()
        return {'results': [{'key': k, 'value': v} for k,v in rows]}
    return {'error': 'Unknown tool'}


@app.post('/agents/{agent_id}/chat')
def chat(agent_id: str, request: ChatRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with db() as conn:
        instructions, facts, history = read_agent_context(conn, agent_id, request.conversation_id)
    knowledge = '\n'.join(f'{k}: {v}' for k,v in facts)
    system = instructions + '\n\nSHARED KNOWLEDGE (source of truth, may be updated via tools):\n' + knowledge + '\n\nUse save_knowledge for explicit decisions or lasting project facts. Never claim a tool ran unless it succeeded. Do not reveal credentials.'
    messages = [{'role': role, 'content': content} for role, content in history]
    messages.append({'role': 'user', 'content': request.message})
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=65.0)
    try:
        for _ in range(5):
            result = client.messages.create(model=CLAUDE_MODEL, max_tokens=1600, system=system, messages=messages, tools=TOOLS)
            blocks = result.content
            tool_calls = [b for b in blocks if b.type == 'tool_use']
            if not tool_calls:
                answer = '\n'.join(b.text for b in blocks if b.type == 'text').strip()
                with db() as conn:
                    conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id,request.conversation_id,'user',request.message))
                    conn.execute('INSERT INTO messages(agent_id,conversation_id,role,content) VALUES (%s,%s,%s,%s)', (agent_id,request.conversation_id,'assistant',answer))
                return {'agent_id': agent_id, 'conversation_id': request.conversation_id, 'reply': answer}
            messages.append({'role': 'assistant', 'content': [b.model_dump(exclude_none=True) for b in blocks]})
            tool_results = []
            for block in tool_calls:
                output = call_tool(agent_id, block.name, block.input)
                tool_results.append({'type':'tool_result','tool_use_id':block.id,'content':str(output)})
            messages.append({'role':'user','content':tool_results})
    except anthropic.APIError:
        raise HTTPException(502, 'Claude API request failed')
    raise HTTPException(502, 'Agent tool loop exceeded')
