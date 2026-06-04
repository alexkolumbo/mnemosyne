#!/usr/bin/env python3
"""Mnemosyne Phase 2 PoC — cross-session long-term memory.
Session A stores a fact (via X-Mnemosyne-Remember). A SEPARATE session B asks about
it — without recall the model can't know; WITH recall the gateway injects the fact
from the long-term store and the model answers. Proves memory BETWEEN sessions.
Usage: poc-memory.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
MODEL = "moonshotai/Kimi-K2.6"
NS = "alex_memtest"


def chat(content, extra):
    body = json.dumps({"model": MODEL, "stream": False, "temperature": 0, "max_tokens": 60,
                       "messages": [{"role": "system", "content": "You are concise."},
                                    {"role": "user", "content": content}]}).encode()
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
         "X-Mnemosyne-Namespace": NS}
    h.update(extra)
    req = urllib.request.Request(GW, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


print("Session A — store a fact (separate conversation):")
chat("Please remember for later: my favorite database is PostgreSQL and my lucky number is 4242.",
     {"X-Mnemosyne-Remember": "1"})
print("  fact stored to long-term memory.\n")
time.sleep(1)

q = "What is my favorite database and my lucky number?"
print("Session B (fresh conversation — the fact is NOT in these messages)")
print("  --- WITHOUT recall ---")
a1 = chat(q, {})
print("  answer:", repr(a1))
print("  --- WITH Mnemosyne recall ---")
a2 = chat(q, {"X-Mnemosyne-Recall": "1"})
print("  answer:", repr(a2))
ok = ("postgres" in a2.lower()) and ("4242" in a2)
print("\n  CROSS-SESSION RECALL:", "YES ✅" if ok else "NO ❌")
