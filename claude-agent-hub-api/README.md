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
- `tools/flai_sync.pl` (perl + curl, both on macOS) syncs `~/flai-work` and the cloud both ways; see below.

## WhatsApp via Whapi

Variables: `WHAPI_TOKEN` (channel token), `WEBHOOK_SECRET` (random, part of the webhook URL), `ALLOWED_NUMBERS` (comma separated, e.g. `972501234567`). In Whapi set the webhook to `https://YOUR-DOMAIN/whapi/<WEBHOOK_SECRET>` for incoming messages.

Start a message with an agent's name ("גרשון, ...") to talk to it; later messages stay with that agent until you name another. Only text for now; voice and images come next. Messages from other numbers and from groups are ignored.

## Files (images, logos, PDFs)

- `PUT /agents/{id}/files/<key>` with the raw file as body stores a binary file (`_shared` allowed). `GET /agents/{id}/files` lists, `GET /agents/{id}/files/<key>` downloads.
- Agents get `list_files` and `send_file`. With `"tools": ["code_execution"]` they also get Anthropic's sandbox (Python + Pillow, no internet) and `use_files` to copy stored files into it. Every file they save at the top level of `$OUTPUT_DIR` is stored under `Agents/<id>/outputs/<date>/` and returned with the reply (`files` in the API response, sent as image/document on WhatsApp).
- Incoming WhatsApp images and documents are stored under `Agents/<id>/inbox/<date>/`, shown to the agent, and copied into its sandbox. Requires **Auto Download** on in the Whapi channel. The caption picks the agent like a text message.

## Gefen in the cloud

- Server-side tools per agent: `fetch_image` (adds `find_images` + `fetch_image`; only pages/images on the sites listed in the agent's `current-state.md`) and `remove_background` (rembg, `isnet-general-use`; the model is downloaded at build time, ~180MB, and needs ~1.5GB RAM while running).
  Background removal runs in its own process (`bg_worker.py`, images scaled to max 2000px): if it runs out of memory, only that step fails and the agent carries on.
- Scheduled runs: each run writes `Agents/<id>/runs/<date>.md` (started / done / failed, tools used, the agent's summary); it syncs to flai-work. A run that started but never finished (server restart) is retried once after 30 minutes, at most two attempts a day. The agent is told to save its output file early and update it as it goes.
- Schedules: `PUT /agents/{id}/schedule` `{"at":"08:30","message":"הפעל את גפן","notify":null}` runs once a day in Israel time (up to 3 hours late after a restart). `POST /agents/{id}/schedule/run` runs it now. `GET /schedules` lists them. With `notify` set to a WhatsApp number, the result is sent there.

## Two-way sync with flai-work

`perl tools/flai_sync.pl [--dry-run]` (copy it to `~/flai-work/sync.pl`). Uses `GET /sync/manifest` (sha256 of every entry) and a local state file `.cloud-sync-state.json`. Changed on one side → copied to the other; changed on both → your file stays and the cloud version is saved as `<name>.cloud-conflict-<date>.<ext>`; nothing is ever deleted. Agent instructions and settings always go computer → cloud.

## Cost
- Prompt caching: every step of a run resends the tools, the system prompt and the conversation so far. The Hub marks two cache breakpoints (end of the system prompt, end of the latest message), so later steps within ~5 minutes pay the cache-read price for that prefix. Each step logs its tokens: `agent <id>: tokens in X + cache write Y + cache read Z, out W`.
- The model for all agents is the `CLAUDE_MODEL` variable (e.g. `claude-haiku-5-5` is far cheaper than Sonnet for search-and-summarize agents).

## WhatsApp via Meta's official Cloud API
Runs next to Whapi; both numbers reach the same agents, with the same "name, message" routing and one conversation history per person.

Railway variables:
- `META_TOKEN`: System User token (business_management, whatsapp_business_messaging, whatsapp_business_management)
- `META_PHONE_NUMBER_ID`: the number's phone number ID
- `META_VERIFY_TOKEN`: any string; enter the same one as the Verify token at Meta
- `META_APP_SECRET`: the app's App Secret (App settings > Basic); every webhook POST must carry a valid signature
- `META_GRAPH_VERSION` (optional, default `v23.0`)
- Reuses `WEBHOOK_SECRET` and `ALLOWED_NUMBERS`.

Callback URL at Meta: `https://<hub>/meta/<WEBHOOK_SECRET>`, subscribed to the `messages` field.
Outside the 24-hour window after the person's last message, Meta accepts only approved templates, so a scheduled notice can fail there (logged as `Meta send failed`).
