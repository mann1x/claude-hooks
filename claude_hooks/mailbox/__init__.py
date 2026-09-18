"""Inter-session mailbox — direct messaging between Claude sessions.

Replaces the ``/shared/dev/handover/*.md`` convention, which failed in
both directions on 2026-09-16: one session rewrote a handover underneath
another's reply, and nothing told anybody a document was waiting.

See ``docs/PLAN-session-mailbox.md`` for the design and the reasoning
behind the parts that look odd — chiefly that acknowledgement is
optional and gated on a note, and that announcements never carry a
message body.
"""

from claude_hooks.mailbox.addressing import (
    Address,
    AddressError,
    Session,
    describe_recipients,
    parse_address,
    resolve,
)
from claude_hooks.mailbox.store import (
    DEFAULT_EXPIRY_DAYS,
    DEFAULT_REGISTRY_DAYS,
    MailboxError,
    MailboxStore,
    host_name,
    os_name,
)

__all__ = [
    "Address",
    "AddressError",
    "DEFAULT_EXPIRY_DAYS",
    "DEFAULT_REGISTRY_DAYS",
    "MailboxError",
    "MailboxStore",
    "Session",
    "describe_recipients",
    "host_name",
    "os_name",
    "parse_address",
    "resolve",
]
