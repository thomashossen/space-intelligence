# Space Intelligence

Veille sur le spatial et la défense, en local. Le site collecte des sources publiques gratuites (institutions, presse spécialisée, agrégateurs, recherche), regroupe les articles en événements recoupés, les note, détecte des tendances et rédige une fiche en français pour chaque événement. Chaque information renvoie à ses sources.

## Installer et lancer

Python 3.11 ou plus récent.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install ctranslate2 sentencepiece            # fiches en français hors ligne (mode « local »)
python -m app.pipeline translator install        # modèle de traduction anglais → français (~100 Mo)

python -m app.pipeline run --live                # première collecte → data/pipeline/live.sqlite
uvicorn app.main:app --reload                    # http://127.0.0.1:8000
pytest
```

Ensuite, le bouton **Actualiser** du site fait tout : il revérifie les sources désactivées, collecte, analyse et rédige les fiches manquantes, en affichant sa progression.

## Déploiement gratuit (GitHub Actions + Render)

- **GitHub Actions** (`.github/workflows/collecte.yml`) collecte, analyse et rédige les fiches toutes les
  15 minutes, et à chaque clic sur « Actualiser ». La base est conservée entre deux passages comme fichier
  de la release `data` (`live.sqlite.gz`). Gratuit sans limite dans un dépôt **public**.
- **Render, offre gratuite** (`render.yaml`) sert le site en lecture seule : il télécharge la base au
  démarrage puis toutes les 5 minutes (`app/sync.py`). Sa mise en veille après 15 minutes sans visite ne
  fait rien perdre ; le réveil prend environ une minute.
- « Actualiser » est ouvert à tous, au plus une fois toutes les 15 minutes pour tout le site ; il démarre le
  workflow (ou suit celui déjà en cours) et affiche ses étapes.
- `keepalive.yml` évite que GitHub suspende les collectes programmées après 60 jours sans activité.

Hébergement payant sur un seul serveur avec disque : voir `deploy/render-starter.yaml`.

## Pages

| Adresse | Contenu |
| --- | --- |
| `/` | Accueil : événements à la une, recherche récente, tendances, écosystème, état de la collecte |
| `/actualites` | Tous les événements : recherche, secteur, type, thème, tri, pagination |
| `/evenements/{id}` | Un événement : fiche en français, enjeux, sources avec liens, scores expliqués |
| `/tendances` | Tuiles par secteur et par thème (7 jours, évolution, 12 semaines) et signaux statistiques |
| `/acteurs`, `/acteurs/{id}` | Acteurs cités ; pour chacun : activité, acteurs associés, événements |
| `/collecte` | Dernière collecte, état de chaque source, erreurs, étapes, historique |
| `/conditions` | Conditions d'utilisation (projet à compléter) |
| `/docs` | Documentation de l'API JSON (`/api/events`, `/api/items`, `/api/signals`, `/api/entities`…) |

Les adresses du premier prototype (`/trends`, `/brief`, `/pipeline`…) redirigent vers les pages actuelles.

## Ligne de commande

```bash
python -m app.pipeline run --live             # collecter et analyser (incrémental)
python -m app.pipeline run --live --full-rebuild
python -m app.pipeline status                 # dernières collectes
python -m app.pipeline sources --live         # registre des sources
python -m app.pipeline check                  # interroger chaque source une fois (n'enregistre rien)
python -m app.pipeline check --disable-failing
python -m app.pipeline redact --all           # rédiger toutes les fiches manquantes
python -m app.pipeline translator test "Some English sentence."
```

## Comment ça marche

```
COLLECTE → NORMALISATION → NETTOYAGE → DÉDOUBLONNAGE → ACTEURS ET THÈMES → ÉVÉNEMENTS → SCORES → RELATIONS → SIGNAUX → STOCKAGE → FICHES
```

- **Collecte.** Un connecteur par format (`rss`, `google_news`, `gdelt`, `arxiv`, `hal`, `youtube`). Une source en panne ne bloque jamais les autres ; les agrégateurs sont plafonnés (`max_items`) pour ne pas noyer la presse spécialisée.
- **Dédoublonnage.** Les copies d'un même article sont fusionnées en une *origine* ; seules les origines indépendantes comptent pour recouper un fait.
- **Événements.** Les origines qui racontent le même fait sont regroupées ; des chiffres contradictoires rendent l'événement *contesté*.
- **Scores (0-100), chacun avec son explication.** Importance, crédibilité, nouveauté, portée stratégique (géopolitique, industrie, technologie, approvisionnement, souveraineté).
- **Signaux.** Calculés sur des événements, jamais sur des articles, par rapport aux 12 semaines précédentes. Ce sont des observations, pas des prévisions ; il faut 8 semaines d'historique.
- **Fiches.** Mode `local` (par défaut conseillé, gratuit, hors ligne) : phrases extraites des articles et traduites de l'anglais. D'autres fournisseurs sont possibles dans `config/pipeline/redaction.toml` (Mistral, Groq, Claude, Ollama).

## Configuration sans code

| Pour ajouter | Modifier |
| --- | --- |
| Une source d'un format connu | un bloc `[[source]]` dans `config/pipeline/sources.live.toml`, puis `check` |
| Un acteur ou un alias | un bloc `[[entity]]` dans `entities.toml` |
| Un thème | un bloc `[category.X]` dans `taxonomy.toml` |
| Un seuil ou un poids | `scoring.toml` |
| Le mode de rédaction des fiches | `redaction.toml` |

## Images (facultatives)

Le site fonctionne sans image. Pour l'illustrer, générez des images (avec l'outil de votre choix) d'après les
consignes fournies, nommez-les comme les emplacements, puis importez-les :

```bash
python -m app.images list                      # emplacements : bandeau, rubriques, écosystème, vignettes par type d'événement
python -m app.images prompts > prompts.md      # une consigne par emplacement, même direction artistique bleu nuit
pip install pillow && python -m app.images import ~/Downloads/images-generees
```

Plusieurs variantes par emplacement sont possibles (`ev-launch-2.png`…) ; chaque événement garde toujours la même.

## Organisation

```
app/
├── main.py, config.py, templating.py
├── routes/site.py        pages du site, bouton Actualiser, anciennes adresses
├── routes/data_api.py    API JSON
├── services/site.py      données des pages, en français
├── services/refresh.py   actualisation en arrière-plan
└── pipeline/             collecte, analyse, stockage, fiches (voir les docstrings)
config/pipeline/          sources, acteurs, thèmes, scores, rédaction
templates/site/, static/  gabarits, styles, icônes, polices (Inter, Source Serif 4, OFL)
data/pipeline/live.sqlite base de données (créée par la première collecte)
data/fixtures/, tests/    flux fictifs et vérité terrain pour les tests uniquement
```

## Limites

- Les acteurs viennent d'un référentiel (`entities.toml`) : un acteur absent n'est pas reconnu.
- Les titres Google News arrivent sans texte : leurs fiches restent courtes.
- Les signaux dépendent de la couverture : une source ajoutée en cours de route gonfle les comptes pendant quelques semaines (signalé, non corrigé).
- Les fiches en mode `local` reproduisent des phrases d'articles : avant toute mise en ligne publique, vérifiez le droit voisin des éditeurs de presse et complétez les conditions d'utilisation et les mentions légales.
