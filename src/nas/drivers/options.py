"""Driver construction options.

A dataclass rather than a widening list of positional arguments. With one vendor
``(connect_timeout, command_timeout)`` was fine; with Juniper NETCONF, IOS-XE SSH
and NX-API over HTTPS there is now transport-specific configuration, and every
future vendor would add another parameter to every factory signature.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DriverOptions:
    connect_timeout: int = 30
    command_timeout: int = 60

    verify_tls: bool = False
    """Whether to verify device TLS certificates (NX-API).

    Defaults to **False**, deliberately and with a caveat. Nexus switches ship with
    self-signed certificates, and NAS reaches them over a private management
    network — so verification would fail on essentially every device while adding
    little, given the network boundary is doing the work.

    Set ``NAS_DRIVER_VERIFY_TLS=true`` once the estate has certificates NAS can
    validate. It is a real weakening of transport security, so it is configurable
    rather than hardcoded.
    """
