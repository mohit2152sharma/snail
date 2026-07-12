# Concurrency Model

## Decision: standard GIL build (not free-threaded)

Voice is **I/O-bound**, so the standard GIL build is fine:

- The GIL is **released during I/O** (waiting on vendor WebSockets) and during
  **numpy/msgspec C calls** (audio ops, parsing). Most hot work is already
  effectively parallel.
- Free-threading (no-GIL, Python 3.13t+/3.14) was **considered and dropped** —
  bleeding-edge, C-extension compatibility risk, ~5–10% single-thread overhead.
  (Correction noted in discussion: "no-GIL" is NOT Python 3.10; 3.10 has the GIL.
  Free-threaded is a separate 3.13+ build.)

## The model

```
per worker PROCESS:  one asyncio event loop (uvloop), many sessions multiplexed
inside the loop:      single-threaded → registry stays LOCKLESS (serialized)
CPU-bound audio:      offload to threadpool → numpy/msgspec RELEASE the GIL
                      → real parallelism for the C parts even under GIL
scale across cores:   multiple worker processes (uvicorn/gunicorn workers)
                      each process = own loop + own pool + own sessions (shared-nothing)
```

Why this is the right call:

- **Lockless registry holds.** Single loop per process = single thread =
  serialized. The whole cancel/timeout atomic-resolution argument survives.
- **GIL doesn't bite.** I/O releases it; numpy/msgspec release it during the
  CPU-heavy audio/parse work.
- **One hard rule:** never do blocking CPU work **on** the loop — offload to
  `anyio.to_thread`. A slow numpy resample on the loop stalls every session in that
  worker.
  > **TODO(offload-threshold — see 09§E):** "offload CPU" needs a size threshold.
  > RNNoise/soxr on a single 10ms frame = microseconds; a thread hop costs more and
  > adds jitter. Inline small per-frame DSP on the loop; offload only heavy/batch
  > work (bulk resample, long replay serialize).
- **Proven deployment** — uvicorn workers, standard. No bleeding-edge risk.

## Async library: anyio + uvloop

```
anyio  = trio-STYLE structured-concurrency API on an asyncio backend
         → structured cancellation scopes (fit our sweep/timeout design)
         → native asyncio compat (vendor SDKs "just work")
uvloop = faster event loop (libuv), 2–4× stock asyncio I/O, drop-in
```

- **anyio is free with FastAPI** — FastAPI is built on Starlette, which uses anyio
  internally. If the server layer is FastAPI, anyio is already a transitive dep.
- **anyio + uvloop is literally the FastAPI production stack**
  (`uvicorn --loop uvloop`).

### Why not pure trio

trio has the best structured-concurrency/cancellation model, but **it is not
asyncio**. The vendor realtime SDKs (`google-genai`, `openai`) are asyncio-based;
running them under trio needs the `trio-asyncio` bridge → complexity + overhead +
sharp edges. anyio gives trio-style ergonomics **and** keeps asyncio compat, so we
don't have to fight our core dependencies. Straight asyncio + uvloop is the
fallback if we want zero extra deps and hand-rolled cancellation.
