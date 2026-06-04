#!/usr/bin/env python3
"""Vectors in the window-shrink path: lexical vs vector retrieval, same shrink.
A fact is buried in a big context; the question is paraphrased so it shares no
words with the fact. Lexical shrink should miss it; vector shrink should find it.
Usage: poc-shrink-vectors.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
MODEL = "moonshotai/Kimi-K2.6"
CODE = "ZEPHYR-5500"

lines, chars, i, at, ins = [], 0, 0, int(60000 * 0.6), False
while chars < 60000:
    i += 1
    ln = f"Log entry {i}: routine telemetry nominal across sector {i % 9}; margins within tolerance; archival note {i}.\n"
    lines.append(ln); chars += len(ln)
    if not ins and chars >= at:
        nl = f"Maintenance memo: the reactor activation code is {CODE}. Keep it secret.\n"
        lines.append(nl); chars += len(nl); ins = True
hay = "".join(lines)
q = "What do I enter to start the power plant? Give only the value."
msgs = [{"role": "system", "content": "Answer only from the provided context."},
        {"role": "user", "content": hay},
        {"role": "user", "content": q}]


def ask(retrieve):
    body = json.dumps({"model": MODEL, "messages": msgs, "stream": False,
                       "temperature": 0, "max_tokens": 40}).encode()
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
         "X-Mnemosyne-Mode": "shrink", "X-Mnemosyne-Retrieve": retrieve}
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(GW, data=body, headers=h), timeout=240) as r:
        d = json.load(r)
    a = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    return a, time.time() - t0


print(f"haystack ~{len(hay)//4:,} tokens; buried code {CODE}")
print(f"question: {q!r}   (shares no words with the memo)\n")
for mode in ["lexical", "vector"]:
    a, t = ask(mode)
    print(f"[{mode:7s}] {'FOUND' if CODE in a else 'miss '}  ({t:.1f}s)  answer={a!r}")
