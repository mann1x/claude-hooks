"""Who a message is for, and when that question has no single answer.

Three forms, and the third exists because the first two cannot both be
safe:

``osync@solidpc``   one alias on one host
``osync*``          that alias everywhere it is registered
``osync``           bare — fine while unambiguous, refused once not

The refusal is the point. The same alias legitimately runs on more than
one host, so a bare alias is under-specified the moment a second host
registers it. Silently picking one is a wrong delivery that looks like a
right one; silently fanning out sends private instructions to a session
that did not ask for them. Neither is recoverable by the sender, who has
no way to know which happened.

Cross-host traffic is the rare case, so requiring ``@host`` on every
send would tax the common path to guard against the uncommon one. The
rule is therefore: **send when unambiguous, refuse when not.**
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

#: A session id. Ours are UUIDs; anything of that shape is treated as an
#: id rather than an alias, because an alias that looks like a UUID is a
#: pathological name and an id that looks like an alias cannot happen.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE)

#: Aliases are directory-name shaped. Deliberately narrow: `@` and `*`
#: are structure, so they cannot appear in the name they delimit.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class AddressError(ValueError):
    """The address cannot be resolved, and the message says how to fix
    it. Distinct from "nobody is listening", which is not an error —
    mail waits."""


@dataclass(frozen=True)
class Address:
    """A parsed recipient, before it meets the registry."""

    #: Exactly one of these is set.
    session_id: Optional[str] = None
    alias: Optional[str] = None
    #: Only meaningful with ``alias``.
    host: Optional[str] = None
    broadcast: bool = False

    @property
    def is_session(self) -> bool:
        return self.session_id is not None

    def __str__(self) -> str:
        if self.session_id:
            return self.session_id
        if self.broadcast:
            return f"{self.alias}*"
        if self.host:
            return f"{self.alias}@{self.host}"
        return self.alias or ""


@dataclass(frozen=True)
class Session:
    """One row of the registry, as far as addressing cares."""

    session_id: str
    alias: str
    host: str
    os: str = ""
    cwd: str = ""
    last_seen: Optional[str] = None

    def label(self) -> str:
        return f"{self.alias}@{self.host}"


def parse_address(raw: str) -> Address:
    """``osync``/``osync@host``/``osync*``/``<uuid>`` → :class:`Address`.

    Rejects rather than normalises anything ambiguous. ``osync@*`` and
    ``osync@`` are typos with a plausible reading, and guessing which
    would be the same mistake the whole module is avoiding.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise AddressError(
            "No recipient given. Use an alias (`osync`), an alias on a "
            "host (`osync@solidpc`), a broadcast (`osync*`), or a "
            "session id.")
    value = raw.strip()

    if _UUID_RE.match(value):
        return Address(session_id=value.lower())

    broadcast = value.endswith("*")
    if broadcast:
        value = value[:-1]
        if not value:
            raise AddressError(
                "`*` is not an address on its own. Broadcast is "
                "per-alias, as in `osync*` — there is deliberately no "
                "way to message every session on the machine.")

    host: Optional[str] = None
    if "@" in value:
        if broadcast:
            raise AddressError(
                f"`{raw}` combines a host and a broadcast. Pick one: "
                f"`{value.split('@')[0]}*` for every host, or "
                f"`{value}` for that one.")
        value, _, host = value.partition("@")
        if not host:
            raise AddressError(
                f"`{raw}` ends with `@` and no host. Use `{value}` for "
                f"any host, or name one.")
        if not _ALIAS_RE.match(host):
            raise AddressError(f"`{host}` is not a valid host name.")

    if not value:
        raise AddressError(f"`{raw}` has no alias part.")
    if not _ALIAS_RE.match(value):
        raise AddressError(
            f"`{value}` is not a valid alias. Aliases are letters, "
            f"digits, dot, dash and underscore — `@` and `*` are "
            f"address structure.")
    return Address(alias=value, host=host, broadcast=broadcast)


def resolve(address: Address,
            sessions: Sequence[Session]) -> list[Session]:
    """Match ``address`` against the registry, or refuse.

    Returns the recipients. An **empty list is a valid result**: mail
    addressed to an alias nobody has registered waits, which is the
    entire reason this is not a chat system — the session you have
    something for is usually the one that is closed.

    Raises :class:`AddressError` only when the address is genuinely
    ambiguous, and the message carries the candidates so the caller can
    retry without a second lookup.
    """
    if address.is_session:
        matches = [s for s in sessions if s.session_id == address.session_id]
        if len(matches) > 1:
            # Two hosts claiming one session id cannot happen through
            # normal use; it means the registry is wrong, and delivering
            # to an arbitrary one would bury that.
            hosts = ", ".join(sorted(m.host for m in matches))
            raise AddressError(
                f"Session id {address.session_id} is registered on more "
                f"than one host ({hosts}). That is a registry bug, not a "
                f"routing choice — nothing was sent.")
        return matches

    candidates = [s for s in sessions if s.alias == address.alias]

    if address.host is not None:
        return [s for s in candidates if s.host == address.host]

    if address.broadcast:
        return candidates

    if len(candidates) <= 1:
        return candidates

    hosts = sorted({s.host for s in candidates})
    if len(hosts) <= 1:
        # Several registrations, one host: one mailbox, so there is
        # nothing to disambiguate. Counting rows instead of hosts made
        # this raise "`xollama` is registered on 1 hosts: solidpc" and
        # refuse to send — a session that merely restarted eleven times
        # made its own alias unaddressable.
        return candidates

    raise AddressError(
        f"`{address.alias}` is registered on {len(hosts)} hosts: "
        f"{', '.join(hosts)}. Nothing was sent — say which you mean:\n"
        + "\n".join(f"  {address.alias}@{h}" for h in hosts)
        + f"\n  {address.alias}*   (all of them)")


def describe_recipients(recipients: Sequence[Session],
                        address: Address) -> str:
    """What actually happened, for the send confirmation.

    Reporting the resolution is what makes a typo'd alias visible
    instead of silently creating a mailbox nobody reads.
    """
    if not recipients:
        return (f"queued for `{address}` — no session is registered under "
                f"that name right now, so it waits until one is. "
                f"(If that is a typo, nothing will ever read it: check "
                f"`mailbox-sessions`.)")
    # By destination, not by registration: eleven registrations of one
    # alias on one host are one mailbox, and listing the same label
    # eleven times described a fan-out that no longer happens.
    labels: list[str] = []
    for r in recipients:
        if r.label() not in labels:
            labels.append(r.label())
    if len(labels) == 1:
        return f"delivered to `{labels[0]}`"
    return "delivered to " + ", ".join(f"`{x}`" for x in labels)
