# holographic-plus

A Hermes Agent memory provider plugin that extends the bundled `holographic` provider with automatic trait injection, write nudges, and compression-time fact rescue.

## What it does

The base `holographic` provider stores and retrieves facts on demand via the `fact_store` tool. holographic-plus adds five automatic behaviours on top:

| Behaviour | Mechanism | Frequency |
|---|---|---|
| **Trait injection** | Unconditionally injects all high-trust `user_pref` facts into context | Turn 1, then every `inject_interval` turns |
| **Topic retrieval** | HRR similarity search against the current message | Every turn |
| **Write nudge** | Instructs the model to store durable facts from recent turns | Every `write_nudge_interval` turns |
| **Consolidation nudge** | Instructs the model to deduplicate and resolve contradictions | Every `consolidate_nudge_interval` turns (never same turn as write nudge) |
| **Pre-compress rescue** | Injects a fact-extraction instruction before context compression discards old messages | On every compression event |

The key problem it solves: stable user traits (e.g. "prefers thorough root-cause investigation before acting") are semantically distant from most session-opening messages and never surface via similarity search alone. Trait injection fires unconditionally so working-style facts reach the model regardless of session topic.

## Requirements

- Hermes Agent with the bundled `holographic` memory provider present (`plugins/memory/holographic/`)
- No additional dependencies

## Installation

```bash
cd ~/.hermes/plugins
git clone https://github.com/YOUR_USERNAME/holographic-plus.git holographic-plus
```

Then add to `config.yaml`:

```yaml
memory:
  provider: holographic-plus

plugins:
  hermes-memory-store:
    trait_min_trust: 0.7        # trust floor for unconditional trait injection
    inject_interval: 3          # re-inject traits every N turns (0 = turn 1 only)
    write_nudge_interval: 7     # write nudge every N turns (0 = disabled)
    consolidate_nudge_interval: 10  # consolidation nudge every N turns (0 = disabled)
    mirror_memory_writes: true  # mirror MEMORY.md writes to fact_store
```

Restart the container. Confirm registration in agent.log:

```
INFO agent.memory_manager: Memory provider 'holographic-plus' registered (2 tools)
```

## Trust scores

Trait injection only fires for `user_pref` facts at or above `trait_min_trust` (default `0.7`). New facts start at `default_trust: 0.5`. Trust rises via:

- `fact_feedback(action='helpful')` — `+0.05` per call (automatic when facts are retrieved and rated)
- `fact_store(action='update', trust_delta=0.2)` — direct adjustment
- The [agent-dreaming-agnostic](https://github.com/nexus9888/hermes-memory-skills) skill — cross-session synthesis and trust elevation via cron

On a fresh fact bank, manually bump trust on your highest-signal `user_pref` facts to get trait injection working immediately.

## Inherited config keys

The following keys from the base `holographic` provider are still respected:

```yaml
plugins:
  hermes-memory-store:
    db_path: $HERMES_HOME/memory_store.db
    default_trust: 0.5
    min_trust_threshold: 0.3
    hrr_dim: 1024
    auto_extract: false
```

## How it works

holographic-plus subclasses `HolographicMemoryProvider` from the bundled plugin via a deferred import in `_make_class()`. Only seven methods are overridden - everything else (tool schemas, `handle_tool_call`, `system_prompt_block`, `save_config`) is inherited untouched. Upstream fixes to the base class are picked up automatically on image updates.

The subclass pattern was chosen over patching the bundled plugin in place because user plugins in `~/.hermes/plugins/` survive `docker compose pull && up` - the bundled plugin does not.
