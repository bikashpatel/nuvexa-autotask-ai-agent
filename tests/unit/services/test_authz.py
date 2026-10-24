from __future__ import annotations

import uuid

import pytest

from app.core.errors import PermissionDenied
from app.domain.enums import UserRole
from app.services.authz import ROLE_PERMISSIONS, Actor, AuditedPermissionDenied, Perm, require, require_owner_or

ALL_PERMS = {
    Perm.EMAIL_READ,
    Perm.EMAIL_READ_ALL,
    Perm.EMAIL_SYNC,
    Perm.WORKFLOW_RUN,
    Perm.ACTION_EDIT,
    Perm.ACTION_APPROVE,
    Perm.ACTION_EXECUTE,
    Perm.ACTION_APPROVE_ANY,
    Perm.AUDIT_READ_ALL,
    Perm.SETTINGS_WRITE,
}

MEMBER_PERMS = {
    Perm.EMAIL_READ,
    Perm.EMAIL_SYNC,
    Perm.WORKFLOW_RUN,
    Perm.ACTION_EDIT,
    Perm.ACTION_APPROVE,
    Perm.ACTION_EXECUTE,
}
APPROVER_PERMS = MEMBER_PERMS | {Perm.ACTION_APPROVE_ANY, Perm.AUDIT_READ_ALL, Perm.EMAIL_READ_ALL}

EXPECTED = {
    UserRole.VIEWER: {Perm.EMAIL_READ},
    UserRole.MEMBER: MEMBER_PERMS,
    UserRole.APPROVER: APPROVER_PERMS,
    UserRole.ADMIN: APPROVER_PERMS | {Perm.SETTINGS_WRITE},
}


def _actor(role: UserRole, user_id: uuid.UUID | None = None) -> Actor:
    return Actor.build(
        user_id=user_id or uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=role,
        email=f"{role.value}@example.com",
        display_name=role.value.title(),
        ip_address="203.0.113.7",
    )


def test_role_matrix_is_exact_for_every_role() -> None:
    assert set(ROLE_PERMISSIONS) == set(UserRole)
    for role, perms in EXPECTED.items():
        assert set(ROLE_PERMISSIONS[role]) == perms, role
        assert set(ROLE_PERMISSIONS[role]) <= ALL_PERMS


@pytest.mark.parametrize("role", list(UserRole))
def test_build_binds_role_permissions_and_identity(role: UserRole) -> None:
    actor = _actor(role)
    assert actor.permissions == ROLE_PERMISSIONS[role]
    assert actor.ip_address == "203.0.113.7"
    for perm in ALL_PERMS:
        assert actor.has(perm) is (perm in EXPECTED[role])


@pytest.mark.parametrize(
    ("role", "sees"),
    [(UserRole.VIEWER, False), (UserRole.MEMBER, False), (UserRole.APPROVER, True), (UserRole.ADMIN, True)],
)
def test_sees_tenant_only_for_privileged_roles(role: UserRole, sees: bool) -> None:
    assert _actor(role).sees_tenant is sees


def test_actor_is_immutable_and_default_has_no_permissions() -> None:
    bare = Actor(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=UserRole.ADMIN, email="a@x.io", display_name="A")
    assert bare.permissions == frozenset()
    assert not bare.has(Perm.SETTINGS_WRITE)  # permissions come only from Actor.build
    with pytest.raises(AttributeError):
        bare.role = UserRole.VIEWER  # type: ignore[misc]


def test_require_passes_when_permission_held() -> None:
    require(_actor(UserRole.ADMIN), Perm.SETTINGS_WRITE)


def test_require_denial_carries_audit_context() -> None:
    actor = _actor(UserRole.MEMBER)
    with pytest.raises(AuditedPermissionDenied) as info:
        require(actor, Perm.SETTINGS_WRITE, resource_type="settings", resource_id="abc")
    exc = info.value
    assert isinstance(exc, PermissionDenied)
    assert exc.status_code == 403
    assert exc.code == "permission_denied"
    assert exc.actor is actor
    assert exc.resource_type == "settings"
    assert exc.resource_id == "abc"
    assert exc.permission == Perm.SETTINGS_WRITE
    assert exc.message == "Missing permission 'settings:write'."


def test_require_defaults_to_api_resource() -> None:
    with pytest.raises(AuditedPermissionDenied) as info:
        require(_actor(UserRole.VIEWER), Perm.EMAIL_SYNC)
    assert info.value.resource_type == "api"
    assert info.value.resource_id is None


def test_audited_denial_permission_defaults_to_none() -> None:
    exc = AuditedPermissionDenied("nope", actor=_actor(UserRole.VIEWER), resource_type="action", resource_id=None)
    assert exc.permission is None
    assert exc.message == "nope"


def test_require_owner_or_allows_owner_with_own_perm() -> None:
    actor = _actor(UserRole.MEMBER)
    require_owner_or(
        actor, actor.user_id, Perm.ACTION_EDIT, Perm.ACTION_APPROVE_ANY, resource_type="action", resource_id="1"
    )


def test_require_owner_or_allows_any_perm_for_other_owner() -> None:
    require_owner_or(
        _actor(UserRole.APPROVER),
        uuid.uuid4(),
        Perm.ACTION_EDIT,
        Perm.ACTION_APPROVE_ANY,
        resource_type="action",
        resource_id="1",
    )


def test_require_owner_or_denies_other_owner_without_any_perm() -> None:
    actor = _actor(UserRole.MEMBER)
    with pytest.raises(AuditedPermissionDenied) as info:
        require_owner_or(
            actor, uuid.uuid4(), Perm.ACTION_EDIT, Perm.ACTION_APPROVE_ANY, resource_type="action", resource_id="a1"
        )
    assert info.value.permission == Perm.ACTION_APPROVE_ANY
    assert info.value.resource_type == "action"
    assert info.value.resource_id == "a1"
    assert "for this action" in info.value.message


def test_require_owner_or_denies_owner_missing_own_perm() -> None:
    viewer = _actor(UserRole.VIEWER)
    with pytest.raises(AuditedPermissionDenied) as info:
        require_owner_or(
            viewer, viewer.user_id, Perm.ACTION_EDIT, Perm.ACTION_APPROVE_ANY, resource_type="email", resource_id=None
        )
    assert info.value.permission == Perm.ACTION_EDIT  # the owner is told which own-permission is missing
    assert info.value.actor is viewer
