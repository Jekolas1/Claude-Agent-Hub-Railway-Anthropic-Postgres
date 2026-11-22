# Claude Agent Hub — Railway + Anthropic + Postgres

This is a **starter**, not the completed WhatsApp bot. It supports separately configured Claude agents, per-agent shared knowledge, conversation history, and authenticated HTTPS endpoints for Make. Voice, media, and asynchronous jobs will be added via Make later. The original Claude Desktop project is not synced automatically.

## Railway setup

1. Make a **private GitHub repository** with the contents of this folder.
2. In the existing Railway project, add a new service from this repository. Railway builds the `Dockerfile`.
3. Add these variables **on the new application service**:
   - `DATABASE_URL`: Railway **reference variable** pointing to Postgres `DATABASE_URL` (select the Postgres service's variable in UI, not a copied password).
   - `ANTHROPIC_API_KEY`: create in Anthropic Console; paste only into Railway Variables.
   - `CLAUDE_MODEL`: copy the **exact supported API model ID** from Anthropic Console (e.g., a Sonnet model ID available to your workspace).
   - `HUB_TOKEN`: a new strong random secret, at least 32 bytes. Keep it out of source and chats.
4. Go to the service Networking settings → **Generate Domain**. Set healthcheck path `/health` if desired.
5. Confirm `https://YOUR-DOMAIN/health` reports `status: ok` and `database: connected`.

## Create FLAI SOHO agent

Use Postman, curl, or Make's HTTP module. Replace `YOUR-DOMAIN` and `YOUR_HUB_TOKEN` locally; do not share secrets.

```bash
curl -X PUT 'https://YOUR-DOMAIN/agents/flai-soho' \
  -H 'Authorization: Bearer YOUR_HUB_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"instructions":"You are the FLAI SOHO project assistant. Respond in Hebrew. The knowledge stored for this agent is authoritative for project decisions."}'
```

## Send a message (Make HTTP POST)

```bash
curl -X POST 'https://YOUR-DOMAIN/agents/flai-soho/chat' \
  -H 'Authorization: Bearer YOUR_HUB_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"whatsapp-user-001","message":"שלום, מה הסטטוס?"}'
```

The JSON response contains `reply`. Make will send this text back via WhatsApp. For different agents, create `agent-02`, etc. and send requests to their own paths. Avoid using untrusted sender-provided agent IDs: map each authorized number/command to an allowed agent.

## Knowledge sync

`PUT /agents/flai-soho/knowledge/<key>` with JSON `{"value":"..."}` saves a fact. `GET /agents/flai-soho/knowledge` retrieves it. Agents also have bounded tool calls to save/search knowledge during chat. History is retained per conversation; the latest 16 messages are loaded each time. No automatic import of Claude Desktop knowledge.

## Notes for production

- Keep the GitHub repo private; never commit secrets.
- Currently one shared administrative bearer token protects all agents. Before inviting external users, use per-agent authorization and rate limiting.
- Tool execution is capped at five rounds. Configure explicit confirmation for sensitive or external actions before enabling them.
- The web app does not send WhatsApp/audio/files yet. Make is the media gateway.
- `health` is public by design and exposes only a minimal status.
- Add request-id idempotency, retention rules, backups, budgets, logs/metrics and a queued worker before production scale.
