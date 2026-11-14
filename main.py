import json
import os
import re
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg_pool import ConnectionPool

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
SCHEMA = os.getenv("DB_SCHEMA", "geo_standards")
VIEW = os.getenv("DB_VIEW", "v_troncon_pgrouting")
GEOM_COLUMN = os.getenv("GEOM_COLUMN") or None
MAX_FEATURES = 5000

# Le pooler Supabase en mode transaction n'accepte pas les prepared statements
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=False,
                      kwargs={"prepare_threshold": None})
geom_col = GEOM_COLUMN


@asynccontextmanager
async def lifespan(app: FastAPI):
    global geom_col
    pool.open()
    if not geom_col:
        geom_col = detect_geom_column()
    yield
    pool.close()


app = FastAPI(title="Carte des tronçons", lifespan=lifespan)


def detect_geom_column() -> str:
    """Trouve la colonne géométrie de la vue (geometry_columns inclut les vues)."""
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT f_geometry_column FROM public.geometry_columns
               WHERE f_table_schema = %s AND f_table_name = %s LIMIT 1""",
            (SCHEMA, VIEW),
        ).fetchone()
        if not row:
            # Les vues n'y figurent pas toujours : on cherche par type de colonne
            row = conn.execute(
                """SELECT a.attname
                   FROM pg_attribute a
                   JOIN pg_class c ON c.oid = a.attrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   JOIN pg_type t ON t.oid = a.atttypid
                   WHERE n.nspname = %s AND c.relname = %s
                     AND t.typname IN ('geometry', 'geography')
                     AND a.attnum > 0 AND NOT a.attisdropped
                   LIMIT 1""",
                (SCHEMA, VIEW),
            ).fetchone()
    if not row:
        raise RuntimeError(
            f"Aucune colonne géométrie trouvée dans {SCHEMA}.{VIEW}. "
            "Renseignez GEOM_COLUMN dans .env."
        )
    return row[0]


@app.get("/api/colonnes")
def colonnes():
    """Liste les colonnes de la vue (utile pour vérifier ce qui est disponible)."""
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT a.attname, format_type(a.atttypid, a.atttypmod)
               FROM pg_attribute a
               JOIN pg_class c ON c.oid = a.attrelid
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relname = %s
                 AND a.attnum > 0 AND NOT a.attisdropped
               ORDER BY a.attnum""",
            (SCHEMA, VIEW),
        ).fetchall()
    return {"vue": f"{SCHEMA}.{VIEW}", "geometrie": geom_col,
            "colonnes": [{"nom": r[0], "type": r[1]} for r in rows]}


@app.get("/api/troncons")
def troncons(bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat")):
    """Renvoie les tronçons en GeoJSON (WGS84), filtrés par emprise si fournie."""
    g = sql.Identifier(geom_col)
    geom4326 = sql.SQL("ST_Transform(ST_SetSRID({g}::geometry, COALESCE(NULLIF(ST_SRID({g}::geometry), 0), 4326)), 4326)").format(g=g)

    where = sql.SQL("")
    params: list = []
    if bbox:
        if not re.fullmatch(r"-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}", bbox):
            raise HTTPException(400, "bbox invalide : minLon,minLat,maxLon,maxLat")
        params = [float(x) for x in bbox.split(",")]
        where = sql.SQL("WHERE {geom} && ST_MakeEnvelope(%s, %s, %s, %s, 4326)").format(geom=geom4326)

    query = sql.SQL("""
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(f), '[]'::jsonb)
        )
        FROM (
            SELECT jsonb_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON({geom}, 6)::jsonb,
                'properties', to_jsonb(t) - {gname}
            ) AS f
            FROM {schema}.{view} t
            {where}
            LIMIT {limit}
        ) s
    """).format(
        geom=geom4326, gname=sql.Literal(geom_col),
        schema=sql.Identifier(SCHEMA), view=sql.Identifier(VIEW),
        where=where, limit=sql.Literal(MAX_FEATURES),
    )
    with pool.connection() as conn:
        row = conn.execute(query, params).fetchone()
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
