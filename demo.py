"""The whole story, end to end: two tenants, one memory, one gateway.

Alice stores a memory and recalls it. Bob then asks the same question with
Alice's ``user_id`` in the request body — and gets nothing, because the gateway
replaced it with his own before EverOS ever saw the request.

Ordering matters for the proof: Bob only asks *after* Alice's recall has
succeeded. EverOS's index is eventually consistent, so an empty result before
that would prove nothing but index lag.

Standard library only — the interesting code is in ``gateway.py``.

    everos server start                                   # :8000
    SCALEKIT_MOCK=1 uvicorn gateway:app --port 8080        # :8080
    python demo.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")
MEMORY_ROOT = pathlib.Path(os.environ.get("EVEROS_ROOT", "~/.everos")).expanduser()

# In mock mode a token is just its claims: mock.<sub>.<oid>.<project_id>.
# With a real Scalekit environment, drop in two access tokens instead.
ALICE = os.environ.get("ALICE_TOKEN", "mock.usr_alice.org_acme.agent")
BOB = os.environ.get("BOB_TOKEN", "mock.usr_bob.org_globex.agent")

SESSION = f"demo-{int(time.time())}"
QUESTION = "what should I avoid at dinner?"
FACTS = [
    "I'm allergic to shellfish.",
    "I had a bad reaction to shrimp last summer.",
]


def call(action: str, token: str, **body: object) -> dict:
    request = urllib.request.Request(
        f"{GATEWAY}/api/v2/memory/{action}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:200]
        sys.exit(f"\n  {action} failed: HTTP {exc.code} — {detail}\n")
    except urllib.error.URLError as exc:
        sys.exit(f"\n  cannot reach the gateway at {GATEWAY}: {exc.reason}\n")


def scope_of(payload: dict) -> dict:
    """The gateway echoes the scope it derived, so the demo can show it."""
    return payload.get("data", payload).get("_scope") or payload.get("_scope", {})


def episode_files(scope: dict) -> list[pathlib.Path]:
    directory = (
        MEMORY_ROOT
        / scope["app_id"]
        / scope["project_id"]
        / "users"
        / scope["user_id"]
        / "episodes"
    )
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def rule(who: str, token: str) -> None:
    _, sub, oid, _ = token.split(".")
    label = f"  {who}  {sub} @ {oid} "
    print(f"\n{label}{'─' * max(0, 62 - len(label))}")


def recall(token: str, *, claim_user_id: str | None = None) -> list[dict]:
    """Search, retrying while the LanceDB index catches up with the markdown."""
    body: dict[str, object] = {"query": QUESTION, "top_k": 3}
    if claim_user_id:
        body["user_id"] = claim_user_id
    deadline = time.monotonic() + 90
    while True:
        payload = call("search", token, **body)
        episodes = payload.get("data", {}).get("episodes", [])
        if episodes or time.monotonic() > deadline:
            return episodes
        time.sleep(2)


def main() -> None:
    print(f"\n  gateway {GATEWAY}   session {SESSION}")

    # ── Alice writes, and the markdown lands synchronously ────────────────
    rule("Alice", ALICE)
    now = int(time.time() * 1000)
    added = call(
        "add",
        ALICE,
        session_id=SESSION,
        # EverOS Cloud defaults to async_mode=true (202 queued); an immediate
        # flush would race the queue. Synchronous add works on OSS and Cloud.
        async_mode=False,
        messages=[
            # sender_id says "alice" — the gateway overwrites it with the token's
            # subject, so a client cannot write into someone else's memory either.
            {
                "sender_id": "alice",
                "role": "user",
                "timestamp": now + i,
                "content": fact,
            }
            for i, fact in enumerate(FACTS)
        ],
    )
    scope = scope_of(added)
    for fact in FACTS:
        print(f"  → add     {fact}")

    flushed = call("flush", ALICE, session_id=SESSION)
    flush_status = flushed.get("data", {}).get("status", "?")
    print(f"  → flush   {flush_status}")
    on_disk = episode_files(scope)
    for path in on_disk:
        print(f"            {path}")

    episodes = recall(ALICE)
    print(f"  → search  {QUESTION!r}")
    if not episodes:
        sys.exit("\n  Alice could not recall her own memory — stopping.\n")
    for episode in episodes:
        # Matched facts when the index returns them; the episode's one-line
        # subject otherwise (EverOS Cloud nests facts differently than OSS).
        lines = [f["content"] for f in episode.get("atomic_facts") or []]
        if not lines and episode.get("subject"):
            lines = [episode["subject"]]
        if not lines:
            summary = episode.get("summary") or episode.get("episode") or ""
            lines = [summary[:70] + ("…" if len(summary) > 70 else "")]
        for line in lines:
            print(f"    OK    {line}")

    # ── Bob asks the same question, claiming to be Alice ──────────────────
    rule("Bob", BOB)
    print(f"  → search  {QUESTION!r}")
    print(f"            body claims user_id={scope['user_id']!r}")
    stolen = call("search", BOB, query=QUESTION, top_k=3, user_id=scope["user_id"])
    bob = scope_of(stolen)
    found = stolen.get("data", {}).get("episodes", [])
    outcome = f"LEAK  {len(found)} episodes" if found else "none  no results"
    print(f"    {outcome}")
    print(f"            gateway rewrote  user_id → {bob['user_id']}   (token sub)")
    print(f"                             app_id  → {bob['app_id']}   (token oid)")

    if found:
        verdict = "FAILED — Bob reached Alice's memory."
    elif on_disk:
        verdict = "Alice's memory is on disk. Bob's request never reached it."
    else:
        verdict = "Alice's memory persisted. Bob's request never reached it."
    print(f"\n  {verdict}\n")
    sys.exit(0 if not found else 1)


if __name__ == "__main__":
    main()
