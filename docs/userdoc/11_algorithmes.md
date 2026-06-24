# Algorithmes & calculs

## Décomposition des clusters CIM-11

Un cluster est une chaîne `BA00`, `BA00&XN8P1` ou `1G40/1B5Z` représentant un
mapping post-coordonné CIM-11.

**Conventions OMS** :
- `BA00` : un seul stem (mapping simple).
- `BA00&XN8P1` : un stem + 1+ specifiers (séparés par `&`).
- `1G40/1B5Z` : deux stems combinés (séparés par `/`).
- `1A00&XN8P1/1B5Z` : multi-stem avec specifiers sur le 1er stem.

**Algorithme** (`services/foundation.decompose_cluster_string`) :
1. Split sur `/` → segments (chaque segment commence par un stem).
2. Pour chaque segment, split sur `&` → premier token = stem, suivants = specifiers.
3. Chaque composant est typé `stem` ou `specifier` avec une position.

## Résolution MMS → fondation

Pour un code MMS donné dans une release, on cherche la (les) URI(s) fondation
associée(s).

**Algorithme** (`services/foundation.resolve_mms_to_foundation`) :
1. Décomposer le code en composants.
2. Pour chaque composant, lire `cim11_linearizations.foundation_uris` (JSON array).
3. Aggregér toutes les URIs sans doublon (ordre préservé : stem en premier).

Si un composant n'est pas dans le cache local → `KeyError` (l'utilisateur doit
lancer le bootstrap CIM-11).

## Cohérence round-trip (Bidir)

Pour un mapping `A → B` (forward) et `B → A'` (reverse), on évalue :

| Code | Sémantique |
|---|---|
| `STRICT` | `A == A'` (round-trip parfait) |
| `CATEGORIE` | `A[:3] == A'[:3]` (même catégorie 3 caractères CIM-10) |
| `DISCORDANT` | A et A' différents (problème potentiel) |
| `C11_NOT_IN_REVERSE` | Le code MMS cible n'apparaît pas en reverse |
| `C10_NOT_IN_FORWARD` | Le code CIM-10 reverse n'apparaît pas en forward |
| `NO_DATA` | Aucun mapping trouvé |

En cas de **funnel** (plusieurs forward × plusieurs reverse), on calcule la
matrice de cohérence (toutes les paires) et on retient le meilleur cas
(STRICT > CATEGORIE > DISCORDANT).

## Détection du `target_kind` à l'ingestion

À l'ingestion du seed XLSX, le `target_kind` est dérivé du code cible :

| Cible | `target_kind` |
|---|---|
| Vide / None | `non_mappable` |
| Contient `&` ou `/` | `mms_cluster` |
| Sinon | `mms_simple` |

Le mode `foundation_only` n'est pas auto-détecté à l'ingestion (pas d'URI
fondation explicite dans le seed) ; il est utilisé uniquement lors d'édition
manuelle expert.

## Dérivation du `relation_type`

À l'ingestion (best-effort) :

| `target_kind` | `source_decision` | `relation_type` |
|---|---|---|
| `non_mappable` | — | `non_mappable` |
| `mms_cluster` | — | `composite` |
| `mms_simple` | `BIDIR_CONTESTE` | `ambigu` |
| `mms_simple` | contient `POST_COORD` | `necessite_postcoord` |
| `mms_simple` | `HERITAGE` | `plus_large` |
| `mms_simple` | autre | `equivalent` |

Le curateur peut affiner manuellement dans l'onglet Édition.

## `is_self_validation` (§14 #7)

Lors d'une transition `→ valide` sur un mapping :
1. Lire `mapping_proposals` : trouver la proposition la plus récente.
2. Si `proposed_by == current_user.id` → flag `is_self_validation = 1` +
   note audit `"self-validation"`.

Permet à l'admin de surfacer les cas où un même utilisateur a édité ET validé,
sans pour autant bloquer le workflow (équipe restreinte ATIH).

## Chapitre CIM-10 (mapping range → numéro 01..22)

Le caractère initial du code (A..Z) + le préfixe 3 caractères déterminent le
chapitre :

| Lettre | Plage | Chapitre |
|---|---|---|
| A, B | A00-B99 | 01 (infectieuses) |
| C, D | C00-D48 | 02 (tumeurs) |
| D | D50-D89 | 03 (sang) |
| E | E00-E90 | 04 (métabolisme) |
| F | F00-F99 | 05 (mental) |
| G | G00-G99 | 06 (nerveux) |
| H | H00-H59 | 07 (œil) |
| H | H60-H95 | 08 (oreille) |
| I | I00-I99 | 09 (circulatoire) |
| J | J00-J99 | 10 (respiratoire) |
| K | K00-K93 | 11 (digestif) |
| L | L00-L99 | 12 (peau) |
| M | M00-M99 | 13 (ostéo-articulaire) |
| N | N00-N99 | 14 (génito-urinaire) |
| O | O00-O99 | 15 (grossesse) |
| P | P00-P96 | 16 (périnatal) |
| Q | Q00-Q99 | 17 (malformations) |
| R | R00-R99 | 18 (symptômes/signes) |
| S, T | S00-T98 | 19 (lésions traumatiques) |
| V, W, X, Y | V01-Y98 | 20 (causes externes) |
| Z | Z00-Z99 | 21 (facteurs influant) |
| U | U00-U99 | 22 (usage spécial) |

Implémentation : `services/ingest.cim10_chapter()`.
