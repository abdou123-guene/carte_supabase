import io, json, os, re, zipfile
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
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
    discover()
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



SCH = [s for s in os.getenv("LAYER_SCHEMAS", "geo_standards,transport_gtfs").split(",") if s]
TH = {"Cheminements": ["troncon", "noeud_cheminement", "circulation", "traversee"],
      "Équipements": ["ascenseur", "elevateur", "escalator", "escalier", "rampe", "tapis_roulant", "passage_selectif"],
      "Accès et stationnement": ["entree", "quai", "stationnement"], "Obstacles": ["obstacle"],
      "Transport GTFS · réseau": ["stops", "routes", "shapes", "route_directions"],
      "Transport GTFS · horaires": ["stop_times", "trips", "calendar", "agency", "feed_info", "suivi_imports"]}
LAYERS = {}


def discover():
    with pool.connection() as c:
        rows = c.execute("""SELECT n.nspname, c.relname, a.attname, t.typname, c.reltuples::bigint
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
            JOIN pg_type t ON t.oid=a.atttypid
            WHERE c.relkind IN ('r','v','m','p') AND (n.nspname=ANY(%s) OR (n.nspname=%s AND c.relname=%s))
            ORDER BY 1,2,a.attnum""", (SCH, S, V)).fetchall()
    for s, t, a, ty, n in rows:
        L = LAYERS.setdefault(f"{s}.{t}", {"s": s, "t": t, "cols": [], "n": max(n, 0), "gc": None})
        L["cols"].append(a)
        if ty in ("geometry", "geography") and not L["gc"]:
            L["gc"] = a


def theme(s, t):
    if (s, t) == (S, V):
        return "OpenStreetMap"
    return next((k for k, v in TH.items() if any(t.startswith(x) for x in v)), s)


def src(L):
    """Sous-requête avec une colonne _g (géométrie WGS84), ou (None, None) si non spatiale."""
    t, cs, I = sql.SQL("{}.{}").format(sql.Identifier(L["s"]), sql.Identifier(L["t"])), L["cols"], sql.Identifier
    num = lambda c: sql.SQL("NULLIF({}::text,'')::float8").format(I(c))
    if L["gc"]:
        gx = sql.SQL("ST_Transform(ST_SetSRID({g}::geometry,COALESCE(NULLIF(ST_SRID({g}::geometry),0),4326)),4326)").format(g=I(L["gc"]))
        return sql.SQL("SELECT t.*, {} AS _g FROM {} t").format(gx, t), L["gc"]
    if L["t"] == "shapes" and {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"} <= set(cs):
        return sql.SQL("SELECT shape_id, ST_SetSRID(ST_MakeLine(ST_MakePoint({lo},{la}) ORDER BY {sq}),4326) AS _g FROM {t} "
                       "WHERE {la} IS NOT NULL AND {lo} IS NOT NULL GROUP BY shape_id").format(
            lo=num("shape_pt_lon"), la=num("shape_pt_lat"), sq=num("shape_pt_sequence"), t=t), None
    la = next((c for c in cs if c.endswith("_lat")), None)
    if la and la[:-3] + "lon" in cs:
        return sql.SQL("SELECT t.*, ST_SetSRID(ST_MakePoint({lo},{la}),4326) AS _g FROM {t} t "
                       "WHERE {la} IS NOT NULL AND {lo} IS NOT NULL").format(lo=num(la[:-3] + "lon"), la=num(la), t=t), None
    return None, None


def feats(L, limit=None):
    s, gc = src(L)
    if s is None:
        return None
    minus = sql.SQL(" - {}").format(sql.Literal(gc)) if gc else sql.SQL("")
    return sql.SQL("SELECT jsonb_build_object('type','FeatureCollection','features',COALESCE(jsonb_agg(jsonb_build_object("
                   "'type','Feature','geometry',ST_AsGeoJSON(_g,6)::jsonb,'properties',jsonb_strip_nulls((to_jsonb(x) - '_g'){m}))),'[]'::jsonb)) "
                   "FROM (SELECT * FROM ({s}) q WHERE _g IS NOT NULL {lim}) x").format(
        m=minus, s=s, lim=sql.SQL("LIMIT {}").format(sql.Literal(limit)) if limit else sql.SQL(""))


def csv_bytes(L):
    s, gc = src(L)
    t = sql.SQL("{}.{}").format(sql.Identifier(L["s"]), sql.Identifier(L["t"]))
    c = sql.SQL(",").join(sql.Identifier(x) for x in L["cols"] if x != gc)
    q = (sql.SQL("COPY (SELECT {c}, ST_AsText(_g) AS wkt FROM ({s}) q) TO STDOUT WITH CSV HEADER").format(c=c, s=s)
         if s is not None and (gc or L["t"] != "shapes")
         else sql.SQL("COPY (SELECT {c} FROM {t}) TO STDOUT WITH CSV HEADER").format(c=c, t=t))
    buf = io.BytesIO()
    with pool.connection() as cn, cn.cursor() as cur, cur.copy(q) as cp:
        for d in cp:
            buf.write(d)
    return buf.getvalue()


def getlayer(i):
    if i not in LAYERS:
        raise HTTPException(404, "Couche inconnue")
    return LAYERS[i]


@app.get("/api/layers")
def layers():
    out = [{"id": i, "name": L["t"], "group": theme(L["s"], L["t"]), "spatial": src(L)[0] is not None,
            "n": L["n"], "osm": (L["s"], L["t"]) == (S, V)} for i, L in LAYERS.items()]
    return sorted(out, key=lambda x: (x["group"], x["name"]))


@app.get("/api/layer")
def layer_geojson(l: str):
    q = feats(getlayer(l), 20000)
    if q is None:
        raise HTTPException(400, "Couche non spatiale")
    with pool.connection() as c:
        return c.execute(q).fetchone()[0]


@app.get("/api/download")
def download(l: list[str] = Query(...), fmt: str = "geojson"):
    files = {}
    for i in l:
        L, base = getlayer(i), re.sub(r"[^\w.-]+", "_", i)
        q = feats(L) if fmt == "geojson" else None
        if q is not None:
            with pool.connection() as c:
                files[base + ".geojson"] = json.dumps(c.execute(q).fetchone()[0]).encode()
        else:
            files[base + ".csv"] = csv_bytes(L)
    if len(files) == 1:
        n, d = next(iter(files.items()))
        return Response(d, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{n}"'})
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return Response(b.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="couches.zip"'})



RV = os.getenv("ROUTE_VIEW", "geo_standards.v_troncon_pgrouting_conforme").split(".")
_rc = {}
I = sql.Identifier


def route_cfg(c):
    if _rc:
        return _rc
    s, v = RV
    cols = dict(c.execute("""SELECT a.attname, t.typname FROM pg_attribute a
        JOIN pg_class k ON k.oid=a.attrelid JOIN pg_namespace n ON n.oid=k.relnamespace
        JOIN pg_type t ON t.oid=a.atttypid
        WHERE n.nspname=%s AND k.relname=%s AND a.attnum>0 AND NOT a.attisdropped""", (s, v)).fetchall())
    pick = lambda *k: next((x for x in k if x in cols), None)
    cfg = {"g": next((a for a, t in cols.items() if t == "geometry"), None), "id": pick("id", "fid", "gid", "ogc_fid"),
           "s": pick("source", "from_node", "node_from"), "t": pick("target", "to_node", "node_to"),
           "c": pick("cost", "cout", "longueur", "length"), "r": pick("reverse_cost")}
    miss = [k for k in ("g", "id", "s", "t") if not cfg[k]]
    if miss:
        raise HTTPException(400, f"Colonnes introuvables dans {s}.{v} ({', '.join(miss)}). Colonnes présentes : {', '.join(cols)}")
    fn = c.execute("SELECT n.nspname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                   "WHERE p.proname='pgr_dijkstra' LIMIT 1").fetchone()
    if not fn:
        raise HTTPException(400, "pgRouting n'est pas activé sur la base (Supabase > Database > Extensions > pgrouting)")
    cfg["fn"] = fn[0]
    cfg["srid"] = c.execute(sql.SQL("SELECT ST_SRID({g}) FROM {t} WHERE {g} IS NOT NULL LIMIT 1").format(
        g=I(cfg["g"]), t=sql.SQL("{}.{}").format(I(s), I(v)))).fetchone()[0] or 4326
    _rc.update(cfg)
    return _rc


@app.get("/api/route")
def route(x1: float, y1: float, x2: float, y2: float):
    with pool.connection() as c:
        k = route_cfg(c)
        V_ = sql.SQL("{}.{}").format(I(RV[0]), I(RV[1]))
        g = I(k["g"])

        def node(x, y):
            p = sql.SQL("ST_Transform(ST_SetSRID(ST_MakePoint({},{}),4326),{})").format(
                sql.Literal(x), sql.Literal(y), sql.Literal(k["srid"]))
            r = c.execute(sql.SQL("SELECT {s}, {t}, ST_Distance(ST_StartPoint(ST_LineMerge({g})),{p}), "
                                  "ST_Distance(ST_EndPoint(ST_LineMerge({g})),{p}) FROM {v} WHERE {g} IS NOT NULL "
                                  "ORDER BY {g} <-> {p} LIMIT 1").format(s=I(k["s"]), t=I(k["t"]), g=g, p=p, v=V_)).fetchone()
            if not r:
                raise HTTPException(404, "Aucun tronçon à proximité")
            return r[0] if (r[2] or 0) <= (r[3] or 0) else r[1]

        a, b = node(x1, y1), node(x2, y2)
        if a == b:
            raise HTTPException(404, "Départ et arrivée trop proches")
        cost = sql.SQL("{}::float8").format(I(k["c"])) if k["c"] else sql.SQL("ST_Length({})::float8").format(g)
        rev = sql.SQL(", {}::float8 AS reverse_cost").format(I(k["r"])) if k["r"] else sql.SQL("")
        inner = sql.SQL("SELECT {i}::bigint AS id, {s}::bigint AS source, {t}::bigint AS target, {c} AS cost{r} FROM {v} "
                        "WHERE {s} IS NOT NULL AND {t} IS NOT NULL").format(
            i=I(k["id"]), s=I(k["s"]), t=I(k["t"]), c=cost, r=rev, v=V_).as_string(c)
        row = c.execute(sql.SQL("WITH d AS (SELECT edge FROM {fn}.pgr_dijkstra(%s,%s::bigint,%s::bigint,directed := {dr})) "
                                "SELECT ST_AsGeoJSON(ST_Collect(ST_Transform(e.{g},4326)),6)::jsonb, "
                                "sum(ST_Length(ST_Transform(e.{g},4326)::geography)) "
                                "FROM d JOIN {v} e ON e.{i}=d.edge WHERE d.edge>0").format(
            fn=I(k["fn"]), g=g, v=V_, i=I(k["id"]), dr=sql.SQL("true" if k["r"] else "false")), (inner, a, b)).fetchone()
    if not row or row[0] is None:
        raise HTTPException(404, "Aucun chemin trouvé entre ces deux points")
    return {"geometry": row[0], "m": round(row[1] or 0)}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
