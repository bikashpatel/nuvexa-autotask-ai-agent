"""Demo-mode constants: tenant, personas and the synthetic mailbox fixture (no real people or companies).

Used by the demo login (`POST /api/v1/auth/demo-login`, only when DEMO_MODE=true outside production)
and by `DemoMailboxProvider`. All addresses use the reserved `example.com` / `example.org` domains.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.domain.enums import UserRole


@dataclass(frozen=True, slots=True)
class DemoTenant:
    name: str
    slug: str


@dataclass(frozen=True, slots=True)
class DemoPersona:
    key: str
    display_name: str
    email: str
    role: UserRole
    description: str


DEMO_TENANT = DemoTenant(name="Elarxsys Demo", slug="elarxsys-demo")

# Keyed by persona key (the value the demo-login endpoint accepts).
DEMO_PERSONAS: Mapping[str, DemoPersona] = MappingProxyType(
    {
        "admin": DemoPersona(
            key="admin",
            display_name="Avery Admin",
            email="avery.admin@example.com",
            role=UserRole.ADMIN,
            description="Manages tenant settings and sees the tenant-wide audit log.",
        ),
        "approver": DemoPersona(
            key="approver",
            display_name="Riley Approver",
            email="riley.approver@example.com",
            role=UserRole.APPROVER,
            description="Reviews and approves proposed actions for anyone in the tenant.",
        ),
        "member": DemoPersona(
            key="member",
            display_name="Morgan Member",
            email="morgan.member@example.com",
            role=UserRole.MEMBER,
            description="Owns the demo mailbox and acts on their own emails.",
        ),
        "viewer": DemoPersona(
            key="viewer",
            display_name="Vic Viewer",
            email="vic.viewer@example.com",
            role=UserRole.VIEWER,
            description="Read-only access to their own items.",
        ),
    }
)

DEMO_MAILBOX_EXTERNAL_ID = "demo-mailbox"
DEMO_MAILBOX_ADDRESS = "support-desk@example.com"
DEMO_MAILBOX_DISPLAY_NAME = "Elarxsys Demo Support Desk"

__all__ = [
    "DEMO_MAILBOX_ADDRESS",
    "DEMO_MAILBOX_DISPLAY_NAME",
    "DEMO_MAILBOX_EXTERNAL_ID",
    "DEMO_PERSONAS",
    "DEMO_TENANT",
    "DemoPersona",
    "DemoTenant",
]
