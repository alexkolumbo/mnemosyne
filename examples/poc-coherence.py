#!/usr/bin/env python3
"""Coherence: a value that changes over time. The stale value ($100) is over-
represented so top-k retrieval surfaces it; the current value ($135) is the last
of several chronological changes. Retrieval alone tends to answer stale/ambiguous;
the running summary, which tracked the evolution, should give the current value.
Usage: poc-coherence.py <GONKA_API_KEY>
"""
import sys, json, time, urllib.request

KEY = sys.argv[1]
GW = "http://localhost:8781/v1/chat/completions"
MODEL = "moonshotai/Kimi-K2.6"
FILLER = "Routine status note: telemetry nominal, channels within tolerance, archival cross-check complete. " * 18

parts = []
for s in ["The project budget is $100.", "We confirmed the $100 budget with finance.",
          "Reminder: the budget stands at $100.", "All planning assumes the $100 budget.",
          "The $100 budget was approved at kickoff.", "Note: $100 is our working budget.",
          "Finance has logged the $100 budget.", "Everyone agreed on the $100 budget."]:
    parts += [s, FILLER]
parts += ["Update: the budget has been revised up to $150.", FILLER]
parts += ["Update: the budget was then reduced to $120.", FILLER]
parts += ["Update: the budget is now finalized at $135.", FILLER, FILLER]
transcript = "\n".join(parts)
msgs = [{"role": "system", "content": "Answer only from the provided context. (session BUDGET-COH-1)"},
        {"role": "user", "content": transcript},
        {"role": "user", "content": "What is our current budget? Reply with just the dollar amount."}]


def ask(summary):
    body = json.dumps({"model": MODEL, "messages": msgs, "stream": False,
                       "temperature": 0, "max_tokens": 40}).encode()
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
         "X-Mnemosyne-Mode": "shrink"}
    if summary:
        h["X-Mnemosyne-Summary"] = "1"
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(GW, data=body, headers=h), timeout=600) as r:
        d = json.load(r)
    a = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    return a, time.time() - t0


print("current (correct) budget = $135; stale $100 is over-represented\n")
a1, t1 = ask(False)
print(f"shrink only      : {a1!r:40}  correct={'135' in a1}  ({t1:.0f}s)")
a2, t2 = ask(True)
print(f"shrink + summary : {a2!r:40}  correct={'135' in a2}  ({t2:.0f}s)")
