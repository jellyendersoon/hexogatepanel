from functools import wraps

from app.models.admin import AdminDetails
from app.models.admin_role import PermissionScope, RoleLimits, RolePermissions


class PermissionDenied(Exception):
    def __init__(self, detail: str = "Permission denied"):
        self.detail = detail
        super().__init__(detail)


class LimitExceeded(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def _resolve_scope(action_perm) -> PermissionScope | None:
    """Return PermissionScope if the action value is a scoped permission, else None."""
    if isinstance(action_perm, dict):
        raw = action_perm.get("scope")
        if raw is not None:
            return PermissionScope(raw)
    return None


def _get_resource_action(admin: AdminDetails, resource: str, action: str):
    """Return the action permission value for resource+action, or None if missing."""
    permissions = admin.role.permissions if admin.role else None
    resource_perms = permissions.get(resource) if permissions else None
    return (resource_perms or {}).get(action) if resource_perms is not None else None


_READ_ACTIONS = frozenset({"read", "read_simple", "read_general", "logs", "stats"})


def enforce_permission(admin: AdminDetails, resource: str, action: str) -> None:
    """
    Check if admin has permission for resource+action.
    Raises PermissionDenied if not allowed.

    Resolution order:
    1. role.is_owner → ALLOW unconditionally
    2. admin.is_limited:
       - role.disabled_when_limited=True → DENY all actions
       - role.disabled_when_limited=False → DENY write actions, allow read actions
    3. permissions[resource][action]:
       - missing              → DENY
       - True                 → ALLOW
       - {scope: NONE (0)}    → DENY (explicitly disabled)
       - {scope: OWN  (1)}    → ALLOW (scope enforced separately)
       - {scope: ALL  (2)}    → ALLOW
    """
    if admin.is_owner:
        return

    if admin.is_limited:
        if admin.role and admin.role.disabled_when_limited:
            raise PermissionDenied("Admin is limited — all access blocked")
        if action not in _READ_ACTIONS:
            raise PermissionDenied("Admin is limited — write actions blocked")

    action_perm = _get_resource_action(admin, resource, action)
    if not action_perm:
        raise PermissionDenied(f"Permission denied: {resource}.{action}")

    scope = _resolve_scope(action_perm)
    if scope is PermissionScope.NONE:
        raise PermissionDenied(f"Permission denied: {resource}.{action}")


def enforce_scope(admin: AdminDetails, resource: str, action: str, target_admin_id: int | None) -> None:
    """
    Enforce scope restriction (users resource only). Call AFTER enforce_permission.
    Raises PermissionDenied if scope is OWN and target doesn't belong to this admin.
    """
    if admin.is_owner:
        return

    action_perm = _get_resource_action(admin, resource, action)
    if _resolve_scope(action_perm) is PermissionScope.OWN and target_admin_id != admin.id:
        raise PermissionDenied(f"Permission denied: {resource}.{action} (scope: own)")


def is_scope_all(admin: AdminDetails, resource: str, action: str) -> bool:
    """
    Return True if the action has scope=ALL or True (no scope restriction).
    Used to gate operations that require all-user access.
    """
    if admin.is_owner:
        return True
    action_perm = _get_resource_action(admin, resource, action)
    if action_perm is None:
        return False
    scope = _resolve_scope(action_perm)
    if scope is None:
        # True = allowed with no scope restriction = effectively all
        return action_perm is True
    return scope is PermissionScope.ALL


def get_scope_admin_id(admin: AdminDetails, resource: str, action: str) -> int | None:
    """
    Return admin.id if scope=OWN, else None.
    Pass as admin_id to CRUD queries so the DB enforces scope.
    """
    if admin.is_owner:
        return None
    if admin.role is None:
        return None
    action_perm = _get_resource_action(admin, resource, action)
    if _resolve_scope(action_perm) is PermissionScope.OWN:
        return admin.id
    return None


def get_effective_limits(admin: AdminDetails) -> RoleLimits:
    """
    Merge role limits with per-admin permission_overrides.
    Non-null override values win over role limits.
    """
    base = admin.role.limits if admin.role else RoleLimits()
    overrides = admin.permission_overrides

    if overrides is None:
        return base

    return base.model_copy(
        update={k: getattr(overrides, k) for k in overrides.model_fields_set if getattr(overrides, k) is not None}
    )


def can_extend_time(admin: AdminDetails) -> bool:
    """
    Return True if the admin may add time to existing users (later/unlimited expire,
    new or longer on-hold reservations, later on-hold timeouts, bulk expire increase).
    Owner always may; otherwise governed by role.features.can_extend_time.
    Features have no per-admin override (permission_overrides only carries RoleLimits).
    """
    if admin.is_owner or admin.role is None:
        return True
    return admin.role.features.can_extend_time


def get_allowed_group_ids(admin: AdminDetails) -> list[int] | None:
    """None means all groups allowed (owner or no restriction)."""
    if admin.is_owner or admin.role is None:
        return None
    return admin.role.access.allowed_group_ids


def get_allowed_template_ids(admin: AdminDetails) -> list[int] | None:
    """None means all templates allowed (owner or no restriction)."""
    if admin.is_owner or admin.role is None:
        return None
    return admin.role.access.allowed_template_ids


def _intersect_ids(requested: list[int] | None, allowed: list[int] | None) -> list[int] | None:
    if allowed is None:
        return requested
    if requested is None:
        return allowed
    return [i for i in requested if i in set(allowed)]


def apply_group_access(admin: AdminDetails, ids: list[int] | None) -> list[int] | None:
    """Intersect requested ids with admin's allowed_group_ids."""
    return _intersect_ids(ids, get_allowed_group_ids(admin))


def apply_template_access(admin: AdminDetails, ids: list[int] | None) -> list[int] | None:
    """Intersect requested ids with admin's allowed_template_ids."""
    return _intersect_ids(ids, get_allowed_template_ids(admin))


def check_permission(resource: str, action: str):
    """
    Decorator for operation-layer methods.
    Signature: async def method(self, db, *args, admin: AdminDetails, **kwargs)
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(self, db, *args, admin: AdminDetails, **kwargs):
            enforce_permission(admin, resource, action)
            return await func(self, db, *args, admin=admin, **kwargs)

        return wrapper

    return decorator


def check_permissions_not_exceed_admin(admin: AdminDetails, requested: RolePermissions) -> None:
    """Raise ValueError if any permission in ``requested`` exceeds what ``admin`` has.

    Owners are exempt. Used wherever a non-owner admin hands out permissions
    (API keys, role assignment, password resets of other admins) so nobody
    can grant more than they hold.
    """
    if admin.is_owner:
        return

    admin_perms = admin.role.permissions if admin.role else RolePermissions()

    for resource_name, resource_perms in requested.model_dump(exclude_none=True).items():
        if resource_perms is None:
            continue
        admin_resource = admin_perms.get(resource_name)
        if admin_resource is None:
            raise ValueError(f"You don't have access to resource '{resource_name}'")

        for action, value in resource_perms.items():
            if value is None:
                continue
            admin_action = admin_resource.get(action) if admin_resource else None
            if admin_action is None:
                raise ValueError(f"You don't have the '{action}' permission on '{resource_name}'")
            # True means unrestricted — cannot grant if admin only has scoped access
            if value is True and admin_action is not True:
                raise ValueError(
                    f"Cannot grant '{resource_name}.{action}=True': "
                    f"your own access is scoped (scope={admin_action.get('scope', 0) if isinstance(admin_action, dict) else admin_action})"
                )
            # If both sides have a scope dict, requested scope must not exceed admin scope
            if isinstance(value, dict) and isinstance(admin_action, dict):
                requested_scope = value.get("scope", 0)
                admin_scope = admin_action.get("scope", 0)
                if requested_scope > admin_scope:
                    raise ValueError(
                        f"Cannot grant '{resource_name}.{action}' with scope={requested_scope}: your scope is {admin_scope}"
                    )
