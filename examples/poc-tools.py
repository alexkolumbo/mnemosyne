#!/usr/bin/env python3
"""Mnemosyne PoC #2 — tool-call-aware shrink.
Builds a long tool-using conversation (assistant tool_calls <-> tool results) with
a needle buried in a TOOL RESULT, then asks about it with shrink on. Success =
HTTP 200 (tool pairing preserved) + correct needle answer (retrieved from a tool msg).
Usage: poc-tools.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
MODEL = "moonshotai/Kimi-K2.6"
NEEDLE = "EAGLE-9921-DELTA"
N_GROUPS = 130
NEEDLE_AT = 90
PAD = "telemetry within tolerance; archival cross-check complete; redundant channels nominal; " * 6

messages = [{"role": "system", "content": "You are a precise assistant. Answer only from provided context."}]
for i in range(1, N_GROUPS + 1):
    messages.append({"role": "user", "content": f"Check subsystem {i}."})
    messages.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"call_{i}", "type": "function",
                                     "function": {"name": "read_sensor",
                                                  "arguments": json.dumps({"id": i})}}]})
    res = f"subsystem {i}: nominal; {PAD}"
    if i == NEEDLE_AT:
        res = f"subsystem {i}: nominal; IMPORTANT: the vault code is {NEEDLE}. {PAD}"
    messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": res})
messages.append({"role": "user", "content": "What is the vault code? Reply with ONLY the code."})

approx = sum(len(str(m.get("content") or "")) for m in messages) // 4
n_tool = sum(1 for m in messages if m.get("role") == "tool")
print(f"conversation: {len(messages)} messages, {n_tool} tool results, ~{approx:,} tokens; "
      f"needle in tool result #{NEEDLE_AT} = {NEEDLE}\n")

body = json.dumps({"model": MODEL, "messages": messages, "stream": False,
                   "temperature": 0, "max_tokens": 40}).encode()
headers = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
           "X-Mnemosyne-Mode": "shrink"}
req = urllib.request.Request(GW, data=body, headers=headers)
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    ans = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    u = d.get("usage") or {}
    print(f"HTTP 200 (tool pairing preserved ✅)  ({time.time()-t0:.1f}s)")
    print(f"  answer: {ans!r}")
    print(f"  usage:  {u}")
    print(f"  NEEDLE FOUND: {'YES ✅' if NEEDLE in ans else 'NO ❌'}")
except urllib.error.HTTPError as e:
    msg = e.read()[:300].decode("utf-8", "replace")
    print(f"HTTP {e.code} ❌  ({time.time()-t0:.1f}s)\n  {msg}")
    if "tool" in msg.lower():
        print("  -> looks like a tool-pairing break (shrink split an assistant/tool group)")
except Exception as e:
    print(f"FAILED: {e!r}")
