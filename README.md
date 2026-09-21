# MemCo

Memory for edge-cloud agents. The edge keeps preferences and short-term notes. The cloud keeps cache, long-term notes, and distill samples. Training is out of band: MemCo writes samples and loads a snapshot path through an interface you implement.

## Install

```bash
pip install -e .
```

MQTT is optional: `pip install -e ".[mqtt]"`

Laya is an optional host adapter, not part of the memory kernel. MemCo runs without it. `pip install -e ".[laya]"` then tag turns and optionally gate `t0` / distill:

```python
from memco.laya import LayaGate, LayaHost

host = LayaHost()
flags = host.tag(user, agent)
edge.add(flags.turn(user, agent))
edge.recall(user, need=flags.need, pref=flags.pref, keyword=kw, referred=None)

Cloud(..., gate=LayaGate())
```

`flags.ok` is false if Laya is missing or a predict fails; flags then match `Turn` defaults and gates return `None`. High-confidence answers only (default 0.85). `LayaHost` and `LayaGate` share one loaded checkpoint per model name.

## Defaults

| Name | Default | Special |
| --- | --- | --- |
| `long_cap` | 1000 | `-1` no cap, no forget |
| `short_cap` | 50 | `-1` no cap, no upload |
| `recall_boost` | 1 | `0` no boost |
| `cache_ttl` | 86400 | `-1` no expiry |
| `cache_cap` | 1024 | `-1` no cap |
| `cache_fill` | 1.0 | `-1` no size eviction |
| `cache_policy` | `lru` | `lru` `lfu` `fifo` `random` `none` |
| `n_tiers` | 4 | `1` one tier |
| `forget_pct` | 20 | `0` no forget |
| `tier_pcts` | `(1, 3, 6, 10)` | must sum to `forget_pct` |
| `silence_sec` | 10 | `0` host calls `end_session` |
| `qos_recall` / `qos_archive` / `qos_snapshot` | 1 | `0` `1` `2` |
| `topic_prefix` | `memco` | |
| `shard_bytes` | 262144 | `0` no split |
| `net_timeout` | 5 | `-1` wait forever |
| `net_retries` | 3 | `-1` retry forever; `0` still tries once |
| `recall_limit` | 3 | `-1` no cut |
| `distill_limit` | 3 | `-1` all scarce buckets |

Within a tier, forget picks equal-weight random ids. Each tier deletes `min(ceil(cap * pct / 100), size(tier))`, using `long_cap` (or live N if cap is `-1`). Forget repeats until live N is below cap.

## Plug points

Implement these if you need them. Built-ins exist for tests.

- `Tokenizer` — pick one rare keyword
- `VectorIndex` — add / query / rebuild
- `Cache` — set / get / delete / clear
- `Teacher.complete(task, ctx)` — `t0` and `distill`; no vendor wire format
- `Router.route(event, payload)` — `recall.empty`, `tok.down`, `vec.down`, `gate.t0`, `gate.distill`, `session.end`. `None` is the default no-op
- `memco.laya` — optional. `LayaHost` fills `Turn.kind` / `unresolved` / `high_emotion` and recall `need` / `pref`. `LayaGate` implements `gate.t0` and `gate.distill`. The kernel does not import this module
- `Snapshot.load(path)` — apply a trained adapter; MemCo does not train
- `Backup.dump` / `restore` — not implemented; optional

## Flow

1. Host appends `Turn`s. Session ends after `silence_sec` idle (`edge.idle` / `edge.tick`) or a manual `end_session`.
2. End of session writes every short-term bucket from that session. Preferences stay still on the edge.
3. If live N has reached `short_cap`, the edge uploads. Success deletes those short-term live files.
4. If live N is above `short_cap` and the bus is down, extra live short-term items are deleted at random until N equals the cap.
5. Cloud ingest writes long-term, clears cache, appends distill jsonl, and preference text, then publishes `t0` back. Long-term forget also clears cache.
6. Recall: preferences (local) → short-term → cloud. A cache hit returns the stored ranked set and boosts matching long-term ids. A miss ranks long-term + vectors, then stores that set. A down bus skips cache and long-term. Empty recall is the only failure; a down bus is not.

Short-term and long-term buckets: `commitment` body is user+agent; `event` recall adds a few source lines; `feeling` body is user lines only.

## Test

```bash
pip install -e ".[dev]"
pytest
```
