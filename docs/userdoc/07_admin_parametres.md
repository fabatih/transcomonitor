# Administration — Paramètres

**Onglet Administration → ⚙️ Paramètres** (visible pour admins uniquement).

Cet onglet gère deux types de configuration :

## 1. Paramètres applicatifs (clé/valeur)

### Comment ajouter ou modifier un paramètre

1. Saisir une **clé** existante (visible dans la table en haut) **OU** une
   nouvelle clé.
2. Saisir la **valeur** souhaitée dans le champ « Valeur ».
3. Cliquer sur **Enregistrer**. Un toast confirme la sauvegarde.

> ⚠️ Pour les **secrets** (clés API), utiliser l'onglet 🔑 **Secrets API**
> (chiffrement Fernet).

### Clés notables livrées

| Clé | Sémantique | Valeur typique |
|---|---|---|
| `audit_capture_request_meta` | Enregistre IP + UA dans l'audit | `0` (off, défaut) ou `1` (on) |
| `default_precedence_policy` | Politique d'import par défaut | `auto_unless_valid`, `never_override_valid`, `always_override`, `manual_per_row` |

Vous pouvez librement ajouter des clés personnalisées (ex. `support_email`,
`max_export_rows`, …) — l'application les ignore si non utilisées.

## 2. Typologie des problématiques de transcodage (plan §16.7)

Cette liste paramétrable alimente le sélecteur « Problématique éventuelle »
dans l'onglet **Édition** (section Justification).

### Table

Affiche les types existants avec :
- **Code** (identifiant unique)
- **Libellé** (badge coloré)
- **Description**
- **Couleur** (badge Bootstrap)
- **Ordre** (sort_order — plus petit = affiché en premier)
- **Statut** (actif / désactivé)

### Créer un nouveau type

1. Saisir un **code** unique sans espaces (ex. `lignes_perdues`).
2. Saisir le **libellé** (visible en badge).
3. Choisir la **couleur** Bootstrap (primary, success, warning, danger, info, …).
4. Définir l'**ordre** d'affichage (numérique, ex. 100).
5. Saisir une **description** (visible au survol / dans la doc).
6. Cliquer sur **Créer**.

### Modifier ou désactiver un type

1. Sélectionner le code existant dans la liste déroulante.
2. Modifier libellé/couleur/ordre (les valeurs vides ne sont pas mises à jour).
3. Cliquer sur **Mettre à jour**.
4. Pour désactiver / réactiver, cliquer sur **Activer/Désactiver**.

Les types désactivés ne sont plus proposés dans le sélecteur d'édition mais
restent visibles dans les anciennes justifications (pour préserver l'historique).

Toutes les actions sont auditées via `audit_events` :
- action = `admin_config_change`
- object_type = `config`
- object_id = `problematique_types/<code>`
