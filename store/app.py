"""
Mnemosyne store — context state + long-term memory.

State: snapshots of full conversations by context_id (Phase 0).
Memory: cross-session facts by namespace, with VECTOR retrieval (fastembed +
qdrant) and a LEXICAL fallback if the vector backend is unavailable.

Endpoints:
  GET  /healthz
  POST /ingest                                   -> snapshot context state
  GET  /context/{id} | /context/{id}/turns | /contexts
  POST /memory/remember {namespace,text,meta?}   -> store a memory (embed+upsert)
  POST /memory/recall   {namespace,query,top_k,backend?}  -> retrieve (vector|lexical)
"""
import os
import re
import json
import uuid
import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DATA = os.environ.get("DATADIR", "/data")
MEMDIR = os.path.join(DATA, "_memory")
os.makedirs(MEMDIR, exist_ok=True)
app = FastAPI()

# ── vector backend (best-effort; lexical fallback if it fails) ──────────────
COLL = "mnemosyne_mem"      # long-term memory
CTX_COLL = "mnemosyne_ctx"  # conversation chunks for window-shrink (incrementally indexed)
DIM = 384
USE_VEC = False
_embed = _qc = _qmodels = None
try:
    from fastembed import TextEmbedding
    from qdrant_client import QdrantClient
    from qdrant_client import models as _qmodels
    _embed = TextEmbedding(model_name=os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
                           cache_dir=os.path.join(DATA, ".fastembed"))
    _qc = QdrantClient(url=os.environ.get("QDRANT_URL", "http://mnemosyne-qdrant:6333"), timeout=30)
    for _c in (COLL, CTX_COLL):
        try:
            _qc.get_collection(_c)
        except Exception:
            _qc.create_collection(_c, vectors_config=_qmodels.VectorParams(
                size=DIM, distance=_qmodels.Distance.COSINE))
    # context chunks are filtered by context_id a lot — index that payload field
    try:
        _qc.create_payload_index(CTX_COLL, field_name="context_id",
                                 field_schema=_qmodels.PayloadSchemaType.KEYWORD)
    except Exception:
        pass
    USE_VEC = True
    print("[store] vector backend READY (fastembed + qdrant)", flush=True)
except Exception as e:
    print(f"[store] vector backend unavailable -> lexical fallback: {e!r}", flush=True)

_STOP = set("the a an of to in on and or is are was were be for with as at by this that it "
            "its from i you my your me we our they what which who do does".split())


def _safe(cid) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(cid))[:128] or "default"


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _terms(s):
    t = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,}", str(s).lower())
    return [x for x in t if x not in _STOP]


def _vec(text):
    return list(_embed.embed([text]))[0].tolist()


# ─────────────────────────── context state ─────────────────────────────────
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "role": "mnemosyne-store",
            "memory_backend": "vector" if USE_VEC else "lexical", "data": DATA}


@app.post("/ingest")
async def ingest(request: Request):
    body = await request.json()
    cid = _safe(body.get("context_id", "default"))
    d = os.path.join(DATA, cid)
    os.makedirs(d, exist_ok=True)
    msgs = body.get("messages") or []
    meta = body.get("meta") or {}
    with open(os.path.join(d, "latest.json"), "w") as f:
        json.dump({"context_id": cid, "updated": _now(), "meta": meta, "messages": msgs},
                  f, ensure_ascii=False, indent=2)
    last = msgs[-1] if msgs else {}
    with open(os.path.join(d, "turns.jsonl"), "a") as f:
        f.write(json.dumps({"ts": _now(), "n_messages": len(msgs),
                            "approx_tokens": meta.get("approx_tokens"),
                            "model": meta.get("model"), "last_role": last.get("role")}) + "\n")
    return {"ok": True, "context_id": cid, "n_messages": len(msgs)}


@app.get("/context/{cid}")
async def get_context(cid: str):
    p = os.path.join(DATA, _safe(cid), "latest.json")
    if not os.path.exists(p):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(json.load(open(p)))


@app.get("/contexts")
async def list_contexts():
    out = []
    for name in sorted(os.listdir(DATA)):
        lp = os.path.join(DATA, name, "latest.json")
        if not name.startswith(".") and name != "_memory" and os.path.exists(lp):
            try:
                j = json.load(open(lp))
                out.append({"context_id": name, "updated": j.get("updated"),
                            "n_messages": len(j.get("messages") or [])})
            except Exception:
                pass
    return {"contexts": out, "count": len(out)}


# ─────────────────────── long-term memory ──────────────────────────────────
@app.post("/memory/remember")
async def remember(request: Request):
    b = await request.json()
    ns = _safe(b.get("namespace", "default"))
    text = (b.get("text") or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    rec = {"ts": _now(), "text": text, "meta": b.get("meta") or {}}
    # lexical backup copy (always)
    with open(os.path.join(MEMDIR, ns + ".jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    backend = "lexical"
    if USE_VEC:
        try:
            _qc.upsert(COLL, points=[_qmodels.PointStruct(
                id=str(uuid.uuid4()), vector=_vec(text),
                payload={"namespace": ns, "text": text, "ts": rec["ts"]})])
            backend = "vector"
        except Exception as e:
            print(f"[store] vector upsert failed: {e!r}", flush=True)
    return {"ok": True, "namespace": ns, "backend": backend}


@app.post("/memory/recall")
async def recall(request: Request):
    b = await request.json()
    ns = _safe(b.get("namespace", "default"))
    query = b.get("query") or ""
    top_k = int(b.get("top_k") or 5)
    want = (b.get("backend") or "auto").lower()   # auto|vector|lexical

    if want in ("auto", "vector") and USE_VEC:
        try:
            res = _qc.search(
                COLL, query_vector=_vec(query), limit=top_k,
                query_filter=_qmodels.Filter(must=[_qmodels.FieldCondition(
                    key="namespace", match=_qmodels.MatchValue(value=ns))]))
            return {"namespace": ns, "backend": "vector",
                    "items": [{"text": h.payload.get("text"), "score": round(h.score, 3),
                               "ts": h.payload.get("ts")} for h in res]}
        except Exception as e:
            print(f"[store] vector search failed -> lexical: {e!r}", flush=True)

    # lexical
    p = os.path.join(MEMDIR, ns + ".jsonl")
    if not os.path.exists(p):
        return {"namespace": ns, "backend": "lexical", "items": []}
    qt = _terms(query)
    scored = []
    for line in open(p):
        if not line.strip():
            continue
        rec = json.loads(line)
        tl = rec["text"].lower()
        s = sum(tl.count(t) * (1 + len(t) / 8.0) for t in qt)
        if s > 0:
            scored.append((s, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return {"namespace": ns, "backend": "lexical",
            "items": [{"text": r["text"], "score": round(s, 2)} for s, r in scored[:top_k]]}


def _chunk_id(context_id, text):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, context_id + "|" + text))


@app.post("/summary/get")
async def summary_get(request: Request):
    b = await request.json()
    cid = _safe(b.get("context_id", "default"))
    p = os.path.join(DATA, cid, "summary.json")
    if os.path.exists(p):
        return json.load(open(p))
    return {"summary": "", "summarized_ids": []}


@app.post("/summary/set")
async def summary_set(request: Request):
    b = await request.json()
    cid = _safe(b.get("context_id", "default"))
    d = os.path.join(DATA, cid)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "summary.json")
    cur = json.load(open(p)) if os.path.exists(p) else {"summary": "", "summarized_ids": []}
    cur["summary"] = b.get("summary", cur.get("summary", ""))
    ids = set(cur.get("summarized_ids") or [])
    ids.update(b.get("add_ids") or [])
    cur["summarized_ids"] = list(ids)
    json.dump(cur, open(p, "w"), ensure_ascii=False)
    return {"ok": True, "summary_len": len(cur["summary"]), "n_summarized": len(cur["summarized_ids"])}


@app.post("/shrink/select")
async def shrink_select(request: Request):
    """Window-shrink retrieval with INCREMENTAL indexing.
    Each chunk is embedded and stored in qdrant exactly once (deterministic id,
    deduped against what's already there — survives restarts). Only genuinely new
    chunks get embedded; the query is embedded once and qdrant does the search.
    Returns the top_k chunk texts (or backend=none so the gateway falls back)."""
    b = await request.json()
    context_id = _safe(b.get("context_id", "default"))
    query = b.get("query") or ""
    chunks = b.get("chunks") or []
    top_k = int(b.get("top_k") or 10)
    if not USE_VEC or not chunks:
        return {"backend": "none", "chunks": []}
    try:
        ids = [_chunk_id(context_id, c) for c in chunks]
        existing = set()
        try:
            got = _qc.retrieve(CTX_COLL, ids=ids, with_payload=False)
            existing = {str(p.id) for p in got}
        except Exception:
            pass
        new = [(i, c) for i, c in zip(ids, chunks) if i not in existing]
        if new:
            vecs = list(_embed.embed([c for _, c in new]))
            pts = [_qmodels.PointStruct(id=i, vector=v.tolist(),
                                        payload={"context_id": context_id, "text": c})
                   for (i, c), v in zip(new, vecs)]
            _qc.upsert(CTX_COLL, points=pts)
        res = _qc.search(
            CTX_COLL, query_vector=_vec(query), limit=top_k,
            query_filter=_qmodels.Filter(must=[_qmodels.FieldCondition(
                key="context_id", match=_qmodels.MatchValue(value=context_id))]))
        return {"backend": "vector", "chunks": [h.payload.get("text") for h in res],
                "indexed_new": len(new), "searched": len(chunks)}
    except Exception as e:
        print(f"[store] shrink_select failed: {e!r}", flush=True)
        return {"backend": "none", "chunks": []}
