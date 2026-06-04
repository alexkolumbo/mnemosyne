#!/usr/bin/env python3
"""Mnemosyne PoC — needle in a ~1M-token haystack.
Sends a huge context to the gateway twice: WITHOUT shrink (should fail — too big
for the model) and WITH shrink (gateway retrieves the needle → small window →
model answers). Hits the gateway as a plain OpenAI client.
Usage: poc-1m.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
MODEL = "moonshotai/Kimi-K2.6"
NEEDLE = "BLUEBIRD-7741-ZULU"
TARGET_CHARS = 4_000_000   # ~1M tokens

# ── build haystack with a buried needle at ~70% depth ──────────────────
lines, chars, i = [], 0, 0
needle_at, inserted = int(TARGET_CHARS * 0.70), False
while chars < TARGET_CHARS:
    i += 1
    ln = (f"Log entry {i}: routine telemetry nominal across sector {i % 9}; thermal "
          f"margins within tolerance; subsystem checks completed; archival note {i} recorded.\n")
    lines.append(ln); chars += len(ln)
    if not inserted and chars >= needle_at:
        nl = f"IMPORTANT MEMO: the launch passphrase is {NEEDLE}. Keep it secret and exact.\n"
        lines.append(nl); chars += len(nl); inserted = True
haystack = "".join(lines)
question = "What is the launch passphrase? Reply with ONLY the passphrase, nothing else."
messages = [
    {"role": "system", "content": "You are a precise assistant. Answer only from the provided context."},
    {"role": "user", "content": haystack},
    {"role": "user", "content": question},
]
approx_tok = len(haystack) // 4
print(f"haystack: {len(haystack):,} chars (~{approx_tok:,} tokens), needle at ~70% = {NEEDLE}\n")


def call(shrink: bool, timeout: int):
    body = json.dumps({"model": MODEL, "messages": messages, "stream": False,
                       "temperature": 0, "max_tokens": 40}).encode()
    headers = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    if shrink:
        headers["X-Mnemosyne-Mode"] = "shrink"
    req = urllib.request.Request(GW, data=body, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        ans = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        u = d.get("usage") or {}
        return {"ok": True, "answer": ans, "usage": u, "t": time.time() - t0}
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code}: {e.read()[:200].decode('utf-8','replace')}", "t": time.time() - t0}
    except Exception as e:
        return {"ok": False, "err": repr(e), "t": time.time() - t0}


for label, shrink, tmo in [("WITHOUT shrink (raw 1M -> model)", False, 90),
                           ("WITH Mnemosyne shrink", True, 300)]:
    print(f"===== {label} =====")
    r = call(shrink, tmo)
    if r["ok"]:
        hit = NEEDLE in r["answer"]
        print(f"  answer: {r['answer']!r}")
        print(f"  usage:  {r['usage']}  ({r['t']:.1f}s)")
        print(f"  NEEDLE FOUND: {'YES ✅' if hit else 'NO ❌'}")
    else:
        print(f"  FAILED: {r['err']}  ({r['t']:.1f}s)")
    print()
