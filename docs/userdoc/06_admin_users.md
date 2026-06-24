# Administration — Utilisateurs

**Onglet Administration → 👤 Utilisateurs** (visible pour admins uniquement).

## Vue d'ensemble

Table listant tous les utilisateurs avec :
- Nom (username)
- Nom complet
- Rôle (badge coloré : admin / mainteneur / valideur)
- Statut (actif / désactivé)
- Dernière connexion
- Date de création

## Créer un utilisateur

Champs requis :
- **Username** (unique)
- **Nom complet**
- **Email**
- **Mot de passe initial** (l'utilisateur pourra le changer)
- **Rôle** : Mainteneur / Valideur / Administrateur

Le mot de passe est haché en bcrypt avant stockage. Aucune restriction de
complexité côté serveur (à imposer côté politique organisation).

## Modifier / désactiver

Sélectionner l'utilisateur dans la liste déroulante puis :
- **Nouveau rôle** : changer le rôle.
- **Nouveau mot de passe** : laisser vide pour ne pas changer.
- **Activer/Désactiver** : désactivation soft (l'utilisateur ne peut plus se
  connecter mais ses traces d'audit sont préservées).

Les actions sont auditées (`audit_events` : `admin_user_create`,
`admin_user_update`, `admin_user_deactivate`).

## Matrice de capabilités (RBAC)

Chaque action est filtrée par la capability associée. Voir
`services/authz.py CAPABILITIES`.

| Capability | Admin | Mainteneur | Valideur |
|---|---|---|---|
| `view_mappings` | ✅ | ✅ | ✅ |
| `edit_mapping` | ✅ | ✅ | ❌ |
| `validate_mapping` | ✅ | ❌ | ✅ |
| `contest_mapping` | ✅ | ✅ | ✅ |
| `reject_mapping` | ✅ | ✅ | ✅ |
| `freeze_version` | ✅ | ❌ | ❌ |
| `compare_versions` | ✅ | ✅ | ✅ |
| `preview_import` | ✅ | ✅ | ❌ |
| `apply_import` | ✅ | ✅ | ❌ |
| `create_assignment_list` | ✅ | ✅ | ❌ |
| `edit_assignment_list` | ✅ | ✅ | ❌ |
| `assign_user_to_list` | ✅ | ✅ | ❌ |
| `create_user` / `update_user` / `deactivate_user` | ✅ | ❌ | ❌ |
| `change_app_config` / `set_secret` | ✅ | ❌ | ❌ |
| `manage_backups` / `refresh_cim11_cache` | ✅ | ❌ | ❌ |
| `view_audit_full` / `export_audit` | ✅ | ❌ | ❌ |
| `view_audit_own` | ✅ | ✅ | ✅ |
| `export_complete` | ✅ | ✅ | ✅ |
