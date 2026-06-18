"""tests/test_authz.py — Tests for services/authz.py"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import authz
from services.authz import (
    AuthzError, ROLE_ADMIN, ROLE_MAINTENEUR, ROLE_VALIDEUR,
    can_transition_status, check_self_validation, has_capability, has_role,
    is_admin, is_authenticated, is_mainteneur, is_self_validation, is_valideur,
    require_admin, require_authenticated, require_capability, require_role,
    require_transition, user_role,
)


# Fixtures : minimal user dicts mimicking the row returned by authenticate()
@pytest.fixture
def admin():
    return {"id": 1, "username": "alice_admin", "role": "admin"}


@pytest.fixture
def mainteneur():
    return {"id": 2, "username": "bob_maint", "role": "mainteneur"}


@pytest.fixture
def valideur():
    return {"id": 3, "username": "carla_valid", "role": "valideur"}


@pytest.fixture
def anonymous():
    return None


# ─────────────────────────────────────────────────────────────────────────
# Predicates
# ─────────────────────────────────────────────────────────────────────────

class TestPredicates:
    def test_user_role_extraction(self, admin, anonymous):
        assert user_role(admin) == "admin"
        assert user_role(anonymous) is None
        assert user_role({}) is None

    def test_has_role_admin_overrides(self, admin):
        assert has_role(admin, "mainteneur")
        assert has_role(admin, "valideur")
        assert has_role(admin, "admin")

    def test_has_role_strict(self, mainteneur, valideur):
        assert has_role(mainteneur, "mainteneur")
        assert not has_role(mainteneur, "valideur")
        assert not has_role(mainteneur, "admin")
        assert has_role(valideur, "valideur")
        assert not has_role(valideur, "mainteneur")

    def test_has_role_anonymous(self, anonymous):
        assert not has_role(anonymous, "admin")
        assert not has_role(anonymous, "mainteneur", "valideur")

    def test_is_admin(self, admin, mainteneur, valideur, anonymous):
        assert is_admin(admin)
        assert not is_admin(mainteneur)
        assert not is_admin(valideur)
        assert not is_admin(anonymous)

    def test_is_mainteneur_includes_admin(self, admin, mainteneur, valideur):
        assert is_mainteneur(admin)
        assert is_mainteneur(mainteneur)
        assert not is_mainteneur(valideur)

    def test_is_valideur_includes_admin(self, admin, valideur, mainteneur):
        assert is_valideur(admin)
        assert is_valideur(valideur)
        assert not is_valideur(mainteneur)

    def test_is_authenticated(self, admin, anonymous):
        assert is_authenticated(admin)
        assert not is_authenticated(anonymous)
        assert not is_authenticated({})  # no id
        assert not is_authenticated({"username": "x"})  # no id


# ─────────────────────────────────────────────────────────────────────────
# Enforcement
# ─────────────────────────────────────────────────────────────────────────

class TestEnforcement:
    def test_require_authenticated_ok(self, admin):
        assert require_authenticated(admin) is admin

    def test_require_authenticated_fails(self, anonymous):
        with pytest.raises(AuthzError, match="Authentication required"):
            require_authenticated(anonymous)

    def test_require_role_admin_passes_all(self, admin):
        assert require_role(admin, "mainteneur", action="x") is admin
        assert require_role(admin, "valideur", action="x") is admin

    def test_require_role_match(self, valideur):
        assert require_role(valideur, "valideur", action="validate") is valideur

    def test_require_role_denied(self, mainteneur):
        with pytest.raises(AuthzError, match="cannot perform 'validate_mapping'"):
            require_role(mainteneur, "valideur", action="validate_mapping")

    def test_require_admin_passes_admin(self, admin):
        assert require_admin(admin, action="freeze") is admin

    def test_require_admin_fails_others(self, mainteneur, valideur):
        with pytest.raises(AuthzError):
            require_admin(mainteneur, action="freeze")
        with pytest.raises(AuthzError):
            require_admin(valideur, action="freeze")

    def test_authz_error_carries_metadata(self, mainteneur):
        try:
            require_role(mainteneur, "valideur", action="validate")
        except AuthzError as e:
            assert e.user == mainteneur
            assert e.action == "validate"
            assert "valideur" in e.required_role


# ─────────────────────────────────────────────────────────────────────────
# Workflow transitions
# ─────────────────────────────────────────────────────────────────────────

class TestStatusTransitions:
    def test_admin_can_do_any_allowed_transition(self, admin):
        # All transitions in the matrix
        for from_st, allowed in authz.ALLOWED_STATUS_TRANSITIONS.items():
            for to_st in allowed:
                assert can_transition_status(admin, from_st, to_st), (
                    f"admin should be allowed {from_st} → {to_st}"
                )
        # But cannot do an undefined transition
        assert not can_transition_status(admin, "valide", "propose")
        assert not can_transition_status(admin, "gele", "valide")  # gele is terminal

    def test_mainteneur_cannot_validate(self, mainteneur):
        assert not can_transition_status(mainteneur, "propose", "valide")
        assert not can_transition_status(mainteneur, "en_revue", "valide")
        assert not can_transition_status(mainteneur, "conteste", "valide")

    def test_mainteneur_can_edit_propose_reject(self, mainteneur):
        # propose → en_revue (sending for review)
        assert can_transition_status(mainteneur, "propose", "en_revue")
        # propose → rejete (own rejection)
        assert can_transition_status(mainteneur, "propose", "rejete")
        # valide → en_revue (editing a validated mapping)
        assert can_transition_status(mainteneur, "valide", "en_revue")

    def test_valideur_can_validate_and_contest(self, valideur):
        assert can_transition_status(valideur, "en_revue", "valide")
        assert can_transition_status(valideur, "en_revue", "conteste")
        assert can_transition_status(valideur, "en_revue", "rejete")
        assert can_transition_status(valideur, "conteste", "valide")

    def test_valideur_cannot_send_back_to_propose(self, valideur):
        # valideur cannot reset to 'propose' (that's mainteneur's flow)
        # (but the matrix doesn't allow it anyway from any state)
        assert not can_transition_status(valideur, "en_revue", "propose")
        assert not can_transition_status(valideur, "valide", "propose")

    def test_gele_is_immutable(self, admin, valideur, mainteneur):
        # No transition from 'gele' for anyone
        for u in [admin, valideur, mainteneur]:
            assert not can_transition_status(u, "gele", "valide")
            assert not can_transition_status(u, "gele", "en_revue")
            assert not can_transition_status(u, "gele", "conteste")

    def test_anonymous_cannot_transition(self, anonymous):
        assert not can_transition_status(anonymous, "propose", "en_revue")

    def test_require_transition_raises(self, mainteneur):
        with pytest.raises(AuthzError, match="cannot transition"):
            require_transition(mainteneur, "en_revue", "valide")

    def test_require_transition_passes(self, valideur):
        assert require_transition(valideur, "en_revue", "valide") is valideur


# ─────────────────────────────────────────────────────────────────────────
# Self-validation (§14 #7)
# ─────────────────────────────────────────────────────────────────────────

class TestSelfValidation:
    def test_detects_same_user(self, valideur):
        # valideur (id=3) validates a proposal proposed_by user 3
        assert is_self_validation(valideur, 3)

    def test_detects_different_user(self, valideur):
        assert not is_self_validation(valideur, 2)  # proposed by someone else

    def test_anonymous_never_self(self, anonymous):
        assert not is_self_validation(anonymous, 1)

    def test_proposed_by_none(self, valideur):
        # proposal is from import/system (no user)
        assert not is_self_validation(valideur, None)

    def test_check_self_validation_only_on_valide(self, valideur):
        # Same user, transitioning to 'valide' → flagged
        assert check_self_validation(valideur, proposal_proposed_by=3, to_status="valide")
        # Same user, but transitioning to something else → not flagged
        assert not check_self_validation(valideur, proposal_proposed_by=3, to_status="conteste")
        # Different user, transitioning to 'valide' → not flagged
        assert not check_self_validation(valideur, proposal_proposed_by=99, to_status="valide")


# ─────────────────────────────────────────────────────────────────────────
# Capability matrix
# ─────────────────────────────────────────────────────────────────────────

class TestCapabilities:
    def test_admin_has_all_capabilities(self, admin):
        for cap in authz.CAPABILITIES:
            assert has_capability(admin, cap), f"admin lacks {cap}"

    def test_admin_only_capabilities(self, mainteneur, valideur):
        admin_only = ["freeze_version", "create_user", "update_user",
                      "deactivate_user", "change_app_config", "set_secret",
                      "manage_backups", "refresh_cim11_cache",
                      "view_audit_full", "export_audit"]
        for cap in admin_only:
            assert not has_capability(mainteneur, cap), f"mainteneur should not have {cap}"
            assert not has_capability(valideur, cap), f"valideur should not have {cap}"

    def test_mainteneur_capabilities(self, mainteneur):
        assert has_capability(mainteneur, "edit_mapping")
        assert has_capability(mainteneur, "preview_import")
        assert has_capability(mainteneur, "apply_import")
        assert has_capability(mainteneur, "create_assignment_list")
        assert not has_capability(mainteneur, "validate_mapping")

    def test_valideur_capabilities(self, valideur):
        assert has_capability(valideur, "validate_mapping")
        assert has_capability(valideur, "view_mappings")
        assert not has_capability(valideur, "edit_mapping")
        assert not has_capability(valideur, "preview_import")

    def test_view_mappings_for_all_authenticated(self, admin, mainteneur, valideur, anonymous):
        for u in [admin, mainteneur, valideur]:
            assert has_capability(u, "view_mappings")
        assert not has_capability(anonymous, "view_mappings")

    def test_unknown_capability_raises(self, admin):
        with pytest.raises(ValueError, match="unknown capability"):
            has_capability(admin, "secret_backdoor")

    def test_require_capability_raises(self, mainteneur):
        with pytest.raises(AuthzError, match="lacks capability 'freeze_version'"):
            require_capability(mainteneur, "freeze_version")

    def test_require_capability_passes(self, valideur):
        assert require_capability(valideur, "validate_mapping") is valideur

    def test_anonymous_has_no_capability(self, anonymous):
        for cap in authz.CAPABILITIES:
            assert not has_capability(anonymous, cap)
