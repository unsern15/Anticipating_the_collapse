# Anticipating_the_collapse

Tableau de fragilité (Europe, US, Asie) et grille de paliers d'achat sur le Stoxx 600.

## Comment ça marche

- `index.html` : la page, publiée sur GitHub Pages.
- `scripts/fetch_data.py` : va chercher les indicateurs sur des sources publiques (FRED, Cboe, BCE, Yahoo Finance) et écrit `data/latest.json`.
- `.github/workflows/update-data.yml` : le robot qui lance le script chaque soir de semaine et enregistre le résultat.

Si une source ne répond pas, rien n'est inventé : l'indicateur repasse en saisie manuelle sur la page.

Lancer le robot à la main : onglet **Actions** > « Mise à jour des données » > **Run workflow**.
