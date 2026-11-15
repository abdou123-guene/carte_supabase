import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg_pool import ConnectionPool

load_dotenv()
S = os.getenv("DB_SCHEMA", "amenagement_test_mm")
V = os.getenv("DB_VIEW", "osm_troncon_anglet_2154")  # table ou vue
CAND = ["highway", "surface", "maxspeed", "lanes", "oneway", "cycleway", "lit", "bridge", "tunnel", "access"]
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=4, open=False,
                      kwargs={"prepare_threshold": None})
T = sql.SQL("{}.{}").format(sql.Identifier(S), sql.Identifier(V))
names, G, flt = [], os.getenv("GEOM_COLUMN") or None, []


@asynccontextmanager
async def lifespan(app):
    global names, G, flt
    pool.open()
    with pool.connection() as c:
        rows = c.execute("""SELECT a.attname, t.typname FROM pg_attribute a
            JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_type t ON t.oid=a.atttypid
            WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY a.attnum""", (S, V)).fetchall()
    names = [r[0] for r in rows]
    G = G or next((r[0] for r in rows if r[1] == "geometry"), None)
    if not G:
        raise RuntimeError(f"Pas de colonne géométrie dans {S}.{V}")
    flt = [c for c in CAND if c in names]
    yield
    pool.close()


app = FastAPI(title="Tronçons OSM", lifespan=lifespan)
g = lambda: sql.Identifier(G)


def where(p):
    cl, pr = [], []
    for f in flt:
        if p.get(f):
            cl.append(sql.SQL("COALESCE({},'(vide)')=ANY(%s)").format(sql.Identifier(f)))
            pr.append(p[f].split("|"))
    if p.get("q") and "name" in names:
        cl.append(sql.SQL('"name" ILIKE %s'))
        pr.append(f"%{p['q']}%")
    return (sql.SQL(" WHERE ") + sql.SQL(" AND ").join(cl) if cl else sql.SQL("")), pr


@app.get("/api/meta")
def meta():
    out = {"fields": {}}
    with pool.connection() as c:
        for f in flt:
            rows = c.execute(sql.SQL(
                "SELECT COALESCE({f},'(vide)'), count(*), round(sum(ST_Length({g}))::numeric/1000,2) "
                "FROM {t} GROUP BY 1 ORDER BY 2 DESC LIMIT 30").format(f=sql.Identifier(f), g=g(), t=T)).fetchall()
            out["fields"][f] = [{"v": a, "n": b, "km": float(k or 0)} for a, b, k in rows]
        e = c.execute(sql.SQL("SELECT ST_XMin(x),ST_YMin(x),ST_XMax(x),ST_YMax(x) FROM "
                              "(SELECT ST_Extent(ST_Transform({g},4326)) x FROM {t}) s").format(g=g(), t=T)).fetchone()
    out["bbox"] = [float(v) for v in e] if e and e[0] is not None else None
    return out


@app.get("/api/troncons")
def troncons(r: Request):
    w, pr = where(r.query_params)
    keys = [k for k in ["name"] + flt if k in names]
    props = sql.SQL(",").join(sql.SQL("{},{}").format(sql.Literal(k), sql.Identifier(k)) for k in ["fid"] + keys)
    q = sql.SQL(
        "SELECT jsonb_build_object('type','FeatureCollection','features',COALESCE(jsonb_agg("
        "jsonb_build_object('type','Feature','geometry',ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology({g},1),4326),6)::jsonb,"
        "'properties',jsonb_build_object({p},'len',round(ST_Length({g})::numeric)))),'[]'::jsonb)) "
        "FROM (SELECT * FROM {t}{w} LIMIT 20000) t").format(g=g(), p=props, t=T, w=w)
    with pool.connection() as c:
        return c.execute(q, pr).fetchone()[0]


@app.get("/api/troncon/{fid}")
def troncon(fid: int):
    q = sql.SQL("SELECT (to_jsonb(t) - {k}) || jsonb_build_object('longueur_m', round(ST_Length({g})::numeric)) "
                "FROM {t} t WHERE fid=%s").format(k=sql.Literal(G), g=g(), t=T)
    with pool.connection() as c:
        row = c.execute(q, (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "Tronçon introuvable")
    return {k: v for k, v in row[0].items() if v not in (None, "")}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
