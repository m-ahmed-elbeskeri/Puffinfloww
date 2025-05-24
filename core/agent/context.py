"""
core.agent.context
==================

Unified state container for Puffinflow agents.

Highlights
----------
• set_variable / get_variable                – free-form (no type lock)
• set_typed_variable / get_typed_variable    – first write locks *Python* type
• set_validated_data / get_validated_data    – first write locks Pydantic model
• All metadata (_meta_typed_* / _meta_validated_*) is persisted in shared_state
  so a fresh Context instance can reconstruct locks after reload.
• Constants, secrets, TTL cache, per-state scratch (typed & untyped),
  human-in-the-loop helper.

Compatible with Python 3.8 – 3.13 and Pydantic v2 (preferred) or v1.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    runtime_checkable,
)

# ─────────────────────────────── protocol helper ────────────────────────── #

try:
    from typing import Protocol          # stdlib ≥3.8
except ImportError:
    from typing_extensions import Protocol  # type: ignore

# ─────────────────────────────── pydantic shim ──────────────────────────── #

try:                                         # Prefer v2
    from pydantic import BaseModel as _PBM
    _PYD_VER = 2
except ModuleNotFoundError:
    try:                                     # Accept v1
        from pydantic.v1 import BaseModel as _PBM  # type: ignore
        _PYD_VER = 1
    except ModuleNotFoundError as _e:        # Unavailable
        _PBM = None          # type: ignore
        _PYD_VER = 0
        _PYD_ERR = _e

_PBM_T = TypeVar("_PBM_T", bound=_PBM)        # generic helper


@runtime_checkable
class TypedContextData(Protocol):
    """Marker protocol – implemented at runtime by Pydantic BaseModel."""


class StateType(Enum):
    ANY = "any"
    TYPED = "typed"
    UNTYPED = "untyped"


# ────────────────────────────────── Context ─────────────────────────────── #

class Context:
    """
    Lightweight container passed to every state function.

    Parameters
    ----------
    shared_state : dict
        Mutable mapping persisted for the whole workflow run.
    cache_ttl : int, default 300
        TTL (seconds) for entries set via `set_cached`.
    """

    # metadata prefixes stored in shared_state
    _META_TYPED = "_meta_typed_"
    _META_VALIDATED = "_meta_validated_"

    # keys that may never be overwritten via set_variable
    _IMMUTABLE_PREFIXES = ("const_", "secret_")

    # ---------------------------------------------------------------- init --

    def __init__(self, shared_state: Dict[str, Any], cache_ttl: int = 300) -> None:
        self.shared_state = shared_state
        self.cache_ttl = cache_ttl

        # per-state scratch
        self._state_data: Dict[str, Any] = {}
        self._typed_data: Dict[str, _PBM] = {}
        self._protected_keys: Set[str] = set()

        # shared-state metadata (reconstructed from shared_state)
        self._typed_var_types: Dict[str, Type[Any]] = {}
        self._validated_types: Dict[str, Type[_PBM]] = {}

        self._cache: Dict[str, Tuple[Any, float]] = {}

        self._restore_metadata()

    # ---------------------------------------------------------------- utils --

    def _restore_metadata(self) -> None:
        """
        Rebuild internal maps from persisted _meta_* keys.
        """
        for k, v in self.shared_state.items():
            if k.startswith(self._META_TYPED):
                orig = k[len(self._META_TYPED):]
                if orig in self.shared_state:
                    self._typed_var_types[orig] = type(self.shared_state[orig])
            elif k.startswith(self._META_VALIDATED):
                orig = k[len(self._META_VALIDATED):]
                if orig in self.shared_state and isinstance(self.shared_state[orig], _PBM):
                    self._validated_types[orig] = type(self.shared_state[orig])

    @staticmethod
    def _now() -> float:                # monotonic timestamp
        return time.monotonic()

    def _ensure_pydantic(self) -> None:
        if _PYD_VER == 0:
            raise ImportError(
                "Pydantic is required for validated data but is not installed."
            ) from _PYD_ERR  # type: ignore[name-defined]

    # guard for reserved prefixes
    def _guard_reserved(self, key: str) -> None:
        if key.startswith(self._IMMUTABLE_PREFIXES):
            raise KeyError(
                "Keys beginning with 'const_' or 'secret_' are reserved."
            )

    # persist helper
    def _persist_meta(self, prefix: str, key: str, cls: type) -> None:
        """
        Store a metadata record in shared_state for later reconstruction.

        Only the dotted path of the class is stored; this is informational.
        """
        self.shared_state[f"{prefix}{key}"] = f"{cls.__module__}.{cls.__qualname__}"

    # ==================================================== per-state scratch --

    def set_state(self, key: str, value: Any) -> None:
        if key in self._protected_keys:
            raise KeyError(f"Cannot overwrite typed slot '{key}' via set_state.")
        self._state_data[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        if key in self._protected_keys:
            raise KeyError(f"Cannot access typed slot '{key}' via get_state.")
        return self._state_data.get(key, default)

    # per-state typed scratch
    def set_typed(self, key: str, value: _PBM) -> None:
        self._ensure_pydantic()
        if not isinstance(value, _PBM):
            raise TypeError("set_typed expects a Pydantic BaseModel.")
        self._state_data.pop(key, None)
        self._protected_keys.add(key)
        self._typed_data[key] = value

    def get_typed(self, key: str, expected: Type[_PBM_T]) -> Optional[_PBM_T]:
        self._ensure_pydantic()
        val = self._typed_data.get(key)
        return val if isinstance(val, expected) else None

    def update_typed(self, key: str, **updates: Any) -> None:
        self._ensure_pydantic()
        if key not in self._typed_data:
            raise KeyError(f"No typed data under '{key}'.")
        self._typed_data[key] = self._typed_data[key].model_copy(update=updates, deep=True)

    # ===================================================== free variables --

    def set_variable(self, key: str, value: Any) -> None:
        """Cross-state variable (type can change freely)."""
        self._guard_reserved(key)
        self.shared_state[key] = value

    def get_variable(self, key: str, default: Any = None) -> Any:
        return self.shared_state.get(key, default)

    def get_variable_keys(self) -> Set[str]:
        return {
            k for k in self.shared_state
            if not k.startswith(self._IMMUTABLE_PREFIXES)
               and not k.startswith((self._META_TYPED, self._META_VALIDATED))
        }

    # ========================================== typed-variable (type-locked) --

    def set_typed_variable(self, key: str, value: Any) -> None:
        self._guard_reserved(key)
        current_cls = self._typed_var_types.get(key)
        if current_cls is None:
            # first write → record and persist meta
            self._typed_var_types[key] = type(value)
            self._persist_meta(self._META_TYPED, key, type(value))
        elif not isinstance(value, current_cls):
            raise TypeError(
                f"Typed variable '{key}' already holds {current_cls.__name__}; "
                f"cannot store {type(value).__name__}."
            )
        self.shared_state[key] = value

    def get_typed_variable(self, key: str, expected: Type[Any]) -> Optional[Any]:
        val = self.shared_state.get(key)
        return val if isinstance(val, expected) else None

    # =========================================== validated data (Pydantic) --

    def set_validated_data(self, key: str, value: _PBM) -> None:
        self._ensure_pydantic()
        if not isinstance(value, _PBM):
            raise TypeError("set_validated_data expects a Pydantic BaseModel.")
        self._guard_reserved(key)
        current_cls = self._validated_types.get(key)
        if current_cls is None:
            self._validated_types[key] = type(value)
            self._persist_meta(self._META_VALIDATED, key, type(value))
        elif not isinstance(value, current_cls):
            raise TypeError(
                f"Validated key '{key}' already stores {current_cls.__name__}; "
                f"cannot store {type(value).__name__}."
            )
        self.shared_state[key] = value

    def get_validated_data(self, key: str, expected: Type[_PBM_T]) -> Optional[_PBM_T]:
        self._ensure_pydantic()
        val = self.shared_state.get(key)
        return val if isinstance(val, expected) else None

    # ================================================= constants / secrets --

    def _set_immutable(self, prefix: str, key: str, value: Any) -> None:
        full = f"{prefix}{key}"
        if full in self.shared_state:
            raise ValueError(f"Immutable key '{key}' already set.")
        self.shared_state[full] = value

    def set_constant(self, key: str, value: Any) -> None:
        self._set_immutable("const_", key, value)

    def get_constant(self, key: str, default: Any = None) -> Any:
        return self.shared_state.get(f"const_{key}", default)

    def set_secret(self, key: str, value: str) -> None:
        self._set_immutable("secret_", key, value)

    def get_secret(self, key: str) -> Optional[str]:
        return self.shared_state.get(f"secret_{key}")

    # ==================================================== output helpers --

    def set_output(self, key: str, value: Any) -> None:
        self.set_state(f"output_{key}", value)

    def get_output(self, key: str, default: Any = None) -> Any:
        return self.get_state(f"output_{key}", default)

    def get_output_keys(self) -> Set[str]:
        return {
            k[len("output_"):] for k in self._state_data
            if k.startswith("output_")
        }

    # ======================================================== cache (TTL) --

    def set_cached(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._cache[key] = (value, self._now() + (ttl or self.cache_ttl))

    def get_cached(self, key: str, default: Any = None) -> Any:
        val, exp = self._cache.get(key, (default, 0))
        if self._now() < exp:
            return val
        self._cache.pop(key, None)
        return default

    # ================================================= housekeeping --------

    def remove_state(self, key: str, state_type: StateType = StateType.ANY) -> bool:
        removed = False
        if state_type in (StateType.ANY, StateType.TYPED) and key in self._typed_data:
            del self._typed_data[key]
            self._protected_keys.discard(key)
            removed = True
        if state_type in (StateType.ANY, StateType.UNTYPED) and key in self._state_data:
            del self._state_data[key]
            removed = True
        return removed

    def clear_state(self, state_type: StateType = StateType.ANY) -> None:
        if state_type in (StateType.ANY, StateType.TYPED):
            self._typed_data.clear()
            self._protected_keys.clear()
        if state_type in (StateType.ANY, StateType.UNTYPED):
            self._state_data.clear()

    def get_keys(self, state_type: StateType = StateType.ANY) -> Set[str]:
        if state_type == StateType.ANY:
            return set(self._typed_data) | set(self._state_data)
        if state_type == StateType.TYPED:
            return set(self._typed_data)
        return {k for k in self._state_data if k not in self._protected_keys}

    # ================================= human-in-the-loop helper -----------

    async def human_in_the_loop(
        self,
        prompt: str,
        timeout: Optional[float] = None,
        default: Optional[str] = None,
        validator: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """
        Prompt a human (blocking input executed in a thread).
        """
        while True:
            if timeout is not None:
                try:
                    reply = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, input, prompt),
                        timeout,
                    )
                except asyncio.TimeoutError:
                    return default if default is not None else ""
            else:
                reply = input(prompt)

            if validator is None or validator(reply):
                return reply
