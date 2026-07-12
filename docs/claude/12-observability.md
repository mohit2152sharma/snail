# Observability / Observer Layer

Operational telemetry — **metrics + events** — pluggable at any point in the
pipeline. For humans/analytics, not for the model.

## First: Observer ≠ Event Log (do not conflate)

```
Event LOG (01)        = CONVERSATION context. For the MODEL. Canonical, durable,
                        replayable, drives projections. LOAD-BEARING.
Observer layer (this) = OPERATIONAL telemetry. For HUMANS/analytics. Latency, health,
                        cost, drops, state transitions. FIRE-AND-FORGET.
```

Overlap events (handoff, tool_call) may feed **both**, but separate channels,
separate lifetimes. Log = load-bearing; observer = disposable. **Routing and context
must NEVER depend on an observer.** An observer can be absent, slow, or crash without
affecting a single conversation.

## Two payload kinds

```
Metric  : name, kind(counter|gauge|histogram|timer), value, tags{}   ← numeric
Event   : name, ts, attrs{}                                         ← structured record
```

Both are msgspec structs; built only when an observer is attached (see zero-cost).

## Instrumentation points (tap anywhere — all subsystems)

```
audio      frame_in/out, resample_us, clean_us, queue_depth, drop_count
router     decision, handoff, promote, demote, seam_us, gate_transfer
tool       call_received, call_resolved, exec_us, status_counts
pool       connect_us, recycle, evict, warm_count, pool_size
connection vendor_rtt, goaway, reconnect
session    turn_count, cost, barge_in, first_audio_latency
```

List is the starting set; instrumentation points are additive (new hook = new emit
call, observers opt in by name).

## Interface (mirrors AudioSink)

```
Observer (Protocol):
   on_metric(metric) -> None
   on_event(event)   -> None

rules:
   - N observers, fan-out.
   - OFF hot path: emit → observer ring → observer's own task drains.
   - OBSERVE never mutate/gate — telemetry cannot affect behavior.
   - bounded ring, drop-oldest → a slow observer never stalls the pipeline.
   - NULL default: no observer → emit is a guarded no-op, ZERO cost.
```

## Zero-cost emission (perf-critical — density goal)

Per-frame instrumentation must cost ~nothing when nobody listens:

```
1. GUARD before build:   if observers: emit(...)    ← single bool/empty check, no alloc
2. Don't build the Metric/Event struct unless a subscriber exists.
3. No string formatting on the hot path — pre-resolved tags.
```

## Two emission modes (else the ring swamps)

Per-frame push = thousands/sec = death. Split by frequency:

```
PUSH     rare structured events (handoff, error, recycle, barge_in, goaway)
         → emit immediately.
SAMPLED  high-freq metrics (per-frame latency, queue depth, drop_count)
         → accumulate LOCALLY (lock-free counter/histogram, single loop),
           flush to observers on a timer/interval.
         → observers see AGGREGATES, not every frame.
```

## Relationship to AudioSink (11)

```
AudioSink  = heavy binary payload (pooled int16 frames). Audio-specific.
Observer   = light numeric/structured telemetry. Framework-wide.
```
Same philosophy (tap anywhere · off hot path · observe-never-gate · null=zero-cost),
different payload → **separate interfaces**. Never route audio bytes through the
metric channel.

## Built-in observers (ship)

```
LoggingObserver    structured stdout / log lines
ExporterObserver   prometheus / statsd / OTLP
InMemoryObserver   tests + replay
NullObserver       default
```

## LOCKED

- Observer layer = operational telemetry, **never load-bearing**; distinct from the
  event log (01).
- Payload = **Metric | Event** (msgspec structs).
- Named instrumentation points across all subsystems; additive.
- `Observer.on_metric / on_event`; N observers, fan-out, off hot path, observe never
  gate, bounded/drop-oldest, null default.
- **Zero-cost guarded emission** (build nothing when no observer).
- **PUSH (rare events) vs SAMPLED-aggregate (hot metrics)** split.
- Built-ins: Logging / Exporter / InMemory / Null.
