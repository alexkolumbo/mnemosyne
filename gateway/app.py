"""
Mnemosyne gateway — generic OpenAI-compatible context middleware.

Default = OBSERVER (Phase 0): measure + snapshot to store + forward 1:1.
Opt-in = SHRINK (Phase 1-lite, PoC): when a request carries header
`X-Mnemosyne-Mode: shrink`, the gateway virtualizes the window — keeps system +
a verbatim recent tail, RETRIEVES the most relevant chunks of the (huge) history
for the latest query, and forwards a small window the model can actually ingest.
The FULL original is still snapshotted to the store (state is never lost).

PoC retrieval = lexical (term-overlap). Production swap = vector embeddings
(qdrant/pgvector + an embed service). The architecture is identical; only the
scorer changes.

Chain: client -> mnemosyne-gateway -> <upstream> -> provider.
"""
import os
import re
import json
import hashlib
import datetime

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

UPSTREAM = os.environ.get("UPSTREAM_BASE_URL", "http://prometheus-proxy:8780").rstrip("/")
STORE = os.environ.get("STORE_URL", "http://mnemosyne-store:8782").rstrip("/")
PORT = int(os.environ.get("PORT", "8781"))
LOGDIR = os.environ.get("LOGDIR", "/log")
# Phase-1 shrink params (tunable via env)
BUDGET_TOKENS = int(os.environ.get("SHRINK_BUDGET_TOKENS", "12000"))
TAIL_TOKENS = int(os.environ.get("SHRINK_TAIL_TOKENS", "2000"))  # verbatim recent tail budget
SHRINK_AUTO = os.environ.get("SHRINK_AUTO", "0") == "1"          # auto-shrink live traffic (default OFF)
AUTO_BUDGET = int(os.environ.get("SHRINK_AUTO_BUDGET", "60000")) # only auto-shrink above this many tokens
TOP_K = int(os.environ.get("SHRINK_TOP_K", "10"))
CHUNK_CHARS = int(os.environ.get("SHRINK_CHUNK_CHARS", "1600"))
os.makedirs(LOGDIR, exist_ok=True)

app = FastAPI()
client = httpx.AsyncClient(timeout=httpx.Timeout(None))

HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "accept-encoding",
}
_last_tokens: dict = {}
_STOP = set("the a an of to in on and or is are was were be been being for with as at by "
            "this that these those it its from will would can could should i you he she we "
            "they what which who whom your my our their about into than then over under".split())


def log(msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOGDIR, "gateway.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fwd_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP}


def _text_of(m) -> str:
    parts = []
    c = m.get("content")
    if isinstance(c, str):
        parts.append(c)
    elif isinstance(c, list):
        parts.append(" ".join(str(p.get("text", "")) for p in c if isinstance(p, dict)))
    for tc in (m.get("tool_calls") or []):
        fn = tc.get("function") or {}
        parts.append(f"{fn.get('name', '')} {fn.get('arguments', '')}")
    return " ".join(p for p in parts if p)


def approx_tokens_msgs(messages) -> int:
    total = 0
    for m in messages:
        total += len(_text_of(m))
        for tc in (m.get("tool_calls") or []):
            total += len(((tc.get("function") or {}).get("arguments") or ""))
    return total // 4


def derive_context_id(data, headers) -> str:
    hdr = headers.get("x-context-id")
    if hdr:
        return "ctx_" + hashlib.sha1(hdr.encode()).hexdigest()[:16]
    msgs = data.get("messages") or []
    sys_c = next((_text_of(m) for m in msgs if m.get("role") == "system"), "")
    first_u = next((_text_of(m) for m in msgs if m.get("role") == "user"), "")
    basis = (sys_c[:600] + "||" + first_u[:600]) or "default"
    return "ctx_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def _query_terms(q: str):
    terms = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-]{2,}", q.lower())
    return [t for t in dict.fromkeys(terms) if t not in _STOP]


def prepare_shrink(messages):
    """Split into system + verbatim tail + chunked bulk. Returns a dict, or None
    if there's nothing to shrink. Tail is token-budgeted and tool-pair safe."""
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    if not non_system:
        return None
    tail, bud = [], 0
    for m in reversed(non_system):
        t = approx_tokens_msgs([m])
        if tail and bud + t > TAIL_TOKENS:
            break
        tail.insert(0, m); bud += t
    bulk = list(non_system[:len(non_system) - len(tail)])
    # never start the tail mid tool-group (would split assistant(tool_calls)<->tool)
    while bulk and tail and tail[0].get("role") == "tool":
        tail.insert(0, bulk.pop())
    if not bulk:
        return None
    query = ""
    for m in reversed(tail):
        if m.get("role") == "user":
            query = _text_of(m); break
    if not query:
        query = _text_of(tail[-1])
    bulk_text = "\n".join(_text_of(m) for m in bulk)
    chunks = [bulk_text[i:i + CHUNK_CHARS] for i in range(0, len(bulk_text), CHUNK_CHARS)]
    return {"system": system, "tail": tail, "chunks": chunks, "query": query}


def lexical_top(chunks, query, k):
    """Top-k chunk indices by term overlap (fallback retriever)."""
    terms = _query_terms(query)
    scored = []
    for idx, ch in enumerate(chunks):
        cl = ch.lower()
        s = sum(cl.count(t) * (1 + len(t) / 8.0) for t in terms)
        if s > 0:
            scored.append((s, idx))
    scored.sort(reverse=True)
    return [i for _, i in scored[:k]]


def assemble_shrink(system, selected_chunks, tail):
    retrieved = "\n\n---\n".join(selected_chunks)
    recall_msg = {
        "role": "system",
        "content": (
            "[Mnemosyne] The full conversation/context is too large for the window and "
            "is stored externally. Below are the MOST RELEVANT retrieved excerpts for the "
            "current question. Treat them as authoritative context:\n\n"
            f"{retrieved}\n\n[End of retrieved context]"
        ),
    }
    return system + [recall_msg] + tail


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "role": "mnemosyne-gateway", "phase": "0+shrink",
            "upstream": UPSTREAM, "store": STORE, "budget_tokens": BUDGET_TOKENS}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    body = await request.body()
    is_chat = request.method == "POST" and path.endswith("chat/completions")
    stream_req = False
    mode = request.headers.get("x-mnemosyne-mode", "observe").lower()

    if is_chat and body:
        try:
            data = json.loads(body)
            stream_req = bool(data.get("stream"))
            cid = derive_context_id(data, request.headers)
            msgs = data.get("messages") or []
            ntok = approx_tokens_msgs(msgs)
            prev = _last_tokens.get(cid)
            delta = (ntok - prev) if prev is not None else None
            _last_tokens[cid] = ntok
            # Auto-trigger: shrink live traffic only when WAY over budget (opt-in).
            if mode != "shrink" and SHRINK_AUTO and ntok > AUTO_BUDGET:
                mode = "shrink"
                log(f"AUTO-SHRINK ctx={cid} ~tokens={ntok} > {AUTO_BUDGET}")
            log(f"OBSERVE ctx={cid} msgs={len(msgs)} ~tokens={ntok} delta={delta} "
                f"mode={mode} model={data.get('model')} stream={stream_req}")
            # snapshot the FULL original into the store (state never lost)
            try:
                await client.post(f"{STORE}/ingest", timeout=60, json={
                    "context_id": cid, "messages": msgs,
                    "meta": {"model": data.get("model"), "approx_tokens": ntok,
                             "n_tools": len(data.get("tools") or []), "mode": mode},
                })
            except Exception as e:
                log(f"store ingest failed: {e!r}")

            # ── LONG-TERM MEMORY (opt-in via headers) ──────────────────
            ns = re.sub(r"[^A-Za-z0-9_.-]", "_",
                        request.headers.get("x-mnemosyne-namespace", "default"))[:128]
            last_user = next((_text_of(m) for m in reversed(msgs)
                              if m.get("role") == "user"), "")
            if request.headers.get("x-mnemosyne-remember") == "1" and last_user:
                try:
                    await client.post(f"{STORE}/memory/remember", timeout=20,
                                      json={"namespace": ns, "text": last_user})
                    log(f"REMEMBER ns={ns} +1 ({len(last_user)} chars)")
                except Exception as e:
                    log(f"remember failed: {e!r}")
            if request.headers.get("x-mnemosyne-recall") == "1" and last_user:
                items = []
                try:
                    rr = await client.post(f"{STORE}/memory/recall", timeout=20,
                                           json={"namespace": ns, "query": last_user, "top_k": 5})
                    items = (rr.json() or {}).get("items") or []
                except Exception as e:
                    log(f"recall failed: {e!r}")
                if items:
                    mem = "\n- ".join(it["text"] for it in items)
                    recall_msg = {"role": "system",
                                  "content": "[Mnemosyne long-term memory] Facts the user "
                                             "shared in PAST sessions (treat as known):\n- " + mem}
                    sys_n = 0
                    for m in msgs:
                        if m.get("role") == "system":
                            sys_n += 1
                        else:
                            break
                    data["messages"] = msgs[:sys_n] + [recall_msg] + msgs[sys_n:]
                    msgs = data["messages"]
                    body = json.dumps(data).encode("utf-8")
                    log(f"RECALL ns={ns} injected {len(items)} memories")

            # ── SHRINK (opt-in) ────────────────────────────────────────
            if mode == "shrink":
                prep = prepare_shrink(msgs)
                if not prep:
                    log(f"SHRINK ctx={cid} skipped (nothing to shrink)")
                else:
                    chunks, query = prep["chunks"], prep["query"]
                    retrieve = request.headers.get("x-mnemosyne-retrieve", "vector").lower()
                    selected, method, indexed_new = None, None, None
                    if retrieve != "lexical":
                        try:
                            rr = await client.post(f"{STORE}/shrink/select", timeout=600,
                                                   json={"context_id": cid, "query": query,
                                                         "chunks": chunks, "top_k": TOP_K})
                            j = rr.json()
                            if j.get("backend") == "vector":
                                selected = j.get("chunks") or []
                                method, indexed_new = "vector", j.get("indexed_new")
                        except Exception as e:
                            log(f"vector select failed -> lexical: {e!r}")
                    if selected is None:
                        idxs = sorted(set(lexical_top(chunks, query, TOP_K)))
                        selected, method = [chunks[i] for i in idxs], "lexical"
                    new_msgs = assemble_shrink(prep["system"], selected, prep["tail"])
                    data["messages"] = new_msgs
                    body = json.dumps(data).encode("utf-8")
                    new_tok = approx_tokens_msgs(new_msgs)
                    log(f"SHRINK ctx={cid} {ntok} -> {new_tok} tokens | method={method} "
                        f"chunks={len(chunks)} kept={len(selected)} indexed_new={indexed_new}")
        except Exception as e:
            log(f"observe/shrink error: {e!r}")
    else:
        log(f"PASS {request.method} /{path}")

    # ── forward (stream-aware) ─────────────────────────────────────────
    url = f"{UPSTREAM}/{path}"
    headers = fwd_headers(request.headers)
    if stream_req:
        upstream_req = client.build_request(request.method, url, headers=headers, content=body)
        r = await client.send(upstream_req, stream=True)

        async def gen():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()

        return StreamingResponse(gen(), status_code=r.status_code,
                                 media_type=r.headers.get("content-type", "text/event-stream"))

    r = await client.request(request.method, url, headers=headers, content=body)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))
