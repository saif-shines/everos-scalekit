# everos-scalekit

Identity-scoped memory: [Scalekit](https://www.scalekit.com) decides who the
agent is, [EverOS](https://github.com/EverMind-AI/EverOS) decides what it knows.
Memory scope is derived from the verified token — never from the request body.

## The demo

Two tenants, one memory, one gateway. Alice stores a memory and recalls it;
Bob then asks the same question *with Alice's `user_id` in the request body*.

```bash
# gateway in front of a self-hosted EverOS (everos server start) ...
SCALEKIT_MOCK=1 uvicorn gateway:app --port 8080

# ... or in front of EverOS Cloud
SCALEKIT_MOCK=1 EVEROS_URL=https://api.evermind.ai EVEROS_API_KEY=<key> \
    uvicorn gateway:app --port 8080

python demo.py
```

```text
  Alice  usr_alice @ org_acme ────────────────────────────────
  → add     I'm allergic to shellfish.
  → add     I had a bad reaction to shrimp last summer.
  → flush   extracted
  → search  'what should I avoid at dinner?'
    OK    Alice reports a shellfish allergy and a bad reaction to shrimp last summer (summer 2025)

  Bob  usr_bob @ org_globex ──────────────────────────────────
  → search  'what should I avoid at dinner?'
            body claims user_id='usr_alice'
    none  no results
            gateway rewrote  user_id → usr_bob   (token sub)
                             app_id  → org_globex   (token oid)

  Alice's memory persisted. Bob's request never reached it.
```

Captured from a real run against EverOS Cloud (2026-08-18); the recall line is
LLM-extracted, so its wording varies run to run. Note it inferred *(summer 2025)*
from "last summer" — that is extraction working, not an echo of the input.
Against a self-hosted server, the flush step also prints the episode's markdown
path on disk. Bob only asks after Alice's recall has succeeded — EverOS's index
is eventually consistent, so an empty result before that would prove nothing.

`SCALEKIT_MOCK=1` stubs **only the identity provider** (tokens carry their
claims in plaintext, loudly unverified) so anyone can run this without a
Scalekit account. Everything downstream is real: the scope derivation, EverOS's
LLM extraction, the markdown on disk, the recall. With a real Scalekit
environment, drop the flag and pass two access tokens via `ALICE_TOKEN` /
`BOB_TOKEN`.

## The mechanism

[`gateway.py`](gateway.py) — one file, ~150 lines. Scalekit validates the bearer
token; the scope is then derived from the claims and overwrites whatever the
caller asked for:

| Scalekit claim | Example | EverOS field | |
| --- | --- | --- | --- |
| `sub` | `usr_8967800122X995270` | `user_id` | who is asking |
| `oid` | `org_89678001X21929734` | `app_id` | which tenant they belong to |
| `memory_project` | `support` | `project_id` | which agentic product/module |

`memory_project` is a custom [Token Claim](https://docs.scalekit.com/guides/accesstoken-claims/). Org metadata holds the value. Token Claims copies it onto the JWT. This gateway reads that key. If the claim is missing, it uses `EVEROS_PROJECT_ID` (default `agent`).

Do not map `client_id` → `project_id`. User tokens do include `client_id`, but it is the Scalekit application (`skc_…`) and is the same for every user. That is not an EverOS folder.

Scalekit's id formats already satisfy EverOS's `ScopeId` charset (`^[a-zA-Z0-9_.-]+$`,
1–128 chars), so nothing needs sanitising. And EverOS already guarantees that a
`/search` or `/get` never crosses an `(app_id, project_id)` pair — so mapping
the Scalekit organization onto `app_id` buys tenant isolation without writing
any enforcement.

Writes are covered too: on `/add`, the gateway overwrites each user message's
`sender_id` (EverOS's per-message identity key), so a client can no more write
into someone else's memory than read from it. On `/search`, a caller passing
`agent_id` to sidestep the user track gets it stripped.

## Why this layer exists

EverOS reads its scope from the request body by design: it is local-first,
binds to loopback, ships no authentication, and warns you when you leave
loopback. Its [API reference](https://github.com/EverMind-AI/EverOS/blob/main/docs/api.md)
says *"place your own gateway or auth layer in front."* This repo is that
sentence, implemented with Scalekit.

## Make `project_id` work

`sub` and `oid` arrive on every user token. `project_id` does not. You must put
the agentic product/module name in Scalekit, then tell this gateway which JWT
key to read.

### One product only

Skip Token Claims. In this repo, set `EVEROS_PROJECT_ID` to the folder name
(for example `agent`) and run the gateway. Every tenant then shares that one
EverOS project. Tenant isolation still comes from `oid` → `app_id`.

### Many products / modules

Do the Scalekit steps first. Then run this repo. Old tokens will not grow the
new claim. Mint again after you save Token Claims.

#### 1. Scalekit dashboard

**A. Write the value on each organization**

1. Open the organization (Alice’s org, Bob’s org, …).
2. Add metadata key `memory_project`.
3. Set the value to the EverOS folder, for example `support` or `sales`.
4. Save.

This is the closet note. It is not on the JWT yet.

**B. Copy that note onto every new access token**

1. Open **Authentication → Token Claims**.
2. Click **Add custom claim**.
3. Claim name: `memory_project`.
4. Expression: `organization.metadata.memory_project ?? "agent"`.
5. Save.

Preview a token if the page allows it. You must see a **top-level** key
`memory_project`. Nested `organization.metadata` alone is not enough.

**C. Sign users in again**

Mint new user access tokens after step B. A token issued before the claim
will not contain `memory_project`. The gateway will then fall back to `agent`.

#### 2. This repo (`everos-scalekit`)

1. Use this mapping in `gateway.py` (this change). Do not read `client_id`
   for `project_id`.
2. Install and export credentials. Do **not** set `SCALEKIT_MOCK=1`.

```bash
pip install fastapi uvicorn httpx scalekit-sdk-python

export SCALEKIT_ENV_URL="https://<your-env>.scalekit.dev"
export SCALEKIT_CLIENT_ID="skc_..."
export SCALEKIT_CLIENT_SECRET="..."
# claim *name* to read — default is already memory_project
# export SCALEKIT_PROJECT_CLAIM="memory_project"
# folder if the claim is absent — default is already agent
# export EVEROS_PROJECT_ID="agent"

uvicorn gateway:app --port 8080
ALICE_TOKEN="<jwt>" BOB_TOKEN="<jwt>" python demo.py
```

`SCALEKIT_PROJECT_CLAIM` is the drawer name (`memory_project`). It is not the
folder (`support` / `sales`). Those values come from the token.

#### 3. Pass if

Decode the JWT (do not paste it). Then call the gateway with that bearer token.

| Who | Token has | Gateway `_scope.project_id` |
| --- | --- | --- |
| Alice | `"memory_project": "support"` | `support` |
| Bob | `"memory_project": "sales"` | `sales` |
| Claim missing | no `memory_project` key | `agent` (the fallback) |

If both people still get `agent`, Token Claims is off, or you reused an old
token. Fix the dashboard, mint again, then retry.

Scalekit's free tier is self-serve and includes unlimited dev environments.

## Status

The demo has run end to end against EverOS Cloud — the capture above is from
that run — and the scope rewrite is unit-verified for all four request shapes,
including the `agent_id` sidestep. `validate_access_token_and_get_claims` has
been run against a real Scalekit environment. User tokens carry `sub` and
`oid` as mapped above. They also carry `client_id`; that value is the app, not
the project, so this gateway no longer uses it for `project_id`.

## What this does not do yet

`gateway.py` covers `/api/v2/memory/*`. Knowledge endpoints
(`/api/v2/knowledge/*`) and OME triggers are not proxied. Scalekit tokens carry
`roles`, `permissions`, and `scope` claims that could gate read versus write —
this example ignores them and authorises any valid token for any memory
operation within its own scope.

## Learn more

- Scalekit access-token claims: https://docs.scalekit.com/guides/accesstoken-claims/
- Scalekit MCP auth (OAuth 2.1 for MCP servers): https://docs.scalekit.com/authenticate/mcp/overview
- EverOS API reference: https://github.com/EverMind-AI/EverOS/blob/main/docs/api.md
