"""
services/authz.py — Role-based access control (RBAC) for transcomonitor.

Sits between modules and DB ops to enforce :
  - 3-role model (admin / mainteneur / valideur — cf. plan §14 #7)
  - Mapping workflow rules (who can transition status to what)
  - Self-validation rule (§14 #7 : autorisé avec trace renforcée
    `is_self_validation=1` enregistré dans le mapping + audit)
  - Strict separation : a mainteneur cannot freeze versions ; only admin can
    create users etc.

Modules MUST call these helpers (no direct role checks) so the policy stays
consistent and unit-testable.
"""
from __future__ import annotations

from typing import Iterable, Optional

# ─────────────────────────────────────────────────────────────────────────
# Role constants
# ─────────────────────────────────────────────────────────────────────────

ROLE_ADMIN      = "admin"
ROLE_MAINTENEUR = "mainteneur"
ROLE_VALIDEUR   = "valideur"
ALL_ROLES = (ROLE_ADMIN, ROLE_MAINTENEUR, ROLE_VALIDEUR)


# ─────────────────────────────────────────────────────────────────────────
# Authorization errors
# ─────────────────────────────────────────────────────────────────────────

class AuthzError(PermissionError):
    """Raised when an action is denied by the policy."""
    def __init__(self, message: str, *, user: Optional[dict] = None, action: str = "",
                 required_role: Optional[str] = None):
        super().__init__(message)
        self.user = user
        self.action = action
        self.required_role = required_role


# ─────────────────────────────────────────────────────────────────────────
# Predicates (read-only, return bool)
# ─────────────────────────────────────────────────────────────────────────

def user_role(user: Optional[dict]) -> Optional[str]:
    """Extract role from a user dict (or None)."""
    return user.get("role") if user else None


def has_role(user: Optional[dict], *roles: str) -> bool:
    """True if user has any of `roles` ; admin always returns True."""
    r = user_role(user)
    if r is None:
        return False
    if r == ROLE_ADMIN:
        return True
    return r in roles


def is_admin(user: Optional[dict]) -> bool:
    return user_role(user) == ROLE_ADMIN


def is_mainteneur(user: Optional[dict]) -> bool:
    """True if user is admin OR mainteneur (admin has all privileges)."""
    return has_role(user, ROLE_MAINTENEUR)


def is_valideur(user: Optional[dict]) -> bool:
    return has_role(user, ROLE_VALIDEUR)


def is_authenticated(user: Optional[dict]) -> bool:
    return user is not None and user.get("id") is not None


# ─────────────────────────────────────────────────────────────────────────
# Enforcement helpers (raise on denial)
# ─────────────────────────────────────────────────────────────────────────

def require_authenticated(user: Optional[dict]) -> dict:
    if not is_authenticated(user):
        raise AuthzError("Authentication required", action="any")
    return user  # narrow type for the caller


def require_role(user: Optional[dict], *roles: str, action: str = "") -> dict:
    """Raises AuthzError if user is not in any of `roles` (admin always OK)."""
    require_authenticated(user)
    if not has_role(user, *roles):
        raise AuthzError(
            f"Forbidden : role '{user_role(user)}' cannot perform '{action}' "
            f"(requires {' or '.join(roles)})",
            user=user, action=action, required_role=" or ".join(roles),
        )
    return user


def require_admin(user: Optional[dict], action: str = "") -> dict:
    return require_role(user, ROLE_ADMIN, action=action)


# ─────────────────────────────────────────────────────────────────────────
# Mapping workflow rules
# ─────────────────────────────────────────────────────────────────────────

ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "propose":  frozenset({"en_revue", "valide", "rejete"}),
    "en_revue": frozenset({"valide", "rejete", "conteste"}),
    "valide":   frozenset({"en_revue", "conteste"}),   # editing a validated mapping → back to review
    "conteste": frozenset({"en_revue", "rejete", "valide"}),
    "rejete":   frozenset({"en_revue"}),               # reopen a rejection
    "gele":     frozenset(),                            # immutable
}


def can_transition_status(
    user: Optional[dict], from_status: str, to_status: str,
) -> bool:
    """Return True if `user` is allowed to change a mapping from
    `from_status` to `to_status`."""
    if not is_authenticated(user):
        return False
    if from_status not in ALLOWED_STATUS_TRANSITIONS:
        return False
    if to_status not in ALLOWED_STATUS_TRANSITIONS[from_status]:
        return False
    # Admin can do any allowed transition
    if is_admin(user):
        return True
    # Mainteneur : can edit/contest/reject but NOT validate alone
    if user_role(user) == ROLE_MAINTENEUR:
        if to_status == "valide":
            return False  # mainteneur cannot validate without valideur role
        return True
    # Valideur : can validate, contest, reject (cannot send back to propose)
    if user_role(user) == ROLE_VALIDEUR:
        return to_status in ("valide", "conteste", "rejete", "en_revue")
    return False


def require_transition(
    user: Optional[dict], from_status: str, to_status: str,
) -> dict:
    require_authenticated(user)
    if not can_transition_status(user, from_status, to_status):
        raise AuthzError(
            f"Forbidden : role '{user_role(user)}' cannot transition mapping "
            f"from '{from_status}' to '{to_status}'",
            user=user, action=f"transition:{from_status}→{to_status}",
        )
    return user


# ─────────────────────────────────────────────────────────────────────────
# Self-validation rule (§14 #7)
# ─────────────────────────────────────────────────────────────────────────

def is_self_validation(
    user: dict, proposal_proposed_by: Optional[int],
) -> bool:
    """True if the user about to validate IS the user who proposed the change.
    Per §14 #7, this is *allowed* but must be flagged in the audit with
    `is_self_validation=1` and audited with `note='self-validation'`."""
    if not is_authenticated(user):
        return False
    if proposal_proposed_by is None:
        return False
    return user["id"] == proposal_proposed_by


def check_self_validation(
    user: dict, proposal_proposed_by: Optional[int], to_status: str,
) -> bool:
    """Convenience : returns True only if THIS specific validation is a
    self-validation (i.e. transitioning to 'valide' on a proposal you made)."""
    return to_status == "valide" and is_self_validation(user, proposal_proposed_by)


# ─────────────────────────────────────────────────────────────────────────
# Capability matrix (used by UI to show/hide actions)
# ─────────────────────────────────────────────────────────────────────────

# Each capability maps to the minimal role(s) required. Admin bypasses all.
CAPABILITIES: dict[str, tuple[str, ...]] = {
    # Mappings
    "view_mappings":           (ROLE_MAINTENEUR, ROLE_VALIDEUR),  # all roles
    "edit_mapping":            (ROLE_MAINTENEUR,),
    "validate_mapping":        (ROLE_VALIDEUR,),
    "contest_mapping":         (ROLE_MAINTENEUR, ROLE_VALIDEUR),
    "reject_mapping":          (ROLE_MAINTENEUR, ROLE_VALIDEUR),
    # Versions
    "freeze_version":          (),  # admin only
    "compare_versions":        (ROLE_MAINTENEUR, ROLE_VALIDEUR),
    # Imports
    "preview_import":          (ROLE_MAINTENEUR,),
    "apply_import":            (ROLE_MAINTENEUR,),
    # Assignments
    "create_assignment_list":  (ROLE_MAINTENEUR,),
    "edit_assignment_list":    (ROLE_MAINTENEUR,),
    "assign_user_to_list":     (ROLE_MAINTENEUR,),
    # Admin
    "create_user":             (),  # admin only
    "update_user":             (),  # admin only
    "deactivate_user":         (),  # admin only
    "change_app_config":       (),  # admin only
    "set_secret":              (),  # admin only
    "manage_backups":          (),  # admin only
    "refresh_cim11_cache":     (),  # admin only
    # Audit
    "view_audit_full":         (),  # admin only
    "view_audit_own":          (ROLE_MAINTENEUR, ROLE_VALIDEUR),
    # Exports
    "export_complete":         (ROLE_MAINTENEUR, ROLE_VALIDEUR),
    "export_audit":            (),  # admin only
}


def has_capability(user: Optional[dict], capability: str) -> bool:
    """Check if `user` has the named capability. Empty tuple = admin-only."""
    if not is_authenticated(user):
        return False
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability : {capability!r}")
    if is_admin(user):
        return True
    allowed_roles = CAPABILITIES[capability]
    if not allowed_roles:
        return False  # admin-only
    return user_role(user) in allowed_roles


def require_capability(user: Optional[dict], capability: str) -> dict:
    require_authenticated(user)
    if not has_capability(user, capability):
        raise AuthzError(
            f"Forbidden : role '{user_role(user)}' lacks capability '{capability}'",
            user=user, action=capability,
        )
    return user
