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

## Agents from flai-work (cloud runtime)

- `PUT /agents/{id}` accepts `{"instructions": "...", "tools": ["web_search"], "shared_write": true}`.
- Knowledge keys are file paths (e.g. `clients/kulam.md`). Keys under `Agents/<id>/` are private to that agent; everything else lives in the `_shared` namespace that every agent can read. Only agents with `shared_write` can write shared keys.
- Agents get `list_knowledge`, `read_knowledge`, `save_knowledge`, `search_knowledge`. Their instructions keep mentioning `flai-work/<path>`; the runtime maps it to key `<path>`.
- `GET /agents` lists agents. `GET|PUT /agents/_shared/knowledge/...` manages shared knowledge.
- `tools/sync_flai.sh` (macOS, no Python needed) or `tools/sync_flai.py` uploads gershon, gefen and tirosh (SKILL.md + text files) from `~/flai-work`. Re-run it to refresh the cloud copy after local edits. Images are not uploaded yet.

## WhatsApp via Whapi

Variables: `WHAPI_TOKEN` (channel token), `WEBHOOK_SECRET` (random, part of the webhook URL), `ALLOWED_NUMBERS` (comma separated, e.g. `972501234567`). In Whapi set the webhook to `https://YOUR-DOMAIN/whapi/<WEBHOOK_SECRET>` for incoming messages.

Start a message with an agent's name ("גרשון, ...") to talk to it; later messages stay with that agent until you name another. Only text for now; voice and images come next. Messages from other numbers and from groups are ignored.

## Files (images, logos, PDFs)

- `PUT /agents/{id}/files/<key>` with the raw file as body stores a binary file (`_shared` allowed). `GET /agents/{id}/files` lists, `GET /agents/{id}/files/<key>` downloads.
- Agents get `list_files` and `send_file`. With `"tools": ["code_execution"]` they also get Anthropic's sandbox (Python + Pillow, no internet) and `use_files` to copy stored files into it. Every file they save at the top level of `$OUTPUT_DIR` is stored under `Agents/<id>/outputs/<date>/` and returned with the reply (`files` in the API response, sent as image/document on WhatsApp).
- Incoming WhatsApp images and documents are stored under `Agents/<id>/inbox/<date>/`, shown to the agent, and copied into its sandbox. Requires **Auto Download** on in the Whapi channel. The caption picks the agent like a text message.
