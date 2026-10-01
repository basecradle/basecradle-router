"""A credential the daemon holds, which no generic representation can emit.

The router's signing secrets (each route's webhook secret, each harness agent's
per-recipient Integration Signing Key, the probe route's secret) used to be plain
``str`` fields on frozen dataclasses, and a dataclass's default ``__repr__`` prints
every field. So ``repr``, ``str``, ``pprint``, ``%r`` and ``%s`` logging, and
``json.dumps(default=str)`` of a :class:`~basecradle_router.config.Config`, a
:class:`~basecradle_router.routes.basecradle.RecipientKeyring` or a
:class:`~basecradle_router.probe.WakeProbe` printed a live secret, and so did the
:class:`~basecradle_router.pipeline.Pipeline` that holds the config. So did
``dataclasses.asdict``, which reads fields directly and never consults a repr, and
``__reduce_ex__``, whose state tuple is how ``pickle`` and ``copy`` see an object
(basecradle-router#317, the class from basecradle/basecradle#612).

**The fix lives on the value, not on each holder.** A per-class ``repr=False`` would
have left ``asdict`` open, and every future holder would have had to remember the
whole list. A :class:`Secret` is safe wherever it lands, so any object that holds
one, and anything that holds *that*, is safe by construction:

- the plaintext sits in a ``__slots__`` field, so there is no ``__dict__`` for
  ``vars()`` or ``json.dump(default=vars)`` to walk;
- ``__repr__`` and ``__str__`` render a fixed redaction that carries no length and
  no prefix, so a reader learns that a credential exists and nothing about it;
- ``__reduce__``/``__reduce_ex__`` raise, which refuses ``pickle`` at every protocol
  and also ``copy.copy`` and ``copy.deepcopy``. Since ``dataclasses.asdict`` and
  ``astuple`` deep-copy their leaves, they refuse too, and never emit.

A shallow copy of a *holder* still works, because it shares the same ``Secret``
object rather than copying it. That puts nothing new in memory and emits nothing.

:meth:`Secret.reveal` is the one way to read the plaintext, and it is called only
inside the expression that computes an HMAC. A loader necessarily sees the plaintext
once, when it reads the environment and wraps it. After that, from the config
through the route to the signature check, the plaintext is never a local variable or
an argument that a frame-rendering crash reporter could print.
"""

from __future__ import annotations

import hmac
from typing import NoReturn

#: What every :class:`Secret` renders as. Fixed: a length or a prefix would tell a
#: reader of a log line something about the key.
REDACTED = "Secret('**********')"


class Secret:
    """An immutable credential whose every generic representation is redacted."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"Secret wraps a str, got {type(value).__name__}")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The plaintext. Call it only where the key is used, never to store or print it."""
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        """Constant-time equality with another :class:`Secret`. A bare ``str`` is never equal."""
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode("utf-8"), other._value.encode("utf-8"))

    # Unhashable: a hash of the plaintext is a fingerprint of it, and nothing in the
    # daemon needs a secret as a set member or a mapping key.
    __hash__ = None  # type: ignore[assignment]

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError(
            "refusing to serialize or copy a Secret: pickle, copy and dataclasses.asdict "
            "would carry the plaintext credential out of this object"
        )

    def __reduce__(self) -> NoReturn:
        return self.__reduce_ex__(None)


__all__ = ["REDACTED", "Secret"]
