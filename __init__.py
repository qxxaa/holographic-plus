"""holographic-plus — Extended holographic memory provider.

True subclass of the bundled HolographicMemoryProvider. Only the seven
methods listed below are overridden. Everything else — tool schemas,
handle_tool_call, system_prompt_block, save_config, is_available, name —
is inherited untouched. Upstream fixes to the base class are inherited
automatically on image updates.

Adds:

  1. Periodic trait injection   — every inject_interval turns, unconditionally
                                  injects all high-trust user_pref facts.
                                  Also fires at turn 1.

  2. Write nudge                — every write_nudge_interval turns, appends a
                                  <memory-task> block instructing the model to
                                  call fact_store(action='add') for durable
                                  facts from the current session.

  3. Consolidation nudge        — every consolidate_nudge_interval turns, appends
                                  a heavier <memory-task> block for bank hygiene.
                                  Never fires on the same turn as the write nudge.

  4. on_pre_compress            — injects a fact-rescue instruction into the
                                  compression prompt before context is discarded.

  5. on_memory_write mirror     — when the native Hermes background review writes
                                  to MEMORY.md, mirrors the write to fact_store
                                  (zero extra compliance dependency).

  6. on_session_switch          — resets turn counter on genuine new sessions only.
                                  Preserved across /resume, /branch, compression.

  7. Turn counting              — via on_turn_start(). Reliable because WebUI caches
                                  the AIAgent per session (SESSION_AGENT_CACHE,
                                  merged in hermes-webui v0.50.210).

Config (under plugins.hermes-memory-store in config.yaml):

  # Inherited from holographic (unchanged defaults)
  db_path: $HERMES_HOME/memory_store.db
  default_trust: 0.5
  min_trust_threshold: 0.3
  hrr_dim: 1024
  auto_extract: false

  # New keys (all optional — defaults shown)
  trait_min_trust: 0.7            # trust floor for unconditional user_pref injection
  inject_interval: 20             # re-inject user_pref traits every N turns (0 = turn 1 only)
  write_nudge_interval: 10        # append write nudge every N turns (0 = disabled)
  consolidate_nudge_interval: 30  # append consolidation nudge every N turns (0 = disabled)
  mirror_memory_writes: true      # mirror MEMORY.md writes to fact_store automatically

Activation:
  memory:
    provider: holographic-plus
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nudge templates
# Framed as system-level tasks to maximise model compliance weight.
# The <memory-task> tag is distinct from the <memory-context> wrapper that
# build_memory_context_block() adds, so the model can distinguish retrieved
# facts from actionable instructions.
# ---------------------------------------------------------------------------

_WRITE_NUDGE = """\
<memory-task>
[MEMORY TASK — complete before replying to the user]
Review the recent turns of this conversation. Call fact_store(action='add') for \
any durable facts not yet stored:
  • category='user_pref'  — preferences, working style, standing instructions
  • category='project'    — technical decisions, architecture choices, rationale
  • category='tool'       — service quirks, config findings, tool-specific patterns
Do not duplicate facts already in the bank. If nothing new warrants storage, \
proceed immediately without calling fact_store.
</memory-task>"""

_CONSOLIDATION_NUDGE = """\
<memory-task>
[MEMORY CONSOLIDATION TASK — complete before replying to the user]
Perform a lightweight consolidation pass on the fact bank:
  1. Call fact_store(action='contradict') to surface conflicting facts.
     Resolve via fact_store(action='update') or fact_store(action='remove').
  2. Call fact_store(action='list', limit=30) to identify near-duplicates.
     Merge via fact_store(action='update') on the better entry and
     fact_store(action='remove') on the redundant one.
  3. For clearly stale or superseded facts, call
     fact_feedback(action='unhelpful', fact_id=N) to decay their trust score.
If the bank is already clean, proceed immediately without calling any tools.
</memory-task>"""


# ---------------------------------------------------------------------------
# HolographicPlusProvider
# ---------------------------------------------------------------------------

class HolographicPlusProvider:
    """Holographic-plus: extended holographic memory via subclassing.

    Loaded dynamically at class-definition time so the bundled holographic
    plugin path is always resolved from the live sys.path rather than
    captured at import time — safe for both CLI and gateway contexts.
    """

    # The actual class is built in _make_class() below and assigned to
    # this name after the module loads. register() uses it directly.


def _make_class():
    """Build HolographicPlusProvider as a true subclass of HolographicMemoryProvider.

    Deferred so the import of the bundled plugin happens at register() time,
    when the plugins/memory/ directory is guaranteed to be on sys.path.
    """
    from plugins.memory.holographic import HolographicMemoryProvider

    class HolographicPlusProvider(HolographicMemoryProvider):
        """Extended holographic memory provider — see module docstring."""

        @property
        def name(self) -> str:
            return "holographic-plus"

        def __init__(self, config: dict | None = None):
            # Load config the same way the base class does, then pass it up.
            if config is None:
                config = _load_plugin_config()
            super().__init__(config=config)

            # New config keys — read after super().__init__ so self._config exists.
            self._trait_min_trust = float(self._config.get("trait_min_trust", 0.7))
            self._inject_interval = int(self._config.get("inject_interval", 20))
            self._write_nudge_interval = int(self._config.get("write_nudge_interval", 10))
            self._consolidate_nudge_interval = int(
                self._config.get("consolidate_nudge_interval", 30)
            )
            self._mirror_memory_writes_plus = bool(
                self._config.get("mirror_memory_writes", True)
            )

            # Turn counter — persists across WebUI messages via SESSION_AGENT_CACHE.
            self._turn_count = 0

        # -- Lifecycle -------------------------------------------------------

        def initialize(self, session_id: str, **kwargs) -> None:
            super().initialize(session_id, **kwargs)
            # reset counter on every fresh initialize() call
            self._turn_count = 0

        def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
            """Increment turn counter each turn.

            Reliable because hermes-webui v0.50.210+ caches the AIAgent
            instance per session (SESSION_AGENT_CACHE), so this counter
            accumulates correctly across messages within a session.
            """
            self._turn_count += 1

        def on_session_switch(
            self,
            new_session_id: str,
            *,
            parent_session_id: str = "",
            reset: bool = False,
            **kwargs,
        ) -> None:
            """Update session ID; reset counter only on genuine new sessions.

            reset=True  → /new, /reset — start counting from zero.
            reset=False → /resume, /branch, compression — preserve the count.
            """
            if reset:
                self._turn_count = 0
            super().on_session_switch(
                new_session_id,
                parent_session_id=parent_session_id,
                reset=reset,
                **kwargs,
            )

        # -- Core prefetch ---------------------------------------------------

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            """Dual-pass prefetch with periodic trait injection and nudge blocks.

            Pass 2 (trait injection) fires first so stable traits are prepended
            above topic-matched results — highest-priority signal at the top.

            Pass 1 (HRR topic search) runs second and dedupes against traits
            already injected by Pass 2.

            Write nudge and consolidation nudge are appended after all memory
            context as separate <memory-task> blocks. They never fire on the
            same turn — consolidation only fires when write nudge does not.
            """
            if not self._retriever or not query:
                return ""

            blocks: list[str] = []
            seen_ids: set = set()

            # ── Pass 2: unconditional trait injection ──────────────────────
            _should_inject = self._turn_count == 1 or (
                self._inject_interval > 0
                and self._turn_count > 0
                and self._turn_count % self._inject_interval == 0
            )
            if _should_inject and self._store:
                try:
                    traits = self._store.list_facts(
                        category="user_pref",
                        min_trust=self._trait_min_trust,
                        limit=20,
                    )
                    if traits:
                        trait_lines = []
                        for t in traits:
                            seen_ids.add(t.get("fact_id"))
                            trust = t.get("trust_score", 0)
                            trait_lines.append(
                                f"- [{trust:.1f}] {t.get('content', '')}"
                            )
                        blocks.append(
                            "## Stable User Traits\n" + "\n".join(trait_lines)
                        )
                except Exception as e:
                    logger.debug("holographic-plus trait injection failed: %s", e)

            # ── Pass 1: HRR topic search ───────────────────────────────────
            try:
                results = self._retriever.search(
                    query,
                    min_trust=self._min_trust,
                    limit=5,
                )
                if results:
                    topic_lines = []
                    for r in results:
                        if r.get("fact_id") in seen_ids:
                            continue  # already present in trait injection
                        trust = r.get("trust_score", r.get("trust", 0))
                        topic_lines.append(f"- [{trust:.1f}] {r.get('content', '')}")
                    if topic_lines:
                        blocks.append(
                            "## Relevant Memory\n" + "\n".join(topic_lines)
                        )
            except Exception as e:
                logger.debug("holographic-plus HRR search failed: %s", e)

            memory_context = ""
            if blocks:
                memory_context = "## Holographic Memory\n" + "\n\n".join(blocks)

            # ── Write nudge ────────────────────────────────────────────────
            nudge_block = ""
            _write_fires = (
                self._write_nudge_interval > 0
                and self._turn_count > 0
                and self._turn_count % self._write_nudge_interval == 0
            )
            if _write_fires:
                nudge_block = _WRITE_NUDGE

            # ── Consolidation nudge (never fires same turn as write nudge) ─
            elif (
                self._consolidate_nudge_interval > 0
                and self._turn_count > 0
                and self._turn_count % self._consolidate_nudge_interval == 0
            ):
                nudge_block = _CONSOLIDATION_NUDGE

            parts = [p for p in [memory_context, nudge_block] if p]
            result = "\n\n".join(parts)
            if result:
                logger.info(
                    "holographic-plus prefetch turn=%d chars=%d traits=%d topic=%d nudge=%s",
                    self._turn_count,
                    len(result),
                    len([l for l in memory_context.splitlines() if l.startswith("- [")]) if memory_context else 0,
                    len([l for l in (blocks[1].splitlines() if len(blocks) > 1 else []) if l.startswith("- [")]),
                    "write" if nudge_block == _WRITE_NUDGE else ("consolidate" if nudge_block else "none"),
                )
            return result

        # -- Pre-compress rescue ---------------------------------------------

        def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
            """Inject fact-extraction instruction into the compression prompt.

            The compressor is already making a dedicated LLM call. This text
            is appended to the compression prompt at zero extra model cost.
            """
            return (
                "Before compressing, extract any durable facts from the messages "
                "above that have not yet been stored. Call fact_store(action='add') "
                "for each one:\n"
                "  • category='user_pref'  — preferences, working style, instructions\n"
                "  • category='project'    — decisions, architecture, rationale\n"
                "  • category='tool'       — service quirks, config findings, tool patterns\n"
                "Only store facts not already in the bank. Then call "
                "fact_store(action='contradict') to surface any conflicts. "
                "If no new facts warrant storage, skip both calls."
            )

        # -- MEMORY.md mirror ------------------------------------------------

        def on_memory_write(
            self,
            action: str,
            target: str,
            content: str,
            metadata: dict | None = None,
        ) -> None:
            """Mirror native MEMORY.md writes to fact_store.

            Fires on every successful memory() tool call, including writes
            from the native nudge_interval background review agent. Gives
            free retention from that existing mechanism.

            Removals are not mirrored — applying a MEMORY.md removal
            blindly to fact_store risks deleting facts that were stored
            by other means with different IDs.
            """
            if not self._mirror_memory_writes_plus:
                return
            if action == "remove":
                return
            if not self._store or not content:
                return
            try:
                category = "user_pref" if target == "user" else "general"
                self._store.add_fact(content, category=category)
                logger.debug(
                    "holographic-plus: mirrored memory.%s (target=%s) to fact_store",
                    action,
                    target,
                )
            except Exception as e:
                logger.debug("holographic-plus on_memory_write mirror failed: %s", e)

        # -- Config schema ---------------------------------------------------

        def get_config_schema(self) -> List[Dict[str, Any]]:
            """Extend the base schema with new config keys."""
            return super().get_config_schema() + [
                {
                    "key": "trait_min_trust",
                    "description": (
                        "Minimum trust score for unconditional user_pref "
                        "trait injection at session start and every inject_interval turns"
                    ),
                    "default": "0.7",
                },
                {
                    "key": "inject_interval",
                    "description": (
                        "Re-inject stable user_pref traits every N turns "
                        "(0 = turn 1 only, never recurs)"
                    ),
                    "default": "20",
                },
                {
                    "key": "write_nudge_interval",
                    "description": (
                        "Append write nudge (fact_store retention task) every N turns "
                        "(0 = disabled)"
                    ),
                    "default": "10",
                },
                {
                    "key": "consolidate_nudge_interval",
                    "description": (
                        "Append consolidation nudge (bank hygiene task) every N turns. "
                        "Never fires on the same turn as write_nudge_interval "
                        "(0 = disabled)"
                    ),
                    "default": "30",
                },
                {
                    "key": "mirror_memory_writes",
                    "description": (
                        "Mirror MEMORY.md writes to fact_store automatically "
                        "when the native memory nudge fires"
                    ),
                    "default": "true",
                    "choices": ["true", "false"],
                },
            ]

    return HolographicPlusProvider


# ---------------------------------------------------------------------------
# Config loader (mirrors the bundled plugin's _load_plugin_config)
# ---------------------------------------------------------------------------

def _load_plugin_config() -> dict:
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.config import cfg_get
        import yaml

        config_path = get_hermes_home() / "config.yaml"
        if not config_path.exists():
            return {}
        with open(config_path) as f:
            all_config = yaml.safe_load(f) or {}
        return cfg_get(all_config, "plugins", "hermes-memory-store", default={}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register holographic-plus as the active memory provider."""
    ProviderClass = _make_class()
    provider = ProviderClass()
    ctx.register_memory_provider(provider)
