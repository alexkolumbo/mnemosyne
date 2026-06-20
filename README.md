# mnemosyne

Move the context window off the model and onto a server next to it.

The idea is simple. A model can only look at so many tokens at once, and that limit is baked into the model. But the thing your app actually knows can be much bigger than that. So instead of stuffing everything into the prompt and hoping it fits, you keep the full history and any long-term knowledge in a store of your own, and each turn you hand the model only the part that matters right now. The model still sees a normal-sized window. Your app behaves like it has a huge one.

This sits in front of an OpenAI-compatible endpoint the same way a normal proxy would. I built it against Gonka, but it doesn't care which provider or model is behind it.

It's part of [hermes-stack](https://github.com/alexkolumbo/hermes-stack), a one-script deploy that runs it together with Hermes and the output-cap proxy, though nothing here depends on that.

There are two pieces, on purpose, because state and behaviour are different jobs:

- a store, which just holds things. Conversations by id, and long-term facts by namespace. It can embed text and search it with vectors (fastembed + qdrant), and falls back to plain keyword search if the vector side isn't available.
- a gateway, which sits in the request path. When a request is about to blow past a token budget, it doesn't truncate blindly. It keeps the system prompt and the most recent messages as-is, pulls the most relevant older chunks back in, and drops the rest. It also pulls in relevant long-term memories from past sessions. Then it forwards a window that actually fits.

You attach it to any container that speaks OpenAI by pointing its `base_url` at the gateway. There's a script for that which doesn't assume anything about the target.

## why bother

Two things you can't do with a fixed window, that you can do with this:

A single conversation or document that's larger than the window. Long transcripts, big files, that kind of thing.

Memory that survives across sessions. You tell it something in one conversation, and weeks later in a completely different conversation it still knows. The model never had that fact in its context; the gateway recalled it and put it there.

Both of those are also, not coincidentally, the workloads that burn the most tokens, which is the point if you're trying to give a decentralised inference network real demand to chew on.

## what actually works right now

These aren't hypotheticals, they're the tests in `examples/`, run against Gonka.

A million-token haystack with one fact buried in it. Sent raw, the model times out, there's no way it fits. Through mnemosyne the model saw about 400 tokens, found the fact, and answered in roughly a second. The full million tokens stayed in the store.

A long tool-using conversation (a few hundred messages, assistant calls and tool results) with a fact hidden in one of the tool results. The tricky part here is that you can't just chop the middle out, because an assistant tool call and its matching tool result have to stay together or the API rejects the whole request. The gateway keeps those pairs intact, shrinks everything else, and still finds and answers from the buried result.

Cross-session memory with no shared words. Store "I'm really into PostgreSQL; 4242 holds special meaning for me." Then in a fresh conversation ask "which database do I prefer, and what's my lucky number?". Keyword search finds nothing, there's no overlap. Vector search finds it, and the model answers PostgreSQL and 4242.

## running it

You need docker, and qdrant + an embedder if you want the vector side (there's a script that brings both up).

```
./deploy-vectors.sh        # qdrant + the store with embeddings
./mnemosyne-attach.sh      # run the gateway and point your container at it
```

`mnemosyne-attach.sh` takes a target container, stands up the gateway and store on its network, and switches the target's `base_url` over. `mnemosyne-detach.sh` puts it back.

The interesting behaviour is opt-in per request via headers while it's still being hardened, so it won't touch traffic you don't ask it to:

- `X-Mnemosyne-Mode: shrink` to virtualise the window for that request
- `X-Mnemosyne-Summary: 1` to also fold the dropped context into a running summary and include it
- `X-Mnemosyne-Retrieve: vector|lexical` to pick the retriever for the shrink (vector is the default)
- `X-Mnemosyne-Remember: 1` to store the last user message as a long-term memory
- `X-Mnemosyne-Recall: 1` to pull relevant memories into the window
- `X-Mnemosyne-Namespace: <name>` to scope memory (per user, per project, whatever)

The `examples/*.py` scripts show each of these end to end.

## honest about the limits

The window the model sees on any single call is still fixed. What's unbounded is the state behind it; the gateway just decides what to show. So the quality of the whole thing comes down to retrieval, and retrieval is never perfect. Both the long-term memory and the window-shrink path use vector search now, and chunks get embedded into qdrant once and reused, so a chunk is never embedded twice and repeat shrinks on the same conversation come back in well under a second. On top of that there's a running summary of the dropped context, built incrementally (only new chunks get summarized, then folded into the existing summary under a size cap), which is what keeps the thread of a long conversation rather than just isolated facts. That matters when the answer depends on following the conversation: in one test a budget changed 100 to 150 to 120 to 135 with the stale 100 over-represented, and plain retrieval answered 150 while the summary path answered 135. Worth being clear about what each part is for: retrieval is for specific facts (turn up top_k if you need more of them), the summary is for the overall state. What's still left is turning all of this on automatically for live traffic by default; the machinery is there, it's just gated behind a header for now. None of that is hidden; it's the next set of things to do.

## license

MIT. See LICENSE.
