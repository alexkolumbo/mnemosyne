#!/usr/bin/env python3
"""Hierarchical summary: when the answer needs MANY scattered facts, top-k retrieval
alone can only return k chunks and misses the rest; the running summary folded them
all in. 15 decisions (codewords DEC1..DEC15) are spread through a big context; we ask
for all of them, with shrink-only vs shrink+summary, and count how many come back.
Usage: poc-summary.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
MODEL = "moonshotai/Kimi-K2.6"
N = 15
FILLER = "Routine status note: telemetry nominal, channels within tolerance, archival cross-check complete. " * 22

parts = []
for n in range(1, N + 1):
    parts.append(f"Decision {n} (codeword DEC{n}): we agreed on item number {n}.")
    parts.append(FILLER)
transcript = "\n".join(parts)
msgs = [{"role": "system", "content": "Answer only from the provided context."},
        {"role": "user", "content": transcript},
        {"role": "user", "content": "List every decision codeword (the DEC-numbers) we have agreed on so far. Just the codewords."}]
approx = len(transcript) // 4


def ask(summary):
    body = json.dumps({"model": MODEL, "messages": msgs, "stream": False,
                       "temperature": 0, "max_tokens": 500}).encode()
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
         "X-Mnemosyne-Mode": "shrink"}
    if summary:
        h["X-Mnemosyne-Summary"] = "1"
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(GW, data=body, headers=h), timeout=600) as r:
        d = json.load(r)
    a = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    found = sum(1 for n in range(1, N + 1) if f"DEC{n}" in a.upper().replace(" ", ""))
    return found, a.strip(), time.time() - t0


print(f"context ~{approx:,} tokens, {N} decisions spread through it, top_k=10\n")
f1, a1, t1 = ask(False)
print(f"shrink only      : {f1}/{N} codewords  ({t1:.0f}s)")
f2, a2, t2 = ask(True)
print(f"shrink + summary : {f2}/{N} codewords  ({t2:.0f}s)")
print(f"\nshrink-only answer : {a1[:160]!r}")
print(f"with-summary answer: {a2[:200]!r}")
