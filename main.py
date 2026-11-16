import heapq, io, json, math, os, re, threading, zipfile
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



RV = os.getenv("ROUTE_VIEW", f"{S}.{V}").split(".")
I = sql.Identifier
GR, LOCK = {}, threading.Lock()


def load_graph():
    """Construit une fois le graphe piéton depuis la table (extrémités des tronçons = noeuds). Sans pgRouting."""
    with LOCK:
        if GR:
            return GR
        s, v = RV
        T_ = sql.SQL("{}.{}").format(I(s), I(v))
        with pool.connection() as c:
            cols = dict(c.execute("""SELECT a.attname, t.typname FROM pg_attribute a
                JOIN pg_class k ON k.oid=a.attrelid JOIN pg_namespace n ON n.oid=k.relnamespace
                JOIN pg_type t ON t.oid=a.atttypid
                WHERE n.nspname=%s AND k.relname=%s AND a.attnum>0 AND NOT a.attisdropped""", (s, v)).fetchall())
            g = next((a for a, t in cols.items() if t == "geometry"), None)
            idc = next((x for x in ("fid", "id", "gid") if x in cols), None)
            if not g or not idc:
                raise HTTPException(400, f"Table d'itinéraire {s}.{v} : géométrie ou identifiant introuvable")
            col = lambda x, ty: sql.SQL("{}::" + ty).format(I(x)) if x in cols else sql.SQL("NULL::" + ty)
            lnc = next((x for x in ("longueur_m", "pmr_longueur_m") if x in cols), None)
            ln = col(lnc, "float8") if lnc else sql.SQL("ST_Length(ST_Transform({}::geometry,4326)::geography)").format(I(g))
            rows = c.execute(sql.SQL(
                "SELECT {i}, ST_X(a), ST_Y(a), ST_X(b), ST_Y(b), len, acc, cls, pm FROM (SELECT {i}, "
                "ST_Transform(ST_StartPoint(ST_GeometryN({g}::geometry,1)),4326) AS a, "
                "ST_Transform(ST_EndPoint(ST_GeometryN({g}::geometry,ST_NumGeometries({g}::geometry))),4326) AS b, "
                "{ln} AS len, {acc} AS acc, {cls} AS cls, {pm} AS pm FROM {t} WHERE {g} IS NOT NULL) q").format(
                i=I(idc), g=I(g), ln=ln, acc=col("access_fauteuil", "text"), cls=col("pmr_classe_pente", "text"),
                pm=col("pmr_pente_maximale_mnt_pct", "float8"), t=T_)).fetchall()
        nid, nodes, adj, lens, pms = {}, [], [], {}, {}

        def node(lon, lat):
            key = (round(lon, 6), round(lat, 6))
            if key not in nid:
                nid[key] = len(nodes)
                nodes.append(key)
                adj.append([])
            return nid[key]

        for f, x1, y1, x2, y2, l, acc, cls, pm in rows:
            if None in (x1, y1, x2, y2):
                continue
            a, b = node(x1, y1), node(x2, y2)
            if a == b:
                continue
            # 2 = interdit en mode PMR, 1 = pénalisé, 0 = normal
            fl = 2 if (cls == "FORTE" or (acc and "NON" in acc.upper())) else 1 if cls == "MODEREE" else 0
            adj[a].append((b, l or 0, f, fl))
            adj[b].append((a, l or 0, f, fl))
            lens[f], pms[f] = l or 0, pm or 0
        GR.update(nodes=nodes, adj=adj, len=lens, pm=pms, t=T_, g=g, i=idc, comp={})
        return GR


def nearest(lon, lat, ok=None):
    G, k, best = load_graph(), math.cos(math.radians(lat)), None
    for i, (x, y) in enumerate(G["nodes"]):
        if ok and not ok(i):
            continue
        d = ((x - lon) * k) ** 2 + (y - lat) ** 2
        if best is None or d < best[0]:
            best = (d, i)
    if not best:
        raise HTTPException(404, "Réseau vide")
    return best[1]


def comps(pmr):
    """Parties connexes du réseau : (numéro par noeud, partie principale, nombre de parties)."""
    G, key = load_graph(), bool(pmr)
    if key not in G["comp"]:
        comp, sizes = [-1] * len(G["adj"]), []
        for s0 in range(len(comp)):
            if comp[s0] != -1:
                continue
            cid, st, n = len(sizes), [s0], 0
            comp[s0] = cid
            while st:
                u = st.pop()
                n += 1
                for v, _, _, fl in G["adj"][u]:
                    if comp[v] == -1 and not (pmr and fl == 2):
                        comp[v] = cid
                        st.append(v)
            sizes.append(n)
        G["comp"][key] = (comp, sizes.index(max(sizes)), len(sizes))
    return G["comp"][key]


def dijkstra(a, b, pmr):
    adj, dist, prev, pq = GR["adj"], {a: 0}, {}, [(0, a)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == b:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, ln, f, fl in adj[u]:
            if pmr and fl == 2:
                continue
            w = d + ln * (1.5 if pmr and fl == 1 else 1)
            if w < dist.get(v, 1e18):
                dist[v], prev[v] = w, (u, f)
                heapq.heappush(pq, (w, v))
    if b not in dist:
        return None
    path, n = [], b
    while n != a:
        n, f = prev[n]
        path.append(f)
    return path[::-1]


@app.get("/api/route")
def route(x1: float, y1: float, x2: float, y2: float, pmr: int = 0):
    G, note, ncomp = load_graph(), "", 0
    a, b = nearest(x1, y1), nearest(x2, y2)
    same = HTTPException(404, "Départ et arrivée rattachés au même nœud : éloignez les deux clics")
    if a == b:
        raise same
    path = dijkstra(a, b, pmr)
    if path is None:
        comp, main, ncomp = comps(pmr)
        ok = lambda i: comp[i] == main
        a, b = nearest(x1, y1, ok), nearest(x2, y2, ok)
        if a == b:
            raise same
        path, note = dijkstra(a, b, pmr), "le nœud le plus proche était isolé : rattachement au réseau principal"
    if not path:
        raise HTTPException(404, f"Aucun chemin trouvé ({ncomp} parties non reliées dans le réseau)")
    with pool.connection() as c:
        gj = c.execute(sql.SQL("SELECT ST_AsGeoJSON(ST_Collect(ST_Transform({g}::geometry,4326)),6)::jsonb FROM {t} WHERE {i}=ANY(%s)").format(
            g=I(G["g"]), t=G["t"], i=I(G["i"])), (path,)).fetchone()[0]
    m = sum(G["len"][f] for f in path)
    return {"geometry": gj, "m": round(m), "min": max(1, round(m / 83.4)),
            "pente_max": round(max(G["pm"][f] for f in path), 1), "from": list(G["nodes"][a]),
            "to": list(G["nodes"][b]), "note": note}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
