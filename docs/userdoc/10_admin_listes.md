# Administration — Listes & affectations

**Onglet Administration → 📋 Listes & assignations** (visible pour admins uniquement).

## Vue d'ensemble

Une **assignment_list** est un ensemble de codes (CIM-10 ou CIM-11) qu'un ou
plusieurs utilisateurs ont la responsabilité de traiter. Deux types :

- **Statique** : liste explicite de codes (chargée une fois).
- **Dynamique** : filtres SQL (re-évalués à chaque consultation).

## Créer une liste statique

1. **Nom** : libellé court de la liste.
2. **Direction** : forward / reverse / les deux.
3. **Codes** : un par ligne ou séparés par virgule.
4. **Description** : contexte de la liste.
5. Cliquer **Créer liste statique**.

## Créer une liste dynamique (par filtres)

1. **Nom** + **Direction**.
2. Choisir un **chapitre** (optionnel).
3. Choisir **Fiabilité** (multi-sélection).
4. Choisir **Statut** (par défaut : propose, en_revue, conteste).
5. Si forward : cocher **classant PMSI** pour ne garder que les codes classant.
6. Cliquer **Aperçu (count)** pour voir combien de mappings seront ciblés.
7. Cliquer **Créer liste dynamique**.

## Éditer une liste existante

1. Sélectionner la **liste** à éditer.
2. Modifier le nom et/ou la description.
3. Cliquer **Mettre à jour la liste**.

## Affecter un utilisateur à une liste

1. Sélectionner la **liste** et l'**utilisateur**.
2. Choisir le **rôle attendu** : mainteneur (édite) ou valideur (valide).
3. Définir une **échéance** (optionnel).
4. Cliquer **Affecter**.

> ⚠️ Une combinaison (liste, user, rôle) est unique. Si elle existe déjà,
> l'opération est rejetée avec un toast d'erreur.

## Vue utilisateur côté mainteneur/valideur

L'onglet **Mes worklists** (visible pour tous) affiche :
- Cartes des assignments de l'utilisateur courant.
- Barre de progression `fait/total`.
- Bouton pour ouvrir les mappings filtrés sur la liste.

## Filtres mappings par worklist

Quand un mainteneur clique « Voir les mappings » sur une worklist :
- L'onglet Forward (ou Reverse, selon la direction de la liste) s'ouvre.
- Un filtre invisible `assignment_list_id` restreint la grille aux codes
  de la liste.
- L'utilisateur peut combiner avec d'autres filtres (fiabilité, statut, …).

## Audit

Toutes les actions sont auditées :
- `admin_list_create`, `admin_list_update` (sur les listes).
- `admin_assign` (sur les affectations).
