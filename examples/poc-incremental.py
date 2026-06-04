#!/usr/bin/env python3
"""Incremental indexing: the same vector-shrink request sent twice. The first call
embeds and indexes the chunks; the second reuses the qdrant index and only embeds
the query, so it should be much faster (and the gateway log shows indexed_new 38 -> 0).
Usage: poc-incremental.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
MODEL = "moonshotai/Kimi-K2.6"
CODE = "ZEPHYR-5500"

lines, chars, i, at, ins = [], 0, 0, int(60000 * 0.6), False
while chars < 60000:
    i += 1
    lines.append(f"Log entry {i}: routine telemetry nominal across sector {i % 9}; margins within tolerance; archival note {i}.\n")
    chars += len(lines[-1])
    if not ins and chars >= at:
        lines.append(f"Maintenance memo: the reactor activation code is {CODE}. Keep it secret.\n")
        chars += len(lines[-1]); ins = True
hay = "".join(lines)
msgs = [{"role": "system", "content": "Answer only from the provided context."},
        {"role": "user", "content": hay},
        {"role": "user", "content": "What do I enter to start the power plant? Give only the value."}]


def ask():
    body = json.dumps({"model": MODEL, "messages": msgs, "stream": False,
                       "temperature": 0, "max_tokens": 40}).encode()
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
         "X-Mnemosyne-Mode": "shrink"}
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(GW, data=body, headers=h), timeout=600) as r:
        d = json.load(r)
    a = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    return a, time.time() - t0


print(f"context ~{len(hay)//4:,} tokens; sending the same vector-shrink request twice\n")
a1, t1 = ask()
print(f"call 1 (cold, indexes chunks): {t1:5.1f}s  found={CODE in a1}")
a2, t2 = ask()
print(f"call 2 (warm, reuses index):   {t2:5.1f}s  found={CODE in a2}")
