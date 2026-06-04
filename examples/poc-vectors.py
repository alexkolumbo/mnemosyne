#!/usr/bin/env python3
"""Mnemosyne Phase 2 — VECTOR vs LEXICAL recall.
Stores a fact phrased WITHOUT the query's keywords (zero lexical overlap), then
recalls with both backends: lexical misses, vector finds it semantically; finally
end-to-end through the gateway the model answers from the recalled memory.
Usage: poc-vectors.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
STORE = "http://localhost:8782"
MODEL = "moonshotai/Kimi-K2.6"
NS = "alex_vectest"


def post(url, obj, headers=None):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def chat(content, extra):
    d = post(GW, {"model": MODEL, "stream": False, "temperature": 0, "max_tokens": 60,
                  "messages": [{"role": "system", "content": "You are concise."},
                               {"role": "user", "content": content}]},
             {"Authorization": "Bearer " + KEY, "X-Mnemosyne-Namespace": NS, **extra})
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


fact = "I'm really into PostgreSQL; 4242 holds special meaning for me."
query = "Which database do I prefer, and what is my lucky number?"
chat(fact, {"X-Mnemosyne-Remember": "1"})
print(f"stored fact : {fact!r}")
print(f"query       : {query!r}   (note: NO shared keywords with the fact)\n")
time.sleep(1)

lex = post(STORE + "/memory/recall", {"namespace": NS, "query": query, "backend": "lexical", "top_k": 3})
vec = post(STORE + "/memory/recall", {"namespace": NS, "query": query, "backend": "vector", "top_k": 3})
print("LEXICAL recall:", [i["text"] for i in lex.get("items", [])] or "—  (miss, 0 overlap)")
print("VECTOR  recall:", [(i["text"], i.get("score")) for i in vec.get("items", [])] or "—")
print()

ans = chat(query, {"X-Mnemosyne-Recall": "1"})
print("model answer (via vector recall):", repr(ans))
ok = ("postgres" in ans.lower()) and ("4242" in ans)
print("SEMANTIC CROSS-SESSION RECALL:", "YES ✅" if ok else "NO ❌")
