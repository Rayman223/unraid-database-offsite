# Une image de générateur par profil

Le contrôleur inspecte un seul conteneur `backup_container` par profil
(`database-backup` par défaut). Son identifiant attendu se configure donc une
seule fois à la racine de `config.json` :

```json
"generator_image_id": "sha256:IDENTIFIANT_LOCAL_VERIFIE"
```

La valeur réelle doit contenir `sha256:` suivi de 64 caractères hexadécimaux
minuscules. Le mode et la preuve de publication restent dans chaque base :

```json
"completion": {
  "mode": "atomic-rename",
  "evidence": "URL du code vérifié à une révision précise"
}
```

Les bases MariaDB et SQLite partagent cet ID quand elles utilisent le même
générateur. Les profils QUA et PRD restent indépendants : chacun conserve une
valeur. `sqlite_restore_image` désigne l'image du **contrôleur de restauration**,
pas celle du générateur ; ce champ reste distinct.

## Migration d'une installation existante

1. Construire une nouvelle image du contrôleur avec ce code, sous un tag distinct
   de l'image encore utilisée. Garder l'ancienne image pour pouvoir revenir en
   arrière. Le code précédent ne sait pas lire l'identifiant global.

   Depuis un checkout contenant ce changement :

   ```bash
   docker build -t unraid-database-offsite:generator-profile .
   docker image inspect unraid-database-offsite:generator-profile --format '{{.Id}}'
   ```

2. Sauvegarder le `config/config.json` privé de chaque profil, avec ses droits
   privés. Relever l'ID et la révision du générateur, sans afficher son
   environnement ni les secrets du YAML :

   ```bash
   docker inspect database-backup --format 'ID={{.Image}} Revision={{index .Config.Labels "org.opencontainers.image.revision"}}'
   ```

3. Vérifier le contrat de publication de cette révision. Reporter son ID dans
   `generator_image_id` à la racine de chaque profil. **Conserver temporairement
   les `completion.generator_image_id` des bases**, tous identiques à cet ID :
   cette configuration est lisible par l'ancien et le nouveau contrôleur.
   Conserver les modes et les preuves `completion.evidence`, actualisées si la
   révision a changé.
4. Sélectionner la nouvelle image dans `OFFSITE_IMAGE`. Pour les profils SQLite,
   reporter son ID local dans `sqlite_restore_image`. Mettre également à jour
   ces références dans les scripts planifiés.

   ```bash
   export OFFSITE_IMAGE=unraid-database-offsite:generator-profile
   ```

5. Exécuter la séquence suivante pour chaque profil avec les chemins/réseau
   habituels et `OFFSITE_ROOT` / `OFFSITE_IMAGE` exportés :

   ```bash
   bash "$OFFSITE_ROOT/run.sh" preflight &&
   flock -w 3600 "$OFFSITE_ROOT/state/schedule.lock" \
     bash "$OFFSITE_ROOT/run.sh" run &&
   bash "$OFFSITE_ROOT/run.sh" monitor &&
   bash "$OFFSITE_ROOT/run.sh" status
   ```

   `monitor` actualise le bilan de surveillance que `status` affiche aussi.
   Après reconstruction du contrôleur, lancer également le contrôle isolé :

   ```bash
   flock -w 3600 "$OFFSITE_ROOT/state/schedule.lock" \
     bash "$OFFSITE_ROOT/run.sh" restore-check
   ```

6. Une fois tous les scripts planifiés du profil basculés et les éventuelles
   exécutions de l'ancienne image terminées, retirer les
   `completion.generator_image_id` des bases. Garder uniquement le champ global,
   puis relancer `preflight` et `monitor`. Ne pas supprimer ces doublons tant
   qu'un script utilise encore l'ancienne image : celle-ci les exige et
   bloquerait la collecte ainsi que les autres commandes.

Aucun dépôt Restic, état, mot de passe ou identifiant de base ne doit être
réinitialisé. Aucune migration SQL n'est nécessaire. Pour un retour à l'ancienne
image du contrôleur, rétablir aussi la configuration sauvegardée et les anciennes
références des scripts planifiés.

Les anciens profils restent compatibles sans réécriture automatique si tous
les contrats `atomic-rename` portent le même ID valide. Un champ global et des
champs historiques identiques peuvent coexister pendant la transition ; toute
divergence est refusée, sans priorité silencieuse. Sans champ global, un ID
manquant sur une base en renommage atomique est également refusé.

Un profil entièrement en `success-marker` n'a pas besoin de cet ID. Dans un
profil mixte, le contrôle de l'image s'applique aux bases `atomic-rename` ; les
marqueurs de succès restent vérifiés indépendamment.

## Éviter une nouvelle interruption après mise à jour

L'ID change dès que le contenu de l'image change, même pour une dépendance.
Le contrôleur bloque alors la collecte des bases en renommage atomique jusqu'à
nouvelle vérification. Les dumps locaux peuvent continuer à être produits.

Pour épingler le générateur, relever le digest de l'image **effectivement
installée**, sans tirer à nouveau le tag mobile `latest` :

```bash
docker inspect database-backup --format '{{.Image}}'
docker image inspect ID_LOCAL_RELEVE --format '{{range .RepoDigests}}{{println .}}{{end}}'
```

Dans le modèle Unraid, utiliser la référence retournée pour le dépôt du
générateur (`ghcr.io/greite/database-backup@sha256:...`) à la place de `:latest`.
Ce digest de dépôt et l'ID local `generator_image_id` sont deux identifiants
différents : conserver l'ID local dans le JSON. Une mise à jour volontaire reste
possible après vérification et actualisation de cet unique champ par profil.
