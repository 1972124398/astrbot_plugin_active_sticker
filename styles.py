"""Conversation-scoped style leases. Only an idle gap permits a probabilistic switch."""

import random
from dataclasses import dataclass


@dataclass
class PackLease:
    pack_id: str
    last_activity: float


def choose_pack(leases, key, available, now, idle_seconds, switch_probability):
    if not available:
        return ""
    lease = leases.get(key)
    if lease is None or lease.pack_id not in available:
        lease = PackLease(random.choice(available), now)
    elif now - lease.last_activity >= idle_seconds:
        others = [ident for ident in available if ident != lease.pack_id]
        if others and random.random() < switch_probability:
            lease.pack_id = random.choice(others)
    lease.last_activity = now
    leases[key] = lease
    leases.move_to_end(key)
    while len(leases) > 32:
        leases.popitem(last=False)
    return lease.pack_id
