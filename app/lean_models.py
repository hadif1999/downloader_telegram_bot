"""Build aiogram's pydantic models on first use instead of at import (saves ~100 MB per process).

aiogram declares its ~460 Telegram types with ``defer_build=True`` but then calls
``model_rebuild(_types_namespace=...)`` on every one of them while ``aiogram.types`` is imported,
only to resolve forward references between types. That compiles every validator and serializer
up front: about 137 MB of resident memory per Python process, although a bot only ever touches a
few dozen types and methods.

`install()` imports aiogram once with those eager rebuilds intercepted. It records the namespace
aiogram supplies and attaches it to every still-incomplete aiogram model as its parent namespace.
Pydantic then builds each model on first validation or serialization, resolving the same forward
references it would have resolved eagerly. Behaviour is unchanged; only the build moves later.

The interception is limited to aiogram's own deferred models during that single import, and it
falls back to aiogram's normal eager behaviour if pydantic's private hook has a different
signature. Set ``AIOGRAM_EAGER_MODELS=1`` to disable it.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys

log = logging.getLogger(__name__)
_installed = False


def install() -> bool:
    """Import aiogram with lazily built models. Returns True if lazy building is active."""
    global _installed
    if _installed:
        return True
    if os.environ.get("AIOGRAM_EAGER_MODELS", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if "aiogram.types" in sys.modules:
        return False  # imported before us: models are already built, nothing left to save

    import pydantic

    descriptor = pydantic.BaseModel.__dict__["model_rebuild"]
    original = descriptor.__func__
    if "_types_namespace" not in inspect.signature(original).parameters:
        log.warning("pydantic model_rebuild signature changed; aiogram models are built eagerly")
        return False

    namespace: dict = {}

    def deferred_rebuild(cls, *args, _types_namespace=None, **kwargs):
        if (
            _types_namespace is not None
            and cls.__module__.startswith("aiogram.")
            and cls.model_config.get("defer_build")
            and not args
            and not kwargs.get("force")
        ):
            namespace.update(_types_namespace)
            return None
        return original(cls, *args, _types_namespace=_types_namespace, **kwargs)

    pydantic.BaseModel.model_rebuild = classmethod(deferred_rebuild)
    try:
        import aiogram.methods  # noqa: F401 -- imported here so its models share the namespace
        import aiogram.types  # noqa: F401
    finally:
        pydantic.BaseModel.model_rebuild = descriptor  # restore the exact original classmethod

    # Make the names aiogram resolves eagerly visible where pydantic looks when it builds a model
    # later: the defining module of every aiogram model, including classes created at runtime such
    # as the generic ``Response[ChatFullInfo]``, which never see a parent namespace. These names
    # are only imported under TYPE_CHECKING in aiogram's modules; adding missing ones is harmless.
    for name, module in list(sys.modules.items()):
        if name.startswith("aiogram.") and module is not None:
            for key, value in namespace.items():
                module.__dict__.setdefault(key, value)
    for cls in _subclasses(pydantic.BaseModel):
        if cls.__module__.startswith("aiogram.") and not cls.__pydantic_complete__:
            cls.__pydantic_parent_namespace__ = {**(cls.__pydantic_parent_namespace__ or {}), **namespace}
    _installed = True
    return True


def _subclasses(base: type) -> set[type]:
    found: set[type] = set()
    pending = [base]
    while pending:
        for sub in pending.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                pending.append(sub)
    return found
