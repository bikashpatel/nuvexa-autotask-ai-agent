"""Actor model and role-based permissions. All authorization decisions are deterministic code."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.errors import PermissionDenied
from app.domain.enums import UserRole


class Perm:
    EMAIL_READ = "email:read"
    EMAIL_READ_ALL = "email:read_all"
    EMAIL_SYNC = "email:sync"
    WORKFLOW_RUN = "workflow:run"
    ACTION_EDIT = "action:edit"
    ACTION_APPROVE = "action:approve"
    ACTION_EXECUTE = "action:execute"
    ACTION_APPROVE_ANY = "action:approve_any"
    AUDIT_READ_ALL = "audit:read_all"
    SETTINGS_WRITE = "settings:write"


_MEMBER = frozenset(
    {Perm.EMAIL_READ, Perm.EMAIL_SYNC, Perm.WORKFLOW_RUN, Perm.ACTION_EDIT, Perm.ACTION_APPROVE, Perm.ACTION_EXECUTE}
)
_APPROVER = _MEMBER | {Perm.ACTION_APPROVE_ANY, Perm.AUDIT_READ_ALL, Perm.EMAIL_READ_ALL}

ROLE_PERMISSIONS: dict[UserRole, frozenset[str]] = {
    UserRole.VIEWER: frozenset({Perm.EMAIL_READ}),
    UserRole.MEMBER: _MEMBER,
    UserRole.APPROVER: _APPROVER,
    UserRole.ADMIN: _APPROVER | {Perm.SETTINGS_WRITE},
}


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: UserRole
    email: str
    display_name: str
    ip_address: str | None = None
    permissions: frozenset[str] = field(default=frozenset())

    @classmethod
    def build(
        cls,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role: UserRole,
        email: str,
        display_name: str,
        ip_address: str | None = None,
    ) -> Actor:
        return cls(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            email=email,
            display_name=display_name,
            ip_address=ip_address,
            permissions=ROLE_PERMISSIONS[role],
        )

    def has(self, perm: str) -> bool:
        return perm in self.permissions

    @property
    def sees_tenant(self) -> bool:
        """Approvers and admins see tenant-wide data; others only their own."""
        return self.has(Perm.EMAIL_READ_ALL)


class AuditedPermissionDenied(PermissionDenied):
    """Permission failure that carries enough context for the API/worker to persist an audit event."""

    def __init__(
        self,
        message: str,
        *,
        actor: Actor,
        resource_type: str,
        resource_id: str | None,
        permission: str | None = None,
    ) -> None:
        super().__init__(message)
        self.actor = actor
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.permission = permission


def require(actor: Actor, perm: str, *, resource_type: str = "api", resource_id: str | None = None) -> None:
    if not actor.has(perm):
        raise AuditedPermissionDenied(
            f"Missing permission '{perm}'.",
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            permission=perm,
        )


def require_owner_or(
    actor: Actor,
    owner_user_id: uuid.UUID,
    own_perm: str,
    any_perm: str,
    *,
    resource_type: str,
    resource_id: str | None,
) -> None:
    """Allow when the actor owns the resource and holds `own_perm`, or holds `any_perm`."""
    if actor.has(any_perm):
        return
    if owner_user_id == actor.user_id and actor.has(own_perm):
        return
    perm = own_perm if owner_user_id == actor.user_id else any_perm
    raise AuditedPermissionDenied(
        f"Missing permission '{perm}' for this {resource_type}.",
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        permission=perm,
    )
