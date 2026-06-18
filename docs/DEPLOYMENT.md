# Déploiement sur shinyapps.io

## Prérequis

1. **Compte shinyapps.io** : `fabatih` avec plan Standard (cf. §14 #3 du plan).
2. **Token shinyapps.io** : via dashboard → Tokens → Show.
3. **rsconnect-python** :
   ```bash
   pip install rsconnect-python
   rsconnect add --account fabatih --name fabatih \
       --token YOUR_TOKEN --secret YOUR_SECRET
   ```

## Variables d'environnement à configurer

Côté shinyapps.io dashboard → App Settings → Variables, créer :

| Variable | Description | Obligatoire |
|---|---|---|
| `WHO_CLIENT_ID` | OAuth2 ICD-11 | ✅ |
| `WHO_CLIENT_SECRET` | OAuth2 ICD-11 | ✅ |
| `AWS_ACCESS_KEY_ID` | Persistance S3 | ✅ |
| `AWS_SECRET_ACCESS_KEY` | Persistance S3 | ✅ |
| `S3_BUCKET` | `transcomonitor` | ✅ |
| `S3_REGION` | `eu-west-3` | ✅ |
| `DEFAULT_ADMIN_PASS` | Mot de passe admin initial | Recommandé |
| `DB_ENCRYPTION_KEY` | Clé Fernet (32 bytes b64) pour secrets en DB | Recommandé |
| `MISTRAL_API_KEY` | LLM assist (V2) | Optionnel |
| `S3_ENDPOINT_URL` | Si bucket non-AWS | Optionnel |

Génération de `DB_ENCRYPTION_KEY` :
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Premier déploiement

```bash
cd /path/to/transcomonitor
./scripts/deploy_shinyapps.sh --new
```

## Mises à jour

```bash
./scripts/deploy_shinyapps.sh
```

## ⚠️ Configuration critique post-déploiement

Dans shinyapps.io dashboard → App Settings → General :

- **Max worker processes : 1** (force `max-instances=1` — cf. §14 #3)
  Indispensable pour la safety du modèle SQLite+S3 sans race conditions
  entre containers.

- **Instance idle timeout : 15 min** (compromis responsivité / heures actives)

## Architecture déployée

```
shinyapps.io container (1 instance forcée)
├── /tmp/transcomonitor.sqlite      (DB locale, restaurée depuis S3 au boot)
├── /tmp/transcomonitor.sqlite.s3tmp (debounced sync copy)
└── app.py (Shiny + WHO proxy ASGI)

      ↕ (async upload debounced toutes les 10s)

AWS S3 (eu-west-3, bucket `transcomonitor`, versioning activé)
├── db/transcomonitor.sqlite       (canonical state)
├── snapshots/<version_label>/...  (frozen_versions)
└── cache/cim11_*.json             (reference caches)
```

## Premier boot sur shinyapps.io

Au premier boot avec une DB vide :
1. `restore_db_from_s3_if_empty` vérifie S3 → 404 → on continue
2. `init_db` crée le schéma
3. Auto-seed depuis `data/seed/transcodage_pipeline_complete.xlsx`
   (~25s pour 61 402 mappings)
4. `ensure_default_admin` crée le compte admin avec `DEFAULT_ADMIN_PASS`
5. Le DB sera uploadée en S3 à la première opération

## Validation post-déploiement

1. Se connecter avec `admin` / `DEFAULT_ADMIN_PASS`
2. Aller dans **Administration → Backup & cache**
3. Vérifier que S3 est OK (test connexion + sauvegarde manuelle)
4. Aller dans **Forward** → vérifier 42 897 mappings
5. (Optionnel) Lancer le bootstrap CIM-11 cache (3h) :
   ```bash
   # Local depuis votre poste
   python3 -m scripts.bootstrap_cim11_refs \
       --from-csv data/seed/transcodage_pipeline_complete.xlsx \
       --release 2024-01 --upload-s3
   ```
   Puis dans l'app : **Admin → Backup → Re-tester S3** pour confirmer.

## Logs

shinyapps.io dashboard → Logs montre :
- Stdout du process (`print(...)` dans le code Python)
- Stderr (exceptions non-attrapées)
- Logs uvicorn/gunicorn
- Audit `[transcomonitor]` prefixes
