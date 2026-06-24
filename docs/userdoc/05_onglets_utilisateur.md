# Onglets utilisateur

## Forward (CIM-10 → CIM-11)

- **Liste paginée** des 42 897 mappings forward (100/page).
- **Filtres** dans la sidebar gauche :
  - Recherche texte (code source ou libellé)
  - Chapitre CIM-10 (01..22 + extension XX)
  - Fiabilité (TRES_HAUTE..NON_RESOLU)
  - Statut (propose..gele)
  - Type de cible (MMS simple / Cluster / Fondation seule / Non mappable)
  - **Source CIM-10** : classant PMSI, CMA, type_code (FR_ONLY, INTERNATIONAL_*)
- **Colonnes affichées** : id, code source, **libellé source**, code cible,
  **libellé cible** (résolution dénormalisée + JOIN cim11_linearizations),
  target_kind, relation, fiabilité, source_decision, statut, flags PMSI.
- **Sélection d'une ligne** : l'onglet Édition se charge automatiquement
  avec ce mapping. Le panneau Bidir est aussi pré-rempli (plan §16.3).
- **Side panel sous la grille** : "Correspondances reverse pour la ligne
  sélectionnée" (top 20 mappings pointant vers la même cible).

## Reverse (CIM-11 → CIM-10)

Symétrique à Forward :
- 18 505 mappings reverse.
- Filtres miroirs : type_code, classant, CMA appliqués à la **cible CIM-10**
  (pas à la source CIM-11 qui n'a pas ces flags).
- Side panel "Correspondances forward".

## Bidir (vue round-trip)

- Saisir un **code source** + direction de départ.
- Affichage en deux colonnes : **Forward** (CIM-10 → CIM-11) et **Reverse**
  (CIM-11 → CIM-10).
- **Indicateur de cohérence** : `STRICT`, `CATEGORIE` (mêmes 3 caractères),
  `DISCORDANT`, `C11_NOT_IN_REVERSE`, `C10_NOT_IN_FORWARD`, `NO_DATA`.
- **Funnel n:1** (plan §16.13) : si plusieurs codes source pointent à la même
  cible, ils apparaissent dans une section "Codes apparentés" (collapsible,
  top 15).
- Pré-rempli automatiquement par sélection dans Forward/Reverse, override
  possible en tapant un code.

## Édition

Formulaire d'édition pour le mapping sélectionné.

**Sections** :
1. **Contexte** : source code + libellé → cible code + libellé.
2. **Cible** :
   - Type de cible (sélecteur adapté à la direction — plan §16.8 strict).
   - **Mode forward** : widget clic-pour-éditer (bouton avec code + libellé,
     ouvre l'EB browser pré-rempli) + toggle "édition manuelle" pour la
     saisie texte. URIs fondation (mode expert). Release MMS.
   - **Mode reverse** : seul `target_cim10_code` est visible.
3. **Relation** : 9 choix (équivalent, plus_large, plus_precis, multiple,
   composite, résiduel, non_mappable, necessite_postcoord, ambigu).
4. **Justification (obligatoire)** :
   - Motif (6 choix)
   - **Problématique éventuelle** (paramétrable via Admin)
   - Commentaire libre
   - Références (URLs / DOI séparés par virgules)
5. **Workflow** : choix de transition (aucune / en_revue / valider / contester / rejeter).
6. **Panneau de droite** : traçabilité pipeline + historique des propositions
   + justifications existantes.

## Mes worklists

- Cartes des listes d'affectation auxquelles l'utilisateur courant est
  rattaché.
- Barre de progression `fait/total`.
- Bouton "Voir les mappings" → ouvre Forward/Reverse pré-filtré sur la liste.

## Exports

6 profils :
- **Complet XLSX** (multi-onglets forward + reverse + métadonnées)
- **PMSI CSV** (colonnes minimales pour groupage)
- **Foundation JSON-LD** (skos:Concept + foundationURIs)
- **Foundation CSV à plat** (1 ligne par URI)
- **Audit CSV** (admin uniquement)
- **Diff entre 2 versions**

Toggle "Mappings validés/gelés uniquement" en haut.

## Documentation

L'onglet où vous êtes : navigation Markdown à gauche, contenu à droite.

## Administration (visible pour admins uniquement)

Voir [`06_admin_users.md`](06_admin_users.md), [`07_admin_parametres.md`](07_admin_parametres.md),
[`08_admin_secrets.md`](08_admin_secrets.md), [`09_admin_backup.md`](09_admin_backup.md),
[`10_admin_listes.md`](10_admin_listes.md).
