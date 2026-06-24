# Workflows

## Workflow d'édition d'un mapping

1. **Mainteneur** ouvre l'onglet Forward ou Reverse, applique des filtres.
2. Sélectionne une ligne → onglet Édition se charge automatiquement.
3. Modifie la cible (3 modes en forward : MMS simple / Cluster / Fondation directe).
4. Choisit un `relation_type`.
5. Renseigne la **justification obligatoire** : motif, problématique, commentaire, références.
6. Optionnellement, change le statut (`en_revue` pour envoyer à un valideur).
7. Clique **Enregistrer**.
8. Audit appendé, ancienne valeur snapshotée dans `mapping_proposals`, foundation links resyncés.

## Workflow de validation

1. **Valideur** consulte les mappings en `en_revue` (filtre statut).
2. Vérifie l'historique des propositions + justifications.
3. Choisit le nouveau statut `valide`, `conteste` ou `rejete`.
4. Enregistre avec une justification.
5. Si auto-validation (= valideur valide sa propre proposition), un flag
   `is_self_validation=1` est tracé + note audit dédiée (§14 #7).

## Workflow de gel d'une version

1. **Admin** ouvre l'onglet Versions (ou utilise l'API).
2. Lance le gel : tous les mappings actifs sont snapshotés dans `version_mappings_snapshot`.
3. Le label de la version est saisi (ex. `v2026.06_ATIH`).
4. Manifest SHA-256 généré, snapshot uploadé en S3.
5. Mappings figés → statut `gele` (immuables).

## Transitions de statut (RBAC)

Matrice des transitions autorisées par rôle :

| De \ Vers | propose | en_revue | valide | conteste | rejete | gele |
|---|---|---|---|---|---|---|
| **propose** | — | M, V, A | A | — | M, V, A | — |
| **en_revue** | — | — | V, A | V, A | M, V, A | — |
| **valide** | — | M, V, A | — | M, V, A | — | A (via gel) |
| **conteste** | — | M, V, A | V, A | — | M, V, A | — |
| **rejete** | — | M, V, A | — | — | — | — |
| **gele** | (aucune transition possible) | | | | | |

Légende : M = Mainteneur, V = Valideur, A = Admin.

**Règles clés** :
- Un mainteneur ne peut pas valider (réservé valideurs).
- Un valideur peut valider ses propres modifications mais l'auto-validation
  est tracée explicitement (§14 #7).
- L'admin peut faire toutes les transitions autorisées.
- `gele` est terminal.

## Workflow d'import massif

Voir [`07_admin_parametres.md`](07_admin_parametres.md) (paramètre
`default_precedence_policy`) et la pipeline source `../transcodage/` pour les
imports en masse.

## Append-only

Deux tables sont strictement append-only (triggers SQL) :

- **`audit_events`** : aucun UPDATE/DELETE possible.
- **`mapping_proposals`** : aucun UPDATE possible (sauf CASCADE de mapping).

Toute violation lève `sqlite3.IntegrityError: append-only`.

## Self-validation (§14 #7)

Quand un valideur (V) ou admin (A) :
1. Édite un mapping (→ `mapping_proposals` enregistre `proposed_by = V`).
2. Valide ensuite ce même mapping (transition → `valide`).

Alors :
- `mappings.is_self_validation` passe à `1`.
- L'`audit_events.note` reçoit `"self-validation"`.
- Le dashboard admin peut surfacer ces cas pour surveillance.
