# Administration — Secrets API

**Onglet Administration → 🔑 Secrets API** (visible pour admins uniquement).

## Principe

Les clés API tierces sont **chiffrées en base** via [Fernet](https://cryptography.io/en/latest/fernet/)
(AES-128-CBC + HMAC-SHA256). La clé Fernet elle-même est fournie via la
variable d'environnement `DB_ENCRYPTION_KEY` (jamais stockée en DB).

## Clés gérées

- `WHO_CLIENT_SECRET` : secret OAuth2 pour l'API CIM-11 OMS.
- `MISTRAL_API_KEY` : clé Mistral (utilisée en V2 pour l'assistance LLM).
- `AWS_SECRET_ACCESS_KEY` : secret S3 pour la persistance des snapshots.

## Mise à jour d'un secret

1. Sélectionner la **clé** dans le menu déroulant.
2. Saisir la **nouvelle valeur** dans le champ password (masqué).
3. Cliquer sur **Enregistrer (chiffré)**.

La valeur est chiffrée par Fernet et stockée dans `app_config.value` avec
`is_secret=1`. Elle ne sera **jamais affichée en clair** dans l'interface après
saisie.

## Comportement runtime

Au démarrage de l'application (`app.py`), chaque secret est :
1. Lu depuis l'environnement (variable d'env shinyapps.io).
2. Si absent en env mais présent en DB → l'env var est définie depuis la valeur déchiffrée.
3. Si présent en env mais absent en DB → la valeur d'env est chiffrée et stockée.

Ce double mécanisme garantit la persistance entre redémarrages de container
sans dépendre des variables d'env (qui peuvent disparaître si l'admin les
oublie de re-déclarer).

## Audit

Chaque enregistrement de secret produit un événement audit :
- action = `admin_secret_set`
- object_id = nom de la clé
- note = `"secret stored (encrypted)"`

La valeur n'apparaît **pas** dans l'audit (pour ne pas créer de fuite).

## Génération de `DB_ENCRYPTION_KEY`

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

À configurer dans les variables d'env shinyapps.io (App Settings → Variables).
