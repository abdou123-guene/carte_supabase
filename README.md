# Carte des tronçons

Application FastAPI + Leaflet qui affiche la vue `geo_standards.v_troncon_pgrouting` (Supabase / PostGIS).

## Lancer en local
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # puis renseignez DATABASE_URL
uvicorn main:app --reload
```
Ouvrir http://localhost:8000 — la liste des colonnes de la vue est sur `/api/colonnes`.

## Mettre sur GitHub
```bash
git init
git add .
git commit -m "Carte des tronçons"
git branch -M main
git remote add origin https://github.com/VOTRE_COMPTE/carte-troncons.git
git push -u origin main
```

## Déployer (Render)
New > Web Service > votre dépôt GitHub
- Build : `pip install -r requirements.txt`
- Start : `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment : ajoutez `DATABASE_URL`, `DB_SCHEMA`, `DB_VIEW`
