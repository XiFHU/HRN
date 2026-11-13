"""HRN Tool (simple) - high-risk network screening.

Run:   python app.py        Open: http://127.0.0.1:8770

Workflow
  1. Upload crash table (CSV/XLSX) + road geometry (GeoJSON/GPKG/zipped SHP),
     snap each crash to its nearest road, draw the map.
  2. Pick a crash-type column (e.g. Manner of Collision) and the values to focus on.
  3. Scan the combined crash+road table: for every column find the conditions
     (value = x, number >= x, ...) where the focus crash type is over-represented
     and has many crashes. Pick how many factors to apply.
  4. Final map: roads coloured by number of factors met; filter by factor count
     and by crash type.

Search these function names when reviewing:
  build_map            crash -> road snapping and combined table
  scan_factors         factor discovery (counts, share, lift, excess)
  score_network        per-road factor counts for the final map
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
MAX_MAP_POINTS = 40_000
FT_TO_M = 0.3048

STATE: dict = {
    "crashes": None, "crash_name": "",
    "roads": None, "road_attr": None, "road_name": "", "road_geojson": None, "road_name_col": None,
    "combined": None, "points": None, "map_stats": None, "road_len": None,
}
LOCK = threading.Lock()

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

EXCLUDE_TOKENS = {
    "id", "ids", "objectid", "fid", "guid", "uuid", "uid", "lat", "lon", "lng", "long",
    "latitude", "longitude", "geometry", "geom", "shape", "date", "time", "timestamp",
    "datetime", "year", "month", "day", "address", "narrative", "notes", "note",
    "description", "milepost", "mp", "zip", "postal", "fips", "coord", "coordinate",
    "coordinates", "easting", "northing", "route", "rte", "link", "serial", "join",
}
EXCLUDE_REGEX = re.compile(
    r"(case|crash|report|record|incident|document|accident|unit|vehicle|person|sequence|seq)\s*(no|num|number|nbr|id)$"
)


def tokens(name: str) -> list[str]:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def is_excluded_name(name: str) -> bool:
    base = name[5:] if name.startswith("road:") else name
    toks = tokens(base)
    if set(toks) & EXCLUDE_TOKENS:
        return True
    return bool(EXCLUDE_REGEX.search(" ".join(toks)))


def pretty_col(col: str) -> str:
    return "Road: " + col[5:] if col.startswith("road:") else col


def fmt_num(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.4f}".rstrip("0").rstrip(".")


def to_float(s) -> np.ndarray:
    """Numbers as a plain float array (blank / text -> NaN). Never returns pandas' NA marker,
    which newer pandas uses for nullable columns and which cannot be used in if-tests or masks."""
    out = pd.to_numeric(pd.Series(s, copy=False).astype(object), errors="coerce")
    return np.asarray(out.to_numpy(dtype=float, na_value=np.nan), dtype=float)


def clean(obj):
    """Make numpy / pandas values JSON safe."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if obj is pd.NA or obj is pd.NaT:
        return None
    return obj


def need_state(*keys):
    for k in keys:
        if STATE.get(k) is None:
            raise ValueError({
                "crashes": "Upload the crash file first.",
                "roads": "Upload the road file first.",
                "combined": "Build the map (Step 1) first.",
            }.get(k, f"Missing {k}."))


# ----------------------------------------------------------------------------
# Upload / parsing
# ----------------------------------------------------------------------------

def _cell_text(v):
    """One cell as trimmed text, or None when blank. Plain Python, so no pandas NA can leak out."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # Excel stores 25 as 25.0
    elif isinstance(v, pd.Timestamp):
        v = v.isoformat(sep=" ").replace(" 00:00:00", "")
    t = str(v).strip()
    return t or None


MERGE_NOTE = {"crash": 0, "road": 0}


def _merge_key(t: str) -> str:
    """Spelling-insensitive key: capitals, spaces, hyphens and other punctuation ignored.
    Values without letters (codes, ranges like '1-15') keep their exact text."""
    if not re.search(r"[A-Za-z]", t):
        return "\x00" + t
    return re.sub(r"[^0-9a-z]+", "", t.casefold())


def merge_spellings(values: list) -> tuple[list, int]:
    """Values that differ only in capitals or spacing ('INTERSTATE' / 'Interstate',
    'Dark  Lighted' / 'Dark Lighted') become one value: the most common spelling
    (on a tie, the one that is not all capitals). Numbers are left alone.
    Returns the new list and how many spellings were folded into another."""
    texts = [v for v in values if isinstance(v, str)]
    if not texts:
        return values, 0
    groups: dict = {}
    for t in texts:
        groups.setdefault(_merge_key(t), {}).setdefault(t, 0)
        groups[_merge_key(t)][t] += 1
    canon, folded = {}, 0
    for spellings in groups.values():
        if len(spellings) < 2:
            continue
        best = max(spellings.items(), key=lambda kv: (kv[1], not kv[0].isupper(), kv[0]))[0]
        for sp in spellings:
            if sp != best:
                canon[sp] = best
                folded += 1
    if not canon:
        return values, 0
    return [canon.get(v, v) if isinstance(v, str) else v for v in values], folded


def _tidy_table(df: pd.DataFrame) -> pd.DataFrame:
    names = []
    for i, c in enumerate(df.columns):
        t = _cell_text(c)
        names.append(None if (t is None or t.lower().startswith("unnamed:")) else t)
    df = df.copy()
    df.columns = [n if n else f"__drop{i}" for i, n in enumerate(names)]
    out = {}
    seen: dict[str, int] = {}
    for c in df.columns:
        vals = [_cell_text(v) for v in df[c].tolist()]
        if c.startswith("__drop") and all(v is None for v in vals):
            continue  # blank column with no header
        name = c if not c.startswith("__drop") else f"Column {len(out) + 1}"
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}_{seen[name]}"
        vals, nf = merge_spellings(vals)
        MERGE_NOTE["crash"] += nf
        out[name] = pd.Series(vals, dtype=object)
    t = pd.DataFrame(out)
    if t.empty or t.shape[1] == 0:
        return t
    keep = [any(v is not None for v in row) for row in t.itertuples(index=False)]
    return t.loc[keep].reset_index(drop=True)


LAST_SHEET_NOTE = {"text": ""}


def read_crash_file(data: bytes, filename: str) -> pd.DataFrame:
    MERGE_NOTE["crash"] = 0
    suffix = Path(filename).suffix.lower()
    LAST_SHEET_NOTE["text"] = ""
    if suffix in (".xlsx", ".xlsm", ".xls"):
        try:
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=object)
        except ImportError:
            raise ValueError("Reading Excel files needs the openpyxl package (pip install openpyxl), or save the sheet as CSV.")
        # use the first sheet that has data; empty sheets are ignored
        df, used, empties = None, None, []
        for name, sh in sheets.items():
            t = _tidy_table(sh)
            if len(t) and t.shape[1]:
                if df is None:
                    df, used = t, name
            else:
                empties.append(str(name))
        if df is None:
            raise ValueError("Every sheet in the workbook is empty.")
        if len(sheets) > 1:
            LAST_SHEET_NOTE["text"] = f"sheet \"{used}\" (of {len(sheets)})"
        return df
    if suffix in (".csv", ".tsv", ".txt"):
        head = data[:20000].decode("latin-1", errors="ignore")
        sep = "\t" if (suffix == ".tsv" or head.count("\t") > head.count(",")) else ","
        df, last = None, None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                df = pd.read_csv(io.BytesIO(data), dtype=object, encoding=enc, sep=sep,
                                 keep_default_na=False, na_values=[""], low_memory=False)
                break
            except UnicodeDecodeError as exc:
                last = exc
        if df is None:
            raise ValueError(f"Could not decode the file: {last}")
    else:
        raise ValueError("Crash file must be CSV, TSV, TXT or XLSX.")
    df = _tidy_table(df)
    if df.empty:
        raise ValueError("The crash file has no data rows.")
    return df


def read_road_file(data: bytes, filename: str):
    try:
        import geopandas as gpd
    except ImportError:
        raise ValueError("Road files need geopandas (pip install -r requirements.txt).")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".geojson", ".json", ".gpkg", ".zip", ".shp"):
        raise ValueError("Road file must be GeoJSON, GeoPackage, or a zipped shapefile (.zip).")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(data)
        src = f"zip://{path.as_posix()}" if suffix == ".zip" else str(path)
        gdf = gpd.read_file(src)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf[gdf.geom_type.isin(["LineString", "MultiLineString"])].copy()
    if gdf.empty:
        raise ValueError("No line geometries found. The road file must contain LineString / MultiLineString features.")
    if gdf.crs is None:
        b = gdf.total_bounds
        if abs(b).max() <= 180:
            gdf = gdf.set_crs(4326)
        else:
            raise ValueError("The road file has no coordinate system (.prj missing) and is not in lat/lon.")
    gdf = gdf.to_crs(4326).reset_index(drop=True)
    attr = pd.DataFrame(gdf.drop(columns="geometry"))
    for c in attr.columns:
        if attr[c].dtype == bool:
            attr[c] = attr[c].astype(str)
        elif attr[c].dtype == object:
            attr[c] = attr[c].map(lambda v: v if (v is None or isinstance(v, (str, int, float))) else str(v))
    attr.columns = [str(c) for c in attr.columns]
    MERGE_NOTE["road"] = 0
    for c in attr.columns:
        if attr[c].dtype == object:
            vals, nf = merge_spellings(attr[c].tolist())
            if nf:
                attr[c] = pd.Series(vals, index=attr.index, dtype=object)
                MERGE_NOTE["road"] += nf
    return gdf, attr


def _round_coords(c, nd=5):
    if isinstance(c[0], (int, float)):
        return [round(c[0], nd), round(c[1], nd)]
    return [_round_coords(x, nd) for x in c]


def build_road_geojson(gdf, name_col) -> str:
    from shapely.geometry import mapping
    feats = []
    names = gdf[name_col].tolist() if name_col else [None] * len(gdf)
    for rid, (geom, nm) in enumerate(zip(gdf.geometry, names)):
        g = geom.simplify(0.00002, preserve_topology=True)
        if g.is_empty:
            g = geom
        m = mapping(g)
        feats.append({
            "type": "Feature",
            "properties": {"id": rid, "name": None if (nm is None or (isinstance(nm, float) and math.isnan(nm))) else str(nm)},
            "geometry": {"type": m["type"], "coordinates": _round_coords(m["coordinates"])},
        })
    return json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"))


def guess_name_column(cols: list[str]) -> str | None:
    lower = {c.lower(): c for c in cols}
    for key in ("name", "fullname", "full_name", "road_name", "roadname", "street", "street_name",
                "st_name", "streetname", "route_name", "ref"):
        if key in lower:
            return lower[key]
    return None


def column_summary(df: pd.DataFrame) -> list[dict]:
    out = []
    for c in df.columns:
        s = df[c]
        out.append({"name": c, "n_unique": int(s.nunique(dropna=True)), "n_missing": int(s.isna().sum())})
    return out


def safe_latlon_guess(df: pd.DataFrame) -> dict:
    """Pre-fill for the lat/lon drop-downs only. The user always chooses the columns, so a
    failed guess must never stop the file from loading."""
    try:
        return detect_latlon(list(df.columns), df)
    except Exception:
        traceback.print_exc()
        return {"lat": None, "lon": None, "projected_warning": "", "hint": "Choose the latitude / longitude columns below."}


def detect_latlon(cols: list[str], df: pd.DataFrame) -> dict:
    numeric_cache: dict[str, bool] = {}

    def is_numeric(c):  # at least half of the filled cells are numbers
        if c not in numeric_cache:
            s = df[c].dropna()
            numeric_cache[c] = bool(len(s)) and bool(np.isfinite(to_float(s.head(5000))).mean() >= 0.5)
        return numeric_cache[c]

    def find(exact, partial):
        for p in exact:
            for c in cols:
                if re.fullmatch(p, c.strip().lower()) and is_numeric(c):
                    return c
        for p in partial:  # word-level match only, so "y" never matches e.g. "Injury Severity"
            for c in cols:
                if re.search(p, c.strip().lower()) and is_numeric(c):
                    return c
        return None
    lat = find([r"lat(itude)?", r"y", r"y[ _]?coord\w*", r"northing"],
               [r"\blat", r"latitude", r"(^|[ _])y([ _]|$)", r"\by[ _]?coord", r"northing"])
    lon = find([r"lon(g|gitude)?", r"lng", r"x", r"x[ _]?coord\w*", r"easting"],
               [r"\blon", r"\blng", r"longitude", r"(^|[ _])x([ _]|$)", r"\bx[ _]?coord", r"easting"])
    note, hint = "", ""
    if not (lat and lon):
        hint = "Could not find numeric latitude / longitude columns. Pick them below."
    else:
        la, lo = to_float(df[lat]), to_float(df[lon])
        if not (np.isfinite(la).any() and np.isfinite(lo).any()):
            hint = (f"The guessed columns ({lat} / {lon}) have no numbers in them. "
                    "Pick the correct latitude / longitude columns below.")
        elif np.nanmax(np.abs(la)) > 90 or np.nanmax(np.abs(lo)) > 180:
            note = ("Coordinates look projected (not lat/lon). Enter the EPSG code of the crash coordinate system "
                    "(e.g. 2232 = Colorado North, ftUS).")
    return {"lat": lat, "lon": lon, "projected_warning": note, "hint": hint}


# ----------------------------------------------------------------------------
# Spatial matching (the only part that needs geopandas)
# ----------------------------------------------------------------------------

def snap_points_to_roads(lon, lat, crash_idx, crs_code, roads_ll, tol_ft):
    """Return (matched crash_idx array, road_id array, distance_ft array, all points lon/lat in WGS84)."""
    import geopandas as gpd
    pts = gpd.GeoDataFrame({"crash_idx": crash_idx}, geometry=gpd.points_from_xy(lon, lat), crs=f"EPSG:{crs_code}")
    pts_ll = pts.to_crs(4326)
    good = np.isfinite(pts_ll.geometry.x) & np.isfinite(pts_ll.geometry.y)
    pts_ll = pts_ll[good].reset_index(drop=True)
    utm = roads_ll.estimate_utm_crs()
    roads_m = roads_ll[["geometry"]].copy()
    roads_m["road_id"] = np.arange(len(roads_ll))
    roads_m = roads_m.to_crs(utm)
    pts_m = pts_ll.to_crs(utm)
    joined = gpd.sjoin_nearest(pts_m, roads_m, how="inner", max_distance=tol_ft * FT_TO_M, distance_col="dist_m")
    joined = joined[~joined.index.duplicated(keep="first")]
    all_lonlat = np.column_stack([pts_ll.geometry.x.values, pts_ll.geometry.y.values])
    return (pts_ll.loc[joined.index, "crash_idx"].to_numpy(), joined["road_id"].to_numpy(),
            (joined["dist_m"] / FT_TO_M).to_numpy(), pts_ll["crash_idx"].to_numpy(), all_lonlat,
            float(roads_m.length.sum() / 1609.344))


def build_map(params: dict) -> dict:
    need_state("crashes", "roads")
    crashes: pd.DataFrame = STATE["crashes"]
    lat_col, lon_col = params.get("lat_col"), params.get("lon_col")
    if lat_col not in crashes.columns or lon_col not in crashes.columns:
        raise ValueError("Choose the latitude and longitude (or Y and X) columns.")
    crs_code = str(params.get("crash_crs") or "4326").strip().replace("EPSG:", "")
    try:
        tol_ft = float(params.get("tolerance_ft") or 100)
    except ValueError:
        tol_ft = 100.0
    tol_ft = max(5.0, min(5280.0, tol_ft))

    lat = to_float(crashes[lat_col])
    lon = to_float(crashes[lon_col])
    valid = np.isfinite(lat) & np.isfinite(lon) & ~((lat == 0) & (lon == 0))
    if valid.sum() == 0:
        raise ValueError("No usable coordinates in the chosen columns.")
    idx = crashes.index.to_numpy()[valid]
    m_idx, m_road, m_dist, p_idx, p_lonlat, miles = snap_points_to_roads(
        lon[valid], lat[valid], idx, crs_code, STATE["roads"], tol_ft)

    comb = crashes.loc[m_idx].reset_index(drop=True)
    road_part = STATE["road_attr"].loc[m_road].add_prefix("road:").reset_index(drop=True)
    comb = pd.concat([comb, road_part], axis=1)
    comb["__road_id"] = m_road
    comb["__crash_idx"] = m_idx
    comb["__dist_ft"] = m_dist
    STATE["combined"] = comb

    matched_set = set(m_idx.tolist())
    flags = np.fromiter((i in matched_set for i in p_idx), dtype=bool, count=len(p_idx))
    order = np.arange(len(p_idx))
    if len(order) > MAX_MAP_POINTS:
        order = np.random.default_rng(1).choice(order, MAX_MAP_POINTS, replace=False)
    STATE["points"] = {
        "points": [[round(float(p_lonlat[i][1]), 6), round(float(p_lonlat[i][0]), 6), int(flags[i])] for i in order],
        "truncated": len(p_idx) > MAX_MAP_POINTS,
    }
    b = STATE["roads"].total_bounds
    stats = {
        "crashes_total": int(len(crashes)),
        "with_coordinates": int(valid.sum()),
        "matched": int(len(comb)),
        "unmatched": int(valid.sum() - len(comb)),
        "no_coordinates": int((~valid).sum()),
        "median_distance_ft": float(np.median(m_dist)) if len(m_dist) else None,
        "tolerance_ft": tol_ft,
        "roads": int(len(STATE["roads"])),
        "network_miles": miles,
        "bounds": [[float(b[1]), float(b[0])], [float(b[3]), float(b[2])]],
    }
    STATE["map_stats"] = stats
    return stats


# ----------------------------------------------------------------------------
# Column classification and conditions
# ----------------------------------------------------------------------------

def range_bounds(label: str):
    """(lo, hi_exclusive) for a group label: '25-34' -> (25, 35), '75+' -> (75, inf),
    '0 (unknown)' / '0' -> (0, 0 exactly), '1.5 to <3' -> (1.5, 3)."""
    t = label.strip()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?) to <(-?\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.fullmatch(r"(-?\d+)-(-?\d+)", t)
    if m:
        return float(m.group(1)), float(m.group(2)) + 1
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\+", t)
    if m:
        return float(m.group(1)), math.inf
    m = re.fullmatch(r"<(-?\d+(?:\.\d+)?)", t)
    if m:
        return -math.inf, float(m.group(1))
    m = re.fullmatch(r"\u2264(-?\d+)", t)
    if m:
        return -math.inf, float(m.group(1)) + 1
    m = re.match(r"(-?\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1)), None  # single value
    raise ValueError(f"Cannot read the group '{label}'.")


def condition_mask(s: pd.Series, cond: dict) -> np.ndarray:
    op, val = cond["op"], cond["value"]
    if op == "age":
        names = [g[0] for g in AGE_GROUPS]
        if val not in names:
            raise ValueError(f"Unknown age group '{val}'.")
        return age_codes(s) == names.index(val)
    if op == "rng":
        x = to_float(s)
        lo, hi = range_bounds(str(val))
        with np.errstate(invalid="ignore"):
            m = (x == lo) if hi is None else ((x >= lo) & (x < hi))
        return np.asarray(m, dtype=bool)
    if cond["kind"] == "num":
        x = to_float(s)
        v = float(val)
        with np.errstate(invalid="ignore"):
            m = (x == v) if op == "eq" else (x >= v) if op == "ge" else (x <= v)
        return np.asarray(m, dtype=bool)
    txt = s.astype("string").str.strip()
    return (txt == val).fillna(False).to_numpy(dtype=bool)


def cond_label(cond: dict) -> str:
    sym = {"eq": "=", "rng": "=", "age": "=", "ge": "\u2265", "le": "\u2264"}[cond["op"]]
    return f"{pretty_col(cond['column'])} {sym} {cond['value']}"


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam), i.e. the regularized upper incomplete gamma Q(k+1, lam).

    This is the "Prob." column: how likely it is to see this many (or fewer) crashes of the
    group if the condition made no difference. Close to 100% = clearly over-represented."""
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0
    return gamma_q(k + 1.0, float(lam))


def chi2_sf(x: float, df: int) -> float:
    """P(chi-square with df degrees of freedom >= x)."""
    if df <= 0:
        return 1.0
    if x <= 0:
        return 1.0
    return gamma_q(df / 2.0, x / 2.0)


def gamma_q(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x)."""
    if x <= 0:
        return 1.0
    log_front = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:  # series for the lower function P(a, x); Q = 1 - P
        ap, term, total = a, 1.0 / a, 1.0 / a
        for _ in range(1_000_000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-13:
                break
        return min(1.0, max(0.0, 1.0 - total * math.exp(log_front)))
    tiny = 1e-300  # continued fraction for Q(a, x)
    b = x + 1 - a
    c, d = 1 / tiny, 1 / b
    h = d
    for i in range(1, 1_000_000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-13:
            break
    return min(1.0, max(0.0, math.exp(log_front) * h))


def stat_block(n_all: int, n_foc: int, N: int, F: int) -> dict:
    base = F / N if N else 0
    exp = n_all * base
    var = exp * (1 - base)
    return {
        "n_focus": int(n_foc), "n_all": int(n_all),
        "pct_all": n_all / N if N else 0,
        "share": n_foc / F if F else 0,
        "rate": n_foc / n_all if n_all else 0,
        "lift": (n_foc / n_all) / base if (n_all and base) else None,
        "excess": n_foc - exp,
        "z": (n_foc - exp) / math.sqrt(var) if var > 0 else 0,
        "prob": poisson_cdf(int(n_foc), exp),
    }


def group_block(n_all: int, n_g: int, N: int, G: int) -> dict:
    """Count / share / Prob. of a second (display-only) group such as KSI."""
    exp = n_all * (G / N) if N else 0
    return {"n": int(n_g), "share": n_g / G if G else 0, "prob": poisson_cdf(int(n_g), exp) if G else None,
            "lift": (n_g / exp) if exp > 0 else None}


KABCO = "KABCO"


def breakdown_codes(df: pd.DataFrame, column: str | None):
    """Integer code per row for the 'Crash frequency' columns (-1 = blank), plus ordered labels.
    KABCO-style values are put in K, A, B, C, O order; anything else by count."""
    if not column:
        return None, [], []
    if column not in df.columns:
        raise ValueError(f"Column not found: {column}")
    txt = df[column].astype("string").str.strip()
    counts = txt.value_counts()
    if len(counts) > 12:
        raise ValueError(f"'{column}' has {len(counts)} values; pick a column with 12 or fewer for the crash frequency columns.")
    labels = list(counts.index.astype(str))
    ranks = [severity_rank(lb) for lb in labels]
    if labels and all(r is not None for r in ranks):
        order = sorted(range(len(labels)), key=lambda i: ranks[i][0])
        labels = [labels[i] for i in order]
        short = [ranks[i][1] for i in order]
    else:
        short = labels
    if len(set(short)) != len(short):  # e.g. "PDO" and "No Injury (PDO)" both shorten to O
        short = labels
    pos = {lb: i for i, lb in enumerate(labels)}
    codes = np.array([pos.get(v, -1) if v is not None else -1 for v in df[column].map(_cell_text).tolist()], dtype=int)
    return codes, labels, short


def severity_rank(label: str):
    """(sort key, short header) for KABCO-style severity labels, or None if not recognised.
    Handles 'K', 'Fatal (K)', 'Evident, Incapacitating (A)', 'No Injury (PDO)', 'FAT', 'INJ', 'PDO'."""
    t = label.strip().upper()
    m = re.search(r"\(([KABCO])\)", t)
    if m:
        return KABCO.index(m.group(1)), m.group(1)
    if re.fullmatch(r"[KABCO]", t):
        return KABCO.index(t), t
    if "PDO" in t or "PROPERTY DAMAGE" in t:
        return 4.0 + (0.5 if t == "PDO" else 0), "PDO" if t == "PDO" else "O"
    if t in ("FAT", "FATAL", "FATALITY", "KILLED"):
        return 0.5, label.strip()
    if t in ("INJ", "INJURY"):
        return 3.5, label.strip()
    return None


def natural_key(text: str):
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", text)
    return (0, float(m.group(1)), text.lower()) if m else (1, 0.0, text.lower())


def numeric_thresholds(finite: np.ndarray) -> np.ndarray:
    uniq = np.unique(finite)
    if len(uniq) <= 60:
        return uniq
    q = np.quantile(finite, np.arange(0.05, 0.9501, 0.05))
    if np.allclose(finite, np.round(finite)):
        q = np.round(q)
    else:
        q = np.round(q, 2)
    return np.unique(q)


# Standard age groups. Always listed in full, in this order, whatever the input data looks like.
AGE_GROUPS = [("0-15", 0, 16), ("16-20", 16, 21), ("21-24", 21, 25), ("25-34", 25, 35), ("35-44", 35, 45),
              ("45-54", 45, 55), ("55-64", 55, 65), ("65-74", 65, 75), ("75+", 75, math.inf)]
AGE_MAX = 120  # ages above this (e.g. 999 'unknown' codes) are treated as blank


def is_age_column(col: str) -> bool:
    return bool(set(tokens(col[5:] if col.startswith("road:") else col)) & {"age", "ages"})


def _age_one(v) -> float:
    """One age cell -> years. Numbers as is; text ranges by their middle so they land in the
    right standard group: '25-34' / '25 to 34' -> 29.5, '75+' / '75 and over' -> 75,
    'Under 16' / '<16' -> 15.5. Anything else -> NaN."""
    if v is None:
        return math.nan
    if isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool):
        f = float(v)
    else:
        t = str(v).strip().lower()
        if not t:
            return math.nan
        try:
            f = float(t)
        except ValueError:
            nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", t)]
            if not nums:
                return math.nan
            if len(nums) >= 2:
                f = (nums[0] + nums[1]) / 2
            elif re.search(r"under|below|less|younger|<", t):
                f = nums[0] - 0.5
            else:  # '75+', '75 and over', 'over 75', '75 or older'
                f = nums[0]
    if not math.isfinite(f) or f < 0 or f > AGE_MAX:
        return math.nan
    return f


def age_values(s) -> np.ndarray:
    return np.array([_age_one(v) for v in pd.Series(s, copy=False).astype(object).tolist()], dtype=float)


def age_codes(s) -> np.ndarray:
    """Index into AGE_GROUPS per row (-1 = blank / unreadable)."""
    a = age_values(s)
    codes = np.full(len(a), -1, dtype=int)
    for gi, (_, lo, hi) in enumerate(AGE_GROUPS):
        with np.errstate(invalid="ignore"):
            codes[(a >= lo) & (a < hi)] = gi
    return codes
SPEED_STEP = 10                                  # 1-9, 10-19, ..., 80+
MAX_EXACT_VALUES = 25                            # up to this many distinct values: one row per value


def _dominant_zero(v: np.ndarray) -> bool:
    """True when 0 is clearly a 'not recorded' code (far more common than any real value)."""
    zeros = int((v == 0).sum())
    if zeros < max(20, 0.05 * len(v)):
        return False
    rest = v[v != 0]
    if not len(rest):
        return True
    return zeros > 3 * int(np.unique(rest, return_counts=True)[1].max())


def _int_groups(edges: list[float], top: float, zero_label: str | None) -> list:
    """(label, lo, hi_exclusive) for integer edges; the last group is open-ended ('75+')."""
    out = []
    if zero_label:
        out.append((zero_label, 0.0, None))
    for i, lo in enumerate(edges):
        if zero_label and lo == 0:
            lo = 1
        if i + 1 < len(edges):
            hi = edges[i + 1]
            if hi - 1 < lo:
                continue
            out.append((f"{fmt_num(lo)}-{fmt_num(hi - 1)}" if hi - 1 > lo else fmt_num(lo), float(lo), float(hi)))
        else:
            out.append((f"{fmt_num(lo)}+", float(lo), math.inf))
    return out


def _nice(x: float) -> float:
    if x <= 0:
        return 0.0
    p = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * p:
            return m * p
    return 10 * p


def numeric_groups(col: str, v: np.ndarray, nun: int):
    """Groups for a numeric column, or None to list every value.
    Ages use the standard groups (0-15, 16-20, 21-24, 25-34 ... 75+), speeds 10 mph bands,
    other wide-ranging numbers about 8 equal-width bands; 0 gets its own row when it is clearly a
    'not recorded' code (e.g. driver age 0)."""
    toks = set(tokens(col[5:] if col.startswith("road:") else col))
    integer = bool(np.allclose(v, np.round(v)))
    zero = _dominant_zero(v)
    if nun <= MAX_EXACT_VALUES:
        return None
    if toks & {"speed", "mph", "spd"}:
        top = 80
        edges = [0] + list(range(SPEED_STEP, top + 1, SPEED_STEP))
        return _int_groups(edges, v.max(), "0" if zero else None)
    w = v[v != 0] if zero else v
    lo, hi = float(np.quantile(w, 0.01)), float(np.quantile(w, 0.99))
    step = _nice(max(hi - lo, 1e-9) / 8)
    if integer:
        step = max(1.0, round(step))
    start = math.floor(lo / step) * step
    edges = [start + i * step for i in range(int(math.ceil((hi - start) / step)) + 1)]
    edges = edges[:12] or [start]
    if integer:
        out = _int_groups(edges, v.max(), "0" if zero else None)
        i0 = 1 if zero else 0
        if len(out) > i0 + 1 and out[i0][1] > float(w.min()):  # values below the first band go in it
            lb, lo_, hi_ = out[i0]
            out[i0] = (f"\u2264{fmt_num(hi_ - 1)}", -math.inf, hi_)
        return out
    out = [("0", 0.0, None)] if zero else []
    for i, e in enumerate(edges):
        if i + 1 < len(edges):
            out.append((f"{fmt_num(e)} to <{fmt_num(edges[i + 1])}", e, edges[i + 1]))
        else:
            out.append((f"{fmt_num(e)}+", e, math.inf))
    if out and out[0 if not zero else min(1, len(out) - 1)][1] > float(v.min()):
        first = out[1 if zero else 0]  # values below the first edge go in the first group
        out[1 if zero else 0] = (f"<{fmt_num(first[2])}" if first[2] != math.inf else first[0], -math.inf, first[2])
    return out


def scan_column(col: str, s: pd.Series, fm: np.ndarray, N: int, F: int, min_focus: int,
                max_levels: int, ctx: dict):
    """Return (list of condition dicts sorted by excess, kind) or (None, reason).

    Every value of a categorical (or low-cardinality numeric) column is returned so the factor
    table can show the full breakdown; `over` marks the over-represented ones (enough focus
    crashes and more than the overall rate predicts). For numeric columns the best >= and <=
    thresholds are added when they are over-represented.
    ctx: bcode / nb (crash-frequency breakdown codes), g2 / F2 (second display group)."""
    nonnull = s.dropna()
    # flag-style columns (e.g. User Type = Bicycle / Motorcycle / blank) are mostly blank but useful,
    # so only skip a column when it has almost no values at all
    if len(nonnull) < max(min_focus, 0.01 * len(s)):
        return None, "almost all blank"
    nun = int(nonnull.nunique())
    if nun < 1:
        return None, "no values"
    if nun == 1 and len(nonnull) == len(s):
        return None, "only one value"
    if pd.api.types.is_bool_dtype(s):
        s = s.astype(str)
        nonnull = s.dropna()
    is_num = bool(np.isfinite(to_float(nonnull)).mean() >= 0.8) and not pd.api.types.is_bool_dtype(s)
    is_age = is_age_column(col)
    bcode, nb, g2, F2 = ctx["bcode"], ctx["nb"], ctx["g2"], ctx["F2"]

    def finish(cond: dict, n_all: int, n_foc: int, by=None, n_g2=None) -> dict:
        st = stat_block(int(n_all), int(n_foc), N, F)
        cond.update(st)
        cond["over"] = bool(n_foc >= min_focus and st["excess"] > 0)
        if nb:
            cond["by"] = [int(v) for v in by]
        if g2 is not None:
            cond["g2"] = group_block(int(n_all), int(n_g2), N, F2)
        return cond

    def from_mask(cond: dict, m: np.ndarray) -> dict:
        by = None
        if nb:
            b = bcode[m]
            by = np.bincount(b[b >= 0], minlength=nb)
        return finish(cond, int(m.sum()), int((m & fm).sum()), by, int((m & g2).sum()) if g2 is not None else None)

    def from_levels(codes: np.ndarray, labels: list[str], kind: str, op: str = "eq") -> list[dict]:
        """One condition per level; counts done with bincount for speed."""
        nU = len(labels)
        ok = codes >= 0
        c_all = np.bincount(codes[ok], minlength=nU)
        c_foc = np.bincount(codes[ok & fm], minlength=nU)
        c_g2 = np.bincount(codes[ok & g2], minlength=nU) if g2 is not None else None
        by = None
        if nb:
            okb = ok & (bcode >= 0)
            by = np.bincount(codes[okb] * nb + bcode[okb], minlength=nU * nb).reshape(nU, nb)
        out = []
        for i, lb in enumerate(labels):
            if op == "rng" and c_all[i] == 0:  # (age groups are always all listed)
                continue  # empty group
            out.append(finish({"column": col, "op": op, "value": lb, "kind": kind},
                              c_all[i], c_foc[i], by[i] if by is not None else None,
                              c_g2[i] if c_g2 is not None else None))
        return out

    conds: list[dict] = []
    if is_age:
        codes = age_codes(s)
        if (codes >= 0).sum() == 0:
            return None, "no readable ages"
        conds = from_levels(codes, [g[0] for g in AGE_GROUPS], "num", op="age")
        kind = "num"
    elif is_num:
        x = to_float(s)
        fin = np.isfinite(x)
        if fin.sum() == 0:
            return None, "no numeric values"
        if nun > 50 and np.allclose(x[fin], np.round(x[fin])) and nun > 0.9 * fin.sum():
            return None, "looks like an identifier"
        groups = numeric_groups(col, x[fin], nun)
        if groups is None:  # few distinct values (speed limit, vehicles, ...): one row per value
            vals = np.unique(x[fin])
            codes = np.full(len(x), -1, dtype=int)
            codes[fin] = np.searchsorted(vals, x[fin])
            conds += from_levels(codes, [fmt_num(float(v)) for v in vals], "num")
        else:
            codes = np.full(len(x), -1, dtype=int)
            for gi, (lb, lo, hi) in enumerate(groups):
                with np.errstate(invalid="ignore"):
                    m = (x == lo) if hi is None else ((x >= lo) & (x < hi))
                codes[m & (codes < 0)] = gi
            conds += from_levels(codes, [g[0] for g in groups], "num", op="rng")
        kind = "num"
    else:
        if nun > max_levels:
            return None, f"too many categories ({nun})"
        txt = s.astype("string").str.strip()
        codes, uniq = pd.factorize(txt, use_na_sentinel=True)
        conds = from_levels(np.asarray(codes, dtype=int), [str(v) for v in uniq], "cat")
        kind = "cat"
    # display order for the "all values" view: levels in natural order, thresholds after
    levels = [c for c in conds if c["op"] in ("eq", "rng", "age")]
    if all(c["op"] in ("rng", "age") for c in levels) and levels:
        pass  # groups are already in order
    else:
        levels.sort(key=lambda c: natural_key(c["value"]))
    for i, c in enumerate(levels):
        c["ord"] = i
    for c in conds:
        if c["op"] not in ("eq", "rng", "age"):
            c["ord"] = len(levels) + (0 if c["op"] == "ge" else 1)
    conds.sort(key=lambda c: (not c["over"], -c["excess"]))
    return conds, kind


def apply_value_filter(df: pd.DataFrame, flt: dict | None) -> np.ndarray:
    if not flt or not flt.get("column") or not flt.get("values"):
        return np.ones(len(df), dtype=bool)
    col = flt["column"]
    if col not in df.columns:
        raise ValueError(f"Column not found: {col}")
    vals = {str(v) for v in flt["values"]}
    return df[col].astype("string").str.strip().isin(vals).fillna(False).to_numpy(dtype=bool)


def crash_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if not c.startswith("__") and not c.startswith("road:")]


def focus_columns() -> dict:
    need_state("combined")
    comb = STATE["combined"]
    out = []
    for c in crash_columns(comb):
        s = comb[c]
        nun = int(s.nunique(dropna=True))
        if 2 <= nun <= 150 and float(s.notna().mean()) > 0.2:
            hint = bool(re.search(r"collision|manner|crash.?type|first.?harm|event|type", c, re.I))
            out.append({"name": c, "n_unique": nun, "suggested": hint})
    out.sort(key=lambda d: (not d["suggested"], d["n_unique"]))
    return {"columns": out}


def column_values(column: str, flt: dict | None = None) -> dict:
    need_state("combined")
    comb = STATE["combined"]
    if column not in comb.columns:
        raise ValueError("Column not found.")
    keep = apply_value_filter(comb, flt)
    s = comb.loc[keep, column].astype("string").str.strip()
    counts = s.value_counts()
    return {"values": [{"value": str(k), "count": int(v)} for k, v in counts.items()],
            "blank": int(s.isna().sum()), "total": int(keep.sum())}


# ----------------------------------------------------------------------------
# Step 3: factor discovery
# ----------------------------------------------------------------------------

def scan_factors(payload: dict) -> dict:
    need_state("combined")
    comb: pd.DataFrame = STATE["combined"]
    focus = payload.get("focus") or {}
    fcol, fvals = focus.get("column"), focus.get("values") or []
    if fcol not in comb.columns or not fvals:
        raise ValueError("Choose a crash-type column and at least one value (Step 2).")
    extra = payload.get("extra")
    group2 = payload.get("group2")
    min_focus = max(1, int(payload.get("min_focus") or 5))
    max_levels = max(2, int(payload.get("max_levels") or 40))

    universe = apply_value_filter(comb, extra)
    u = comb.loc[universe].reset_index(drop=True)
    fm = apply_value_filter(u, focus)
    N, F = len(u), int(fm.sum())
    if F == 0:
        raise ValueError("No focus crashes after filters.")
    skip_cols = {fcol}
    if extra and extra.get("column") and extra.get("values"):
        skip_cols.add(extra["column"])

    g2, F2 = None, 0
    if group2 and group2.get("column") and group2.get("values"):
        if group2["column"] not in u.columns:
            raise ValueError(f"Column not found: {group2['column']}")
        g2 = apply_value_filter(u, group2) & fm  # e.g. KSI among the crashes being analysed
        F2 = int(g2.sum())
        skip_cols.add(group2["column"])
    by_col = payload.get("breakdown") or None
    bcode, by_labels, by_short = breakdown_codes(u, by_col)
    ctx = {"bcode": bcode, "nb": len(by_labels), "g2": g2, "F2": F2}

    lengths = STATE.get("road_len")
    attr = STATE.get("road_attr")
    if attr is None:
        lengths = None
    total_len = float(np.nansum(lengths)) if lengths is not None else None
    txt_cache: dict = {}
    road_ids_u = u["__road_id"].to_numpy(dtype=int) if "__road_id" in u.columns else None
    if road_ids_u is None:
        lengths = None

    results, skipped = [], []
    for col in u.columns:
        if col.startswith("__") or col in skip_cols:
            continue
        if is_excluded_name(col):
            skipped.append({"column": pretty_col(col), "reason": "identifier / date / location field"})
            continue
        conds, info = scan_column(col, u[col], fm, N, F, min_focus, max_levels, ctx)
        if conds and not col.startswith("road:") and lengths is not None:
            txt_cache[col] = u[col].astype("string").str.strip().fillna("\u0000")
        if conds is None:
            skipped.append({"column": pretty_col(col), "reason": info})
            continue
        if not conds:
            skipped.append({"column": pretty_col(col), "reason": "no over-represented value with enough crashes"})
            continue
        for c in conds:
            c["label"] = cond_label(c)
            c["miles"], c["cpm"], c["n_segs"] = None, None, None
            if lengths is None:
                continue
            try:
                if col.startswith("road:") and col[5:] in attr.columns:
                    # road attribute: miles of road that have it
                    seg_m = condition_mask(attr[col[5:]], c)
                else:
                    # crash attribute: miles of the segments where these focus crashes happened
                    m = (txt_cache[col] == c["value"]).to_numpy(dtype=bool) if c["op"] == "eq" and c["kind"] == "cat" \
                        else condition_mask(u[col], c)
                    seg_m = np.zeros(len(lengths), dtype=bool)
                    seg_m[road_ids_u[m & fm]] = True
                mi = float(np.nansum(lengths[seg_m]))
                c["miles"], c["n_segs"] = mi, int(seg_m.sum())
                c["cpm"] = c["n_focus"] / mi if mi > 0 else None
            except (ValueError, KeyError, IndexError):
                pass
        over = [c for c in conds if c["over"]]
        # ordered (numbers, ranges, yes/no, severity) -> Spearman; unordered text -> Cramer's V
        if any(c["op"] in ("rng", "age", "ge", "le") for c in conds):
            ordered, how = True, "number ranges"
        else:
            cc, how = _column_codes(col, u[col].dropna().head(20000).tolist(), None)
            ordered = cc is not None
        results.append({
            "column": col, "label": pretty_col(col), "source": "road" if col.startswith("road:") else "crash",
            "ordered": ordered, "order_how": how,
            "kind": info, "best_excess": over[0]["excess"] if over else 0, "has_over": bool(over),
            "conditions": conds,
        })
    results.sort(key=lambda r: (not r["has_over"], -r["best_excess"]))
    all_row = {"n_all": N, "n_focus": F, "miles": total_len, "cpm": (F / total_len) if total_len else None}
    if by_labels:
        all_row["by"] = np.bincount(bcode[bcode >= 0], minlength=len(by_labels)).tolist()
    if g2 is not None:
        all_row["n_g2"] = F2
    return clean({
        "n_universe": N, "n_focus": F, "base_rate": F / N,
        "columns": results, "skipped": skipped,
        "min_focus": min_focus,
        "breakdown": {"column": by_col, "labels": by_labels, "short": by_short} if by_labels else None,
        "has_group2": g2 is not None, "all": all_row,
    })


# ----------------------------------------------------------------------------
# Step 3: Excel export of the full factor table
# ----------------------------------------------------------------------------

def _sheet_title(name: str, used: set) -> str:
    t = re.sub(r"[\\/*?:\[\]]", "-", str(name)).strip() or "Sheet"
    t = t[:31]
    base, i = t, 2
    while t.lower() in used:
        suffix = f" ({i})"
        t = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(t.lower())
    return t


def export_scan_xlsx(payload: dict) -> bytes:
    """Workbook with one sheet per crash type: every condition of every scanned column, laid out
    like the factor table (Category | Factor | Crash Frequency | Total | group | second group),
    plus Expected / x normal / Extra / Over-represented, a Skipped sheet and an Info sheet."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise ValueError("Excel export needs the openpyxl package (pip install openpyxl).")
    sets = payload.get("sets") or []
    if not sets:
        raise ValueError("Nothing to export. Scan the columns first.")
    g2_name = payload.get("group2_name") or "Group 2"

    head_fill = PatternFill("solid", fgColor="44546A")
    sub_fill = PatternFill("solid", fgColor="56677E")
    all_fill = PatternFill("solid", fgColor="D9DEE5")
    hi_fill = PatternFill("solid", fgColor="DFF3E6")
    mid_fill = PatternFill("solid", fgColor="FFF6DC")
    white_bold = Font(bold=True, color="FFFFFF")
    grey = Font(color="8A97A6")
    thin = Side(style="thin", color="C5CCD4")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    wb = Workbook()
    wb.remove(wb.active)
    used: set = set()
    info_rows = []
    skipped_rows = []
    for st in sets:
        name = st.get("name") or "Crashes"
        scan = scan_factors(st["scan"])
        B = scan["breakdown"]["short"] if scan.get("breakdown") else []
        G2 = scan.get("has_group2")
        ws = wb.create_sheet(_sheet_title(name, used))
        info_rows.append((ws.title, name, ", ".join(st["scan"]["focus"]["values"]), scan["n_focus"],
                          scan["n_universe"], scan["base_rate"]))
        for sk in scan["skipped"]:
            skipped_rows.append((ws.title, sk["column"], sk["reason"]))

        # header: two rows like the factor table
        groups = [("Category", 1), ("Factor", 1), ("Source", 1)]
        if B:
            groups.append(("Crash Frequency" + (f" ({scan['breakdown']['column']})" if scan.get("breakdown") else ""), len(B)))
        groups += [("Total", 2), (name, 7)]
        if G2:
            groups.append((g2_name, 4))
        subs = ["", "", ""] + list(B) + ["#", "%", "#", "%", "Prob.", "Expected", "x normal", "Extra", "Crashes / mi"]
        subs += (["#", "%", "x normal", "Prob."] if G2 else []) + ["Over-represented"]
        groups.append(("", 1))
        c = 1
        for title, span in groups:
            ws.cell(1, c, title)
            if span > 1:
                ws.merge_cells(start_row=1, start_column=c, end_row=1, end_column=c + span - 1)
            c += span
        for i, t in enumerate(subs, start=1):
            ws.cell(2, i, t)
        for col in (1, 2, 3, len(subs)):
            ws.merge_cells(start_row=1, start_column=col, end_row=2, end_column=col)
        ws.cell(1, len(subs), "Over-represented")
        for r in (1, 2):
            for i in range(1, len(subs) + 1):
                cell = ws.cell(r, i)
                cell.fill = head_fill if r == 1 else sub_fill
                cell.font = white_bold
                cell.alignment = center
                cell.border = box

        # "All" row
        A = scan["all"]
        row = ["All", "", ""] + list(A.get("by") or []) + [A["n_all"], 1, A["n_focus"], 1, None, None, None, None,
                                                            None if A.get("cpm") is None else round(A["cpm"], 2)]
        row += ([A.get("n_g2"), 1, None, None] if G2 else []) + [None]
        ws.append(row)
        for i in range(1, len(subs) + 1):
            ws.cell(3, i).fill = all_fill
            ws.cell(3, i).font = Font(bold=True)
            ws.cell(3, i).border = box

        nB = len(B)
        pct_cols = [3 + nB + 2, 3 + nB + 4, 3 + nB + 5]
        if G2:
            pct_cols += [3 + nB + 11, 3 + nB + 13]
        for pc in pct_cols:
            ws.cell(3, pc).number_format = "0%"
        for nc in list(range(4, 4 + nB)) + [3 + nB + 1, 3 + nB + 3] + ([3 + nB + 10] if G2 else []):
            ws.cell(3, nc).number_format = "#,##0"
        cols_out = scan["columns"]
        if payload.get("sort") == "name":
            cols_out = sorted(cols_out, key=lambda c: [(0, float(t)) if t.isdigit() else (1, t)
                                                      for t in re.split(r"(\d+)", c["label"].lower()) if t])
        for col in cols_out:
            rows = sorted(col["conditions"], key=lambda q: q["ord"])
            first = ws.max_row + 1
            for q in rows:
                exp = q["n_focus"] - q["excess"]
                vals = [col["label"], _factor_text(q), col["source"]]
                vals += list(q.get("by") or ([None] * nB))
                vals += [q["n_all"], q["pct_all"], q["n_focus"], q["share"], q["prob"], round(exp, 1),
                         round(q["lift"], 2) if q["lift"] is not None else None, round(q["excess"], 1),
                         round(q["cpm"], 2) if q.get("cpm") is not None else None]
                if G2:
                    g = q.get("g2") or {}
                    vals += [g.get("n"), g.get("share"), round(g["lift"], 2) if g.get("lift") is not None else None, g.get("prob")]
                vals += ["Yes" if q["over"] else ""]
                ws.append(vals)
                r = ws.max_row
                for i in range(1, len(vals) + 1):
                    cell = ws.cell(r, i)
                    cell.border = box
                    if not q["over"] and i > 1:
                        cell.font = grey
                for pc in pct_cols:
                    ws.cell(r, pc).number_format = "0%"
                for pc in [3 + nB + 5] + ([3 + nB + 13] if G2 else []):
                    v = ws.cell(r, pc).value
                    if v is not None:
                        ws.cell(r, pc).fill = hi_fill if v >= 0.95 else mid_fill if v >= 0.85 else PatternFill()
                for nc in list(range(4, 4 + nB)) + [3 + nB + 1, 3 + nB + 3]:
                    ws.cell(r, nc).number_format = "#,##0"
            last = ws.max_row
            if last > first:
                ws.merge_cells(start_row=first, start_column=1, end_row=last, end_column=1)
                ws.merge_cells(start_row=first, start_column=3, end_row=last, end_column=3)
            ws.cell(first, 1).alignment = Alignment(vertical="center", wrap_text=True)
            ws.cell(first, 1).font = Font(bold=True)
            ws.cell(first, 3).alignment = Alignment(vertical="center")
        ws.freeze_panes = "C4"
        ws.column_dimensions["A"].width = 22
        ws.column_dimensions["B"].width = 30
        ws.column_dimensions["C"].width = 8
        for i in range(4, len(subs) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 9
        ws.column_dimensions[get_column_letter(len(subs))].width = 15
        ws.auto_filter.ref = f"A2:{get_column_letter(len(subs))}{ws.max_row}"

    ws = wb.create_sheet("Info", 0)
    ws.append(["HRN Tool - factor scan export"])
    ws["A1"].font = Font(bold=True, size=13)
    meta = payload.get("meta") or {}
    for k, v in [("Crash file", STATE.get("crash_name")), ("Road file", STATE.get("road_name")),
                 ("Crash-type column", meta.get("focus_column")), ("Second group", meta.get("group2")),
                 ("Restricted to", meta.get("extra")), ("Min. crashes per condition", meta.get("min_focus"))]:
        ws.append([k, v if v not in (None, "") else "-"])
    ws.append([])
    ws.append(["Sheet", "Crash type", "Values", "Crashes of this type", "All crashes", "Normal rate"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    for r in info_rows:
        ws.append(list(r))
        ws.cell(ws.max_row, 6).number_format = "0.0%"
    ws.append([])
    for line in ["Columns:",
                 "Total #, % - all crashes meeting the condition and their share of all crashes.",
                 "Group #, % - crashes of the type meeting the condition and their share of that type.",
                 "Prob. - Poisson P(X <= observed), mean = Total # x normal rate. Near 100% = clearly over-represented.",
                 "Expected - crashes of the type expected under the condition if it made no difference.",
                 "x normal - how many times more common the type is under the condition (1.0 = no difference).",
                 "Extra - observed minus expected.",
                 "Crashes / mi - crashes of the type under the condition divided by road miles: for a road attribute the "
                 "miles of road that have it; for a crash attribute the miles of the segments where those crashes "
                 "happened. All years in the file together.",
                 "Over-represented - Yes when Extra > 0 and the count reaches the minimum."]:
        ws.append([line])
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 34
    for col in "CDEF":
        ws.column_dimensions[col].width = 18

    if skipped_rows:
        ws = wb.create_sheet("Skipped columns")
        ws.append(["Sheet", "Column", "Reason"])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in skipped_rows:
            ws.append(list(r))
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 28
        ws.column_dimensions["C"].width = 45
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Step 3: Spearman correlation matrix across road segments
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Step 3: Cramer's V matrix (categories vs categories, per crash) + chi-square
# ----------------------------------------------------------------------------

CV_CACHE: dict = {}


def _crash_categories(col: str, series: pd.Series, glist, zero: bool, max_levels: int):
    """Category of every crash for one column: integer codes (-1 = blank) and labels.
    Number columns shown as ranges in the table use the table's groups; the rarest values are
    folded into 'Other' so a column has at most max_levels categories."""
    if glist:
        codes = np.full(len(series), -1, dtype=int)
        for gi, g in enumerate(glist):
            m = condition_mask(series, {"op": g["op"], "value": g["value"], "kind": "num"})
            codes[m & (codes < 0)] = gi
        labels = [str(g["value"]) for g in glist]
        if zero:
            codes[to_float(series) == 0] = -1
    else:
        texts = [_cell_text(v) for v in series.tolist()]
        if zero:
            texts = [None if _is_zero(t) else t for t in texts]
        cnt = pd.Series([t for t in texts if t is not None], dtype=object).value_counts()
        keep = list(cnt.index[:max_levels - 1]) if len(cnt) > max_levels else list(cnt.index)
        pos = {v: i for i, v in enumerate(keep)}
        other = len(cnt) > max_levels
        labels = [str(v) for v in keep] + ([f"Other ({len(cnt) - len(keep)} values)"] if other else [])
        oi = len(keep)
        codes = np.array([-1 if t is None else pos.get(t, oi if other else -1) for t in texts], dtype=int)
    # drop categories nobody falls in
    used = np.unique(codes[codes >= 0])
    remap = np.full(len(labels), -1, dtype=int)
    remap[used] = np.arange(len(used))
    codes = np.where(codes >= 0, remap[np.clip(codes, 0, None)], -1)
    return codes, [labels[i] for i in used]


def _crosstab(ci, cj, ki, kj):
    m = (ci >= 0) & (cj >= 0)
    t = np.bincount(ci[m] * kj + cj[m], minlength=ki * kj).reshape(ki, kj).astype(float)
    return t


def _chi_stats(t: np.ndarray):
    t = t[t.sum(axis=1) > 0][:, t.sum(axis=0) > 0]
    n = t.sum()
    r, c = t.shape
    if n == 0 or r < 2 or c < 2:
        return None
    exp = np.outer(t.sum(axis=1), t.sum(axis=0)) / n
    chi2 = float(((t - exp) ** 2 / exp).sum())
    df = (r - 1) * (c - 1)
    v = math.sqrt(chi2 / (n * (min(r, c) - 1)))
    return {"chi2": chi2, "df": df, "p": chi2_sf(chi2, df), "v": min(1.0, v), "n": int(n),
            "low": float((exp < 5).mean())}


def cramers_data(payload: dict) -> dict:
    """Cramer's V for every pair of the chosen columns, counted per crash (all crashes in the
    analysis, after any Step 2 restriction). Road columns use the road attribute of the segment
    each crash is on. Outcomes: focus crash (yes/no) and second group (e.g. KSI) (yes/no)."""
    need_state("combined")
    comb: pd.DataFrame = STATE["combined"]
    cols = []
    for c in payload.get("columns") or []:
        if c not in cols and c in comb.columns:
            cols.append(c)
    if not cols:
        raise ValueError("No columns are ticked for the matrix.")
    if len(cols) > 120:
        raise ValueError(f"{len(cols)} columns are ticked; Cramer's V is limited to 120.")
    max_levels = max(2, int(payload.get("max_levels") or 12))
    zero_blank = set(payload.get("zero_blank") or [])
    groups_in = payload.get("groups") or {}
    universe = apply_value_filter(comb, payload.get("extra"))
    u = comb.loc[universe].reset_index(drop=True)
    fm = apply_value_filter(u, payload.get("focus") or {})
    names, sources, kinds, codes, labels, colof = [], [], [], [], [], []
    for col in cols:
        cc, lb = _crash_categories(col, u[col], groups_in.get(col), col in zero_blank, max_levels)
        if len(lb) < 2:
            continue
        ordered = bool(groups_in.get(col)) or _column_codes(col, u[col].head(20000).tolist(), None,
                                                             col in zero_blank)[0] is not None
        names.append(pretty_col(col)); sources.append("road" if col.startswith("road:") else "crash")
        kinds.append("ordered" if ordered else "unordered"); codes.append(cc); labels.append(lb); colof.append(col)
    g1 = payload.get("group1_name") or "Focus"
    outcome = []
    names.append(f"{g1} crash (yes/no)"); sources.append("outcome"); kinds.append("yes/no")
    codes.append(fm.astype(int)); labels.append([f"not {g1}", g1]); colof.append("__focus"); outcome.append(len(names) - 1)
    g2 = payload.get("group2")
    if g2 and g2.get("column") and g2.get("values"):
        gm = apply_value_filter(u, g2) & fm
        n2 = payload.get("group2_name") or "Group 2"
        names.append(f"{n2} {g1} crash (yes/no)"); sources.append("outcome"); kinds.append("yes/no")
        codes.append(gm.astype(int)); labels.append([f"not {n2}", n2]); colof.append("__g2"); outcome.append(len(names) - 1)
    k = len(names)
    V = np.full((k, k), np.nan); P = np.full((k, k), np.nan); NN = np.zeros((k, k)); LOW = np.full((k, k), np.nan)
    for i in range(k):
        V[i, i] = 1.0
        for j in range(i):
            st = _chi_stats(_crosstab(codes[i], codes[j], len(labels[i]), len(labels[j])))
            if st is None:
                continue
            V[i, j] = V[j, i] = st["v"]; P[i, j] = P[j, i] = st["p"]
            NN[i, j] = NN[j, i] = st["n"]; LOW[i, j] = LOW[j, i] = st["low"]
    CV_CACHE.clear()
    CV_CACHE.update(codes=codes, labels=labels, names=names)
    return {"names": names, "sources": sources, "kinds": kinds, "labels": labels, "colof": colof,
            "outcome": outcome, "V": V, "P": P, "N": NN, "LOW": LOW, "n_crashes": int(len(u)), "n_focus": int(fm.sum()),
            "counts": [[int((c == x).sum()) for x in range(len(l))] for c, l in zip(codes, labels)]}


def cramers_json(payload: dict) -> dict:
    d = cramers_data(payload)
    r = lambda M, nd: [[None if not np.isfinite(x) else round(float(x), nd) for x in row] for row in M]
    return clean({"names": d["names"], "sources": d["sources"], "kinds": d["kinds"], "labels": d["labels"],
                  "colof": d["colof"], "outcome": d["outcome"], "V": r(d["V"], 3), "P": r(d["P"], 8),
                  "N": [[int(x) for x in row] for row in d["N"]], "LOW": r(d["LOW"], 3), "counts": d["counts"],
                  "n_crashes": d["n_crashes"], "n_focus": d["n_focus"]})


def crosstab_json(payload: dict) -> dict:
    """Cross-tab for one pair of the last Cramer's V run: counts, row %, x expected."""
    if not CV_CACHE:
        raise ValueError("Build the Cramer's V matrix first.")
    i, j = int(payload.get("i")), int(payload.get("j"))
    codes, labels, names = CV_CACHE["codes"], CV_CACHE["labels"], CV_CACHE["names"]
    t = _crosstab(codes[i], codes[j], len(labels[i]), len(labels[j]))
    st = _chi_stats(t)
    n = t.sum()
    exp = np.outer(t.sum(axis=1), t.sum(axis=0)) / n if n else t
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(exp > 0, t / exp, np.nan)
        rowpct = np.where(t.sum(axis=1, keepdims=True) > 0, t / t.sum(axis=1, keepdims=True), np.nan)
    return clean({"row": names[i], "col": names[j], "row_labels": labels[i], "col_labels": labels[j],
                  "counts": t.astype(int).tolist(), "ratio": ratio.round(3).tolist(), "rowpct": rowpct.round(4).tolist(),
                  "stats": st})


def export_cramers_xlsx(payload: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    d = cramers_data(payload)
    names, V, k = d["names"], d["V"], len(d["names"])
    wb = Workbook()
    ws = wb.active
    ws.title = "Cramer's V matrix"
    ws.cell(1, 1, "Cramer's V")
    for j, nm in enumerate(names, start=2):
        ws.cell(1, j, nm).alignment = Alignment(text_rotation=90, horizontal="center")
        ws.cell(1, j).font = Font(bold=True)
    for i, nm in enumerate(names, start=2):
        ws.cell(i, 1, nm).font = Font(bold=True)
        for j in range(k):
            v = V[i - 2, j]
            ws.cell(i, j + 2, None if not np.isfinite(v) else round(float(v), 3))
    ws.conditional_formatting.add(f"B2:{get_column_letter(k + 1)}{k + 1}",
                                  ColorScaleRule(start_type="num", start_value=0, start_color="FFFFFF",
                                                 end_type="num", end_value=1, end_color="184F95"))
    gold = PatternFill("solid", fgColor="FFF3CC")
    for i in d["outcome"]:
        ws.cell(i + 2, 1).fill = gold; ws.cell(1, i + 2).fill = gold
    ws.column_dimensions["A"].width = 40
    for j in range(2, k + 2):
        ws.column_dimensions[get_column_letter(j)].width = 6.5
    ws.row_dimensions[1].height = 170
    ws.freeze_panes = "B2"
    ws = wb.create_sheet("Pairs")
    ws.append(["Column 1", "Column 2", "Cramer's V", "Chi-square p", "Crashes used", "Share of cells with expected < 5", "Type"])
    for c in ws[1]:
        c.font = Font(bold=True)
    rows = []
    for i in range(k):
        for j in range(i):
            if not np.isfinite(V[i, j]):
                continue
            typ = "with crash outcome" if (i in d["outcome"] or j in d["outcome"]) else "between columns"
            rows.append([names[i], names[j], round(float(V[i, j]), 3), float(d["P"][i, j]), int(d["N"][i, j]),
                         round(float(d["LOW"][i, j]), 3), typ])
    rows.sort(key=lambda x: -x[2])
    for row in rows:
        ws.append(row)
    for col, w in zip("ABCDEFG", [40, 40, 11, 13, 13, 16, 20]):
        ws.column_dimensions[col].width = w
    ws = wb.create_sheet("Categories")
    ws.append(["Column", "Kind", "Category", "Crashes"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for nm, kd, lb, cn in zip(names, d["kinds"], d["labels"], d["counts"]):
        for l, n in zip(lb, cn):
            ws.append([nm, kd, l, n])
    for col, w in zip("ABCD", [40, 11, 40, 10]):
        ws.column_dimensions[col].width = w
    ws = wb.create_sheet("Notes", 0)
    for line in [["Cramer's V matrix (per crash)"], ["Crash file", STATE.get("crash_name") or "-"],
                 ["Crashes used", f"{d['n_crashes']:,} ({d['n_focus']:,} focus crashes)"], [],
                 ["Unit", "one row per crash; road columns use the attribute of the segment each crash is on"],
                 ["Cramer's V", "0 = unrelated, 1 = one column fully determines the other; no direction"],
                 ["Chi-square p", "chance of a pattern this strong if the two columns were unrelated"],
                 ["Expected < 5", "if many cells have an expected count under 5, the p-value is less reliable"],
                 ["Outcomes", "Focus crash (yes/no): is the crash of the focus type; second group (yes/no) likewise"]]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Step 3: column check - one row per value, binomial tests against pN and pC
# ----------------------------------------------------------------------------

def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact, summed in log space (no scipy needed)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    log_pmf = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lp + (n - k) * lq
    total, term, i = 0.0, log_pmf, k
    peak = log_pmf
    while i <= n:
        total += math.exp(term - peak)
        if i > n * p and term - peak < -40:
            break
        term += math.log(n - i) - math.log(i + 1) + lp - lq if i < n else 0
        if term > peak:  # moving towards the mode: rescale
            total *= math.exp(peak - term)
            peak = term
        i += 1
    return min(1.0, math.exp(peak) * total)


def column_check(payload: dict) -> dict:
    """For one column of the factor table: per value NF, pF, pN, pC, crashes / mi, the ratios
    pF/pN and pF/pC, one-sided binomial tests (H0: focus crashes fall on the value in proportion
    to its share of network miles / of all crashes), Based On and weight.

    Road column  : pN = share of network miles with the value; crash rate = NF / those miles.
    Crash column : no network share (pN not applicable); crash rate = NF / miles of the segments
                   where those crashes happened; only the Tot Crash comparison applies."""
    need_state("combined", "roads")
    comb: pd.DataFrame = STATE["combined"]
    attr: pd.DataFrame = STATE["road_attr"]
    lengths = STATE.get("road_len")
    n_roads = len(STATE["roads"])
    col = payload.get("column")
    conds = payload.get("conditions") or []
    if not col or not conds:
        raise ValueError("Choose a column.")
    alpha = float(payload.get("alpha") or 0.05)
    min_n = max(0, int(payload.get("min_n") or 0))
    min_ratio = float(payload.get("min_ratio") or 0)
    focus = payload.get("focus") or {}
    universe = apply_value_filter(comb, payload.get("extra"))
    u = comb.loc[universe]
    fm = apply_value_filter(u, focus)
    N, F = len(u), int(fm.sum())
    if F == 0:
        raise ValueError("No focus crashes.")
    rid_all = u["__road_id"].to_numpy(dtype=int)
    rid_f = rid_all[fm]
    total_len = float(np.nansum(lengths)) if lengths is not None else None
    is_road = col.startswith("road:")
    rows = []
    for c in conds:
        if is_road:
            seg_m = condition_mask(attr[col[5:]], c)
            on = seg_m[rid_all]
            n_all = int(on.sum())
            NF = int((on & fm).sum())
            miles = float(np.nansum(lengths[seg_m])) if lengths is not None else None
            pN = (miles / total_len) if (miles is not None and total_len) else None
            n_seg = int(seg_m.sum())
        else:
            m = condition_mask(u[col], c)
            n_all = int(m.sum())
            mf = m & fm
            NF = int(mf.sum())
            seg_m = np.zeros(n_roads, dtype=bool)
            seg_m[rid_all[mf]] = True
            miles = float(np.nansum(lengths[seg_m])) if lengths is not None else None
            pN = None
            n_seg = int(seg_m.sum())
        pF = NF / F
        pC = n_all / N if N else 0.0
        p_net = binom_sf(NF, F, pN) if pN is not None else None
        p_tot = binom_sf(NF, F, pC)
        r_net = (pF / pN) if pN else None
        r_tot = (pF / pC) if pC else None
        enough = NF >= min_n
        net = enough and pN is not None and pF > pN and p_net < alpha and (r_net or 0) >= (min_ratio or 0)
        tot = enough and pF > pC and p_tot < alpha and (r_tot or 0) >= (min_ratio or 0)
        based = "Both" if (net and tot) else "Network" if net else "Tot Crash" if tot else \
            ("Below min. crashes" if not enough else "Not over-represented")
        rows.append({"value": str(c["value"]), "label": cond_label(c), "op": c["op"], "kind": c["kind"],
                     "segments": n_seg, "miles": miles, "n_all": n_all, "NF": NF, "pF": pF, "pN": pN, "pC": pC,
                     "cpm": (NF / miles) if miles else None, "r_net": r_net, "r_tot": r_tot,
                     "p_net": p_net, "p_tot": p_tot, "based_on": based, "weight": POINTS.get(based, 0)})
    return clean({"column": col, "label": pretty_col(col), "source": "road" if is_road else "crash", "rows": rows,
                  "N": N, "F": F, "network_miles": total_len, "alpha": alpha, "min_n": min_n, "min_ratio": min_ratio,
                  "avg_cpm": (F / total_len) if total_len else None})


def compare_methods(payload: dict) -> dict:
    """Binomial test (Network / Tot Crash / Both) on every screened column, next to its Spearman rho or
    Cramer's V from the matrices (sent by the page in `screen`). A column is flagged by the binomial
    method when at least one value qualifies; by screening when |rho| or V >= the threshold and p < alpha."""
    alpha = float(payload.get("alpha") or 0.05)
    thr = float(payload.get("threshold") if payload.get("threshold") is not None else 0.1)
    base = {k: payload.get(k) for k in ("focus", "extra", "alpha", "min_n", "min_ratio")}
    out = []
    for item in payload.get("columns") or []:
        col, conds = item.get("column"), item.get("conditions") or []
        if not col or not conds:
            continue
        d = column_check(dict(base, column=col, conditions=conds))
        quals = [r for r in d["rows"] if r["based_on"] in ("Both", "Network", "Tot Crash")]
        quals.sort(key=lambda r: (-r["weight"], -(r["r_tot"] or 0)))
        sc = item.get("screen") or {}
        stat, sp = sc.get("value"), sc.get("p")
        s_flag = stat is not None and abs(stat) >= thr and (sp is None or sp < alpha)
        b_flag = bool(quals)
        agree = ("Both methods" if s_flag and b_flag else "Binomial only" if b_flag
                 else "Spearman / V only" if s_flag else "Neither")
        out.append({"column": col, "label": d["label"], "source": d["source"], "ordered": bool(item.get("ordered")),
                    "method": sc.get("method"), "stat": stat, "p": sp, "at": sc.get("at") or "",
                    "screen_flag": s_flag, "binom_flag": b_flag, "agree": agree,
                    "max_points": max([r["weight"] for r in quals], default=0),
                    "values": [{"label": r["label"], "value": r["value"], "based_on": r["based_on"], "weight": r["weight"],
                                "NF": r["NF"], "r_net": r["r_net"], "r_tot": r["r_tot"], "p_net": r["p_net"],
                                "p_tot": r["p_tot"], "cpm": r["cpm"]} for r in quals],
                    "n_values": len(d["rows"])})
    order = {"Both methods": 0, "Spearman / V only": 1, "Binomial only": 2, "Neither": 3}
    out.sort(key=lambda r: (order[r["agree"]], -abs(r["stat"] or 0), -r["max_points"]))
    counts = {k: sum(1 for r in out if r["agree"] == k) for k in order}
    return clean({"rows": out, "counts": counts, "alpha": alpha, "threshold": thr,
                  "min_n": payload.get("min_n") or 0, "min_ratio": payload.get("min_ratio") or 0,
                  "group1_name": payload.get("group1_name") or "Focus"})


def export_compare_xlsx(payload: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    d = compare_methods(payload)
    wb = Workbook()
    ws = wb.active
    ws.title = "Comparison"
    g1 = d["group1_name"]
    ws.append([f"Spearman + Cramer's V vs binomial test - {g1}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"Screening flags a column when |rho| or V >= {d['threshold']} and p < {d['alpha']}. "
               f"Binomial flags a column when at least one value is Network / Tot Crash / Both "
               f"(alpha {d['alpha']}, min. crashes {d['min_n']}, min. ratio {d['min_ratio'] or 'none'})."])
    ws.append(["Result: " + ", ".join(f"{k} {v}" for k, v in d["counts"].items())])
    ws.append([])
    head = ["Column", "Source", "Type", "Spearman rho", "p (rho)", "Cramer's V", "p (V)", "Screening flags", "Binomial: qualifying values (Based On, weight)",
            "Max points", "Binomial flags", "Agreement"]
    ws.append(head)
    hr = ws.max_row
    fill = PatternFill("solid", fgColor="3F4E66")
    for c in ws[hr]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = fill; c.alignment = Alignment(wrap_text=True, vertical="center")
    colors = {"Both methods": "D9EAD3", "Spearman / V only": "DDE7F5", "Binomial only": "FFF2CC", "Neither": "F2F2F2"}
    for r in d["rows"]:
        vals = "; ".join(f"{v['label']} ({v['based_on']}, {v['weight']})" for v in r["values"]) or "none"
        is_r = r["method"] == "Spearman"
        is_v = r["method"] == "Cramer's V"
        ws.append([r["label"], r["source"], "ordered" if r["ordered"] else "unordered",
                   r["stat"] if is_r else None, r["p"] if is_r else None,
                   r["stat"] if is_v else None, r["p"] if is_v else None,
                   "yes" if r["screen_flag"] else "no", vals, r["max_points"],
                   "yes" if r["binom_flag"] else "no", r["agree"]])
        ws.cell(ws.max_row, 12).fill = PatternFill("solid", fgColor=colors[r["agree"]])
        for ci, fmt_ in ((4, "0.00"), (5, "0.0000"), (6, "0.00"), (7, "0.0000")):
            ws.cell(ws.max_row, ci).number_format = fmt_
        ws.cell(ws.max_row, 9).alignment = Alignment(wrap_text=True, vertical="top")
    for i, w in enumerate([26, 8, 11, 10, 9, 10, 9, 10, 60, 9, 10, 18], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(hr + 1, 2)
    ws2 = wb.create_sheet("Binomial values")
    ws2.append(["Column", "Value", "Based On", "Weight", "NF", "pF / pN", "p (vs pN)", "pF / pC", "p (vs pC)", "Crashes / mi"])
    for c in ws2[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = fill
    for r in d["rows"]:
        for v in r["values"]:
            ws2.append([r["label"], v["value"], v["based_on"], v["weight"], v["NF"], v["r_net"], v["p_net"], v["r_tot"], v["p_tot"], v["cpm"]])
    for i, w in enumerate([26, 28, 12, 8, 8, 9, 10, 9, 10, 11], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def export_column_check_xlsx(payload: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    d = column_check(payload)
    wb = Workbook()
    ws = wb.active
    ws.title = _sheet_title(d["label"], set())
    head = ["Value", "Segments", "Miles", "pN", "All crashes", "pC", "NF", "pF", "Crashes / mi",
            "pF / pN", "p (vs pN)", "pF / pC", "p (vs pC)", "Based On", "Weight"]
    ws.append([f"{d['label']} - {payload.get('group1_name') or 'Focus'} crashes"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"{d['F']:,} focus crashes of {d['N']:,}; alpha = {d['alpha']}; min. crashes = {d['min_n']}; "
               f"min. ratio = {d['min_ratio'] or 'none'}"])
    ws.append([])
    ws.append(head)
    fill = PatternFill("solid", fgColor="44546A")
    thin = Side(style="thin", color="C5CCD4")
    for i in range(1, len(head) + 1):
        c = ws.cell(4, i)
        c.fill, c.font = fill, Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    for r in d["rows"]:
        ws.append([r["value"], r["segments"], None if r["miles"] is None else round(r["miles"], 2), r["pN"], r["n_all"],
                   r["pC"], r["NF"], r["pF"], None if r["cpm"] is None else round(r["cpm"], 2),
                   None if r["r_net"] is None else round(r["r_net"], 2), r["p_net"],
                   None if r["r_tot"] is None else round(r["r_tot"], 2), r["p_tot"], r["based_on"], r["weight"]])
        rr = ws.max_row
        for i in (4, 6, 8):
            ws.cell(rr, i).number_format = "0.0%"
        for i in (11, 13):
            ws.cell(rr, i).number_format = "0.0000"
        for i in range(1, len(head) + 1):
            ws.cell(rr, i).border = Border(bottom=thin)
    for i, w in enumerate([30, 9, 9, 8, 10, 8, 8, 8, 11, 8, 10, 8, 10, 20, 8], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.append([])
    for line in ["NF = focus crashes with the value; pF = NF / all focus crashes.",
                 "pN = share of network miles with the value (road columns only).",
                 "pC = share of all crashes with the value.",
                 "Crashes / mi = NF / miles (road column: miles with the value; crash column: miles of the segments where those crashes happened).",
                 "p (vs pN), p (vs pC) = one-sided exact binomial test, H0: focus crashes fall on the value in proportion to pN / pC.",
                 "Network = pF > pN and p (vs pN) < alpha; Tot Crash = pF > pC and p (vs pC) < alpha; Both = both. "
                 "Weight: Both 2, Network / Tot Crash 1. Also needs NF >= min. crashes and ratio >= min. ratio when set."]:
        ws.append([line])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


YES_WORDS = {"yes", "y", "true", "t", "1"}


def _is_zero(t) -> bool:
    try:
        return t is not None and float(t) == 0
    except ValueError:
        return False


def _column_codes(col: str, values: list, order: list | None, zero_blank: bool = False):
    """Turn one column's cell values into numbers that can be ranked.
    zero_blank: treat 0 as 'not recorded' (blank).
    Returns (array of float with NaN for blank/unreadable, coding text) or (None, reason)."""
    texts = [_cell_text(v) for v in values]
    if zero_blank:
        texts = [None if _is_zero(t) else t for t in texts]
    present = [t for t in texts if t is not None]
    if not present:
        return None, "no values"
    uniq = sorted(set(present))
    if len(uniq) == 1:
        return None, "only one value"
    if order:  # user-defined order
        pos = {str(v).strip(): i for i, v in enumerate(order)}
        return np.array([pos.get(t, np.nan) if t is not None else np.nan for t in texts], dtype=float), \
            "your order: " + " < ".join(str(v) for v in order[:6]) + (" ..." if len(order) > 6 else "")
    if is_age_column(col):
        return age_values(values), "age (years)"
    nums = to_float(pd.Series(present))
    if np.isfinite(nums).mean() >= 0.8:
        return to_float(pd.Series(texts, dtype=object)), "number"
    lead = [re.match(r"\s*(-?\d+(?:\.\d+)?)", t) for t in uniq]
    if all(lead) and len(uniq) > 2:
        m = {t: float(x.group(1)) for t, x in zip(uniq, lead)}
        return np.array([m.get(t, np.nan) if t is not None else np.nan for t in texts], dtype=float), "leading number"
    ranks = [severity_rank(t) for t in uniq]
    if all(r is not None for r in ranks) and len(uniq) > 2:
        m = {t: r[0] for t, r in zip(uniq, ranks)}
        return np.array([m.get(t, np.nan) if t is not None else np.nan for t in texts], dtype=float), \
            "severity K=0 ... O=4"
    if len(uniq) == 2:
        yes = [u for u in uniq if u.lower() in YES_WORDS]
        if yes:
            one = yes[0]
        else:
            cnt = pd.Series(present).value_counts()
            one = str(cnt.index[-1])  # the rarer value
        return np.array([(1.0 if t == one else 0.0) if t is not None else np.nan for t in texts], dtype=float), \
            f"1 = {one}"
    return None, f"unordered ({len(uniq)} values)"


def _value_profile(raw: list, codes: np.ndarray | None, max_items: int = 40) -> dict:
    """Raw values of a column with counts and the number each was ranked as."""
    texts = [_cell_text(v) for v in raw]
    present = [t for t in texts if t is not None]
    nums = to_float(pd.Series(present, dtype=object)) if present else np.array([])
    numeric = bool(len(present)) and bool(np.isfinite(nums).mean() >= 0.8)
    code_of: dict = {}
    if codes is not None:
        for t, c in zip(texts, codes):
            if t is not None and t not in code_of:
                code_of[t] = None if not np.isfinite(c) else float(c)
    cnt = pd.Series(present, dtype=object).value_counts()
    items = [{"value": str(k), "count": int(n), "code": code_of.get(k)} for k, n in cnt.items()]
    if numeric:
        items.sort(key=lambda x: (to_float(pd.Series([x["value"]]))[0] if np.isfinite(to_float(pd.Series([x["value"]]))[0]) else math.inf))
    else:
        items.sort(key=lambda x: (x["code"] is None, x["code"] if x["code"] is not None else 0, -x["count"]))
    used = [t for t in present if code_of.get(t) is not None] if codes is not None else present
    un = to_float(pd.Series(used, dtype=object)) if (numeric and used) else np.array([])
    fin = un[np.isfinite(un)]
    return {"kind": "numbers" if numeric else "text", "n_distinct": int(len(cnt)), "n_values": len(used),
            "n_blank": int(len(texts) - len(used)), "items": items[:max_items], "more": max(0, len(items) - max_items),
            "min": float(fin.min()) if len(fin) else None, "median": float(np.median(fin)) if len(fin) else None,
            "max": float(fin.max()) if len(fin) else None}


def correlation_data(payload: dict) -> dict:
    """Spearman correlation matrix with ONE variable per column, across road segments.

    One data row per road segment (segments shorter than min_len are left out).
    Road column  -> the segment's own value, ranked (number, leading number, or 1/0 for two values).
    Crash column -> average over the segment's focus crashes (e.g. average driver age; for a
                    two-value column the share of crashes with the '1' value).
    Outcomes     -> focus crashes / mi and second-group (e.g. KSI) crashes / mi.
    Unordered columns (lighting, weather, ...) are left out unless an order is given.
    Pairs are computed on the segments where both variables have a value."""
    need_state("combined", "roads")
    lengths = STATE.get("road_len")
    if lengths is None:
        raise ValueError("Segment lengths are not available. Reload the road file.")
    comb: pd.DataFrame = STATE["combined"]
    attr: pd.DataFrame = STATE["road_attr"]
    n_roads = len(STATE["roads"])
    cols = []
    for c in payload.get("columns") or []:
        if c not in cols:
            cols.append(c)
    if not cols:
        raise ValueError("No columns are shown in the table.")
    max_vars = int(payload.get("max_vars") or 150)
    if len(cols) > max_vars:
        raise ValueError(f"{len(cols)} columns are shown; the matrix is limited to {max_vars}. Use the filters to show fewer.")
    orders = payload.get("orders") or {}
    zero_blank = set(payload.get("zero_blank") or [])
    min_len = max(0.0, float(payload.get("min_len") or 0))
    focus = payload.get("focus") or {}
    universe = apply_value_filter(comb, payload.get("extra"))
    fm = universe & apply_value_filter(comb, focus)
    fc = comb.loc[fm]
    rid = fc["__road_id"].to_numpy(dtype=int)
    total = np.bincount(rid, minlength=n_roads).astype(float)
    keep = np.isfinite(lengths) & (lengths > 0) & (lengths >= min_len)

    groups_in = payload.get("groups") or {}          # {col: [{"op": "rng"/"age", "value": "0-19999"}, ...]} from the table
    split = payload.get("split_unordered", True)
    split_max = max(1, int(payload.get("split_max") or 8))

    names, sources, codings, data, skipped, profiles, colof = [], [], [], {}, [], [], []

    def add(key, v, src, info, prof, col):
        k, i2 = key, 2
        while k in data:
            k, i2 = f"{key} ({i2})", i2 + 1
        data[k] = v
        names.append(k); sources.append(src); codings.append(info); profiles.append(prof); colof.append(col)

    def per_segment_mean(vals):
        ok = np.isfinite(vals)
        sums = np.bincount(rid[ok], weights=vals[ok], minlength=n_roads)
        cnts = np.bincount(rid[ok], minlength=n_roads).astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(cnts > 0, sums / cnts, np.nan)

    def group_codes(series, glist, zero):
        """Index of the table group (1, 2, 3 ...) each cell falls in; NaN when in none."""
        codes = np.full(len(series), np.nan)
        for gi, g in enumerate(glist, start=1):
            m = condition_mask(series, {"op": g["op"], "value": g["value"], "kind": "num"})
            codes[m & np.isnan(codes)] = gi
        if zero:
            raw = to_float(series)
            codes[raw == 0] = np.nan
        return codes

    for col in cols:
        is_road = col.startswith("road:")
        if is_road and col[5:] not in attr.columns:
            continue
        if not is_road and col not in fc.columns:
            continue
        series = attr[col[5:]] if is_road else fc[col]
        raw = series.tolist()
        label = pretty_col(col)
        zb = col in zero_blank
        glist = groups_in.get(col)
        if glist and not orders.get(col):
            # number column grouped exactly as in the table (e.g. AADT 0-19999, age 16-20)
            codes = group_codes(series, glist, zb)
            info = "table groups: " + ", ".join(f"{g['value']} = {i}" for i, g in enumerate(glist[:5], start=1)) \
                + (" ..." if len(glist) > 5 else "") + ("; 0 treated as blank" if zb else "")
            # list the groups themselves, with how many segments / crashes fall in each
            n_zero = int((to_float(series) == 0).sum()) if zb else 0
            cnt = []
            for i, g in enumerate(glist, start=1):
                if zb and range_bounds(str(g["value"]))[0] == 0 and (range_bounds(str(g["value"]))[1] is None):
                    cnt.append((g["value"], n_zero, None))  # the "0" group itself, treated as blank
                else:
                    cnt.append((g["value"], int((codes == i).sum()), float(i)))
            prof = {"kind": "numbers", "over": "road segments" if is_road else "focus crashes",
                    "n_values": int(np.isfinite(codes).sum()), "n_blank": int((~np.isfinite(codes)).sum()),
                    "n_distinct": len(glist), "items": [{"value": v, "count": n, "code": c} for v, n, c in cnt],
                    "more": 0, "min": None, "median": None, "max": None}
            if is_road:
                add(label, codes, "road", info, prof, col)
            else:
                add(label, per_segment_mean(codes), "crash", "average of crashes, " + info, prof, col)
            continue
        vals, info = _column_codes(col, raw, orders.get(col), zb)
        prof = _value_profile(raw, vals)
        prof["over"] = "road segments" if is_road else "focus crashes"
        if vals is None:
            if split and info.startswith("unordered"):
                # one variable per value: road -> 1/0 per segment; crash -> crashes / mi with that value
                texts = [_cell_text(x) for x in raw]
                top = pd.Series([t for t in texts if t is not None], dtype=object).value_counts().index[:split_max]
                tarr = np.array(texts, dtype=object)
                for val in top:
                    m = tarr == val
                    if is_road:
                        v = m.astype(float)
                        pv = {"kind": "text", "over": "road segments", "n_values": int(len(texts)), "n_blank": 0,
                              "n_distinct": 2, "items": [{"value": str(val), "count": int(m.sum()), "code": 1.0},
                                                         {"value": "any other value", "count": int((~m).sum()), "code": 0.0}],
                              "more": 0, "min": None, "median": None, "max": None}
                        add(f"{label} = {val}", v, "road", f"1 = {val}, 0 = any other value (one variable per value)", pv, col)
                    else:
                        cnt = np.bincount(rid[m], minlength=n_roads).astype(float)
                        with np.errstate(divide="ignore", invalid="ignore"):
                            v = np.where(lengths > 0, cnt / lengths, np.nan)
                        pv = {"kind": "text", "over": "focus crashes", "n_values": int(m.sum()), "n_blank": 0,
                              "n_distinct": 1, "items": [{"value": str(val), "count": int(m.sum()), "code": None}],
                              "more": 0, "min": None, "median": None, "max": None, "rate": True}
                        add(f"{label} = {val} crashes / mi", v, "valout",
                            f"focus crashes with {label} = {val} on the segment / segment length", pv, col)
                n_other = len(pd.Series([t for t in texts if t is not None]).value_counts()) - len(top)
                if n_other > 0:
                    skipped.append({"column": col, "label": label, "reason": f"split: {len(top)} most common values used, {n_other} rarer values not shown",
                                    "values": [], "info": True})
                continue
            skipped.append({"column": col, "label": label, "reason": info,
                            "values": [str(x) for x in pd.Series([_cell_text(x) for x in raw]).dropna().value_counts().index[:40]]})
            continue
        if zb:
            info += "; 0 treated as blank"
        if is_road:
            add(label, vals, "road", info, prof, col)
        else:
            info = ("share of crashes, " + info) if info.startswith("1 = ") else ("average of crashes, " + info)
            add(label, per_segment_mean(vals), "crash", info, prof, col)
    if not names:
        raise ValueError("None of the shown columns can be ranked. Give an order for an unordered column, or show more columns.")
    value_outcomes = [i for i, sname in enumerate(sources) if sname == "valout"]
    for i in value_outcomes:
        sources[i] = "outcome"
    outcome = []
    g1 = payload.get("group1_name") or "Focus"
    with np.errstate(divide="ignore", invalid="ignore"):
        data[f"{g1} crashes / mi"] = np.where(lengths > 0, total / lengths, np.nan)
    names.append(f"{g1} crashes / mi"); sources.append("outcome"); codings.append("crashes / segment length")
    outcome.append(len(names) - 1); profiles.append(None); colof.append("__outcome1")
    g2 = payload.get("group2")
    if g2 and g2.get("column") and g2.get("values"):
        gm = fm & apply_value_filter(comb, g2)
        gr = np.bincount(comb.loc[gm, "__road_id"].to_numpy(dtype=int), minlength=n_roads).astype(float)
        nm = f"{payload.get('group2_name') or 'Group 2'} crashes / mi"
        with np.errstate(divide="ignore", invalid="ignore"):
            data[nm] = np.where(lengths > 0, gr / lengths, np.nan)
        names.append(nm); sources.append("outcome"); codings.append("crashes / segment length")
        outcome.append(len(names) - 1); profiles.append(None); colof.append("__outcome2")

    # order: explanatory columns, then per-value outcomes, then the main outcomes (focus, KSI) at the bottom
    perm = [i for i in range(len(names)) if i not in set(outcome) | set(value_outcomes)] + value_outcomes + outcome
    names = [names[i] for i in perm]; sources = [sources[i] for i in perm]; codings = [codings[i] for i in perm]
    profiles = [profiles[i] for i in perm]; colof = [colof[i] for i in perm]
    pos = {old: new for new, old in enumerate(perm)}
    main_outcome = [pos[i] for i in outcome]
    value_outcomes = [pos[i] for i in value_outcomes]
    outcome = value_outcomes + main_outcome
    # the data table and the matrix follow exactly the order of `names`
    df = pd.DataFrame({k: data[k][keep] for k in names})
    rho = df.corr(method="spearman", min_periods=10)
    assert list(rho.columns) == names
    notna = df.notna().astype(float)
    pair_n = notna.T.dot(notna)
    seg_ids = np.flatnonzero(keep)
    for i in outcome:  # numbers per segment
        x = df[names[i]].to_numpy(dtype=float)
        x = x[np.isfinite(x)]
        prev = profiles[i] or {}
        profiles[i] = {"kind": "numbers", "over": "road segments", "n_values": int(len(x)), "n_blank": 0,
                       "n_distinct": int(len(np.unique(x))), "items": prev.get("items", []) if prev.get("rate") else [], "more": 0,
                       "min": float(x.min()) if len(x) else None, "median": float(np.median(x)) if len(x) else None,
                       "max": float(x.max()) if len(x) else None, "zeros": int((x == 0).sum())}
    return {"main_outcome": main_outcome, "colof": colof, "profiles": profiles, "names": names, "sources": sources, "codings": codings, "outcome": outcome, "rho": rho.to_numpy(),
            "n": pair_n.to_numpy(), "df": df, "seg_ids": seg_ids, "skipped": skipped,
            "n_segments": int(keep.sum()), "n_dropped": int((~keep).sum()), "min_len": min_len,
            "n_with_crashes": int(((total > 0) & keep).sum())}


def correlation_json(payload: dict) -> dict:
    d = correlation_data(payload)
    r = d["rho"]
    return clean({"main_outcome": d["main_outcome"], "colof": d["colof"], "profiles": d["profiles"], "names": d["names"], "sources": d["sources"], "codings": d["codings"], "outcome": d["outcome"],
                  "rho": [[None if not np.isfinite(x) else round(float(x), 3) for x in row] for row in r],
                  "n": [[int(x) for x in row] for row in d["n"]], "skipped": d["skipped"],
                  "n_segments": d["n_segments"], "n_dropped": d["n_dropped"], "min_len": d["min_len"],
                  "n_with_crashes": d["n_with_crashes"]})


def export_correlation_xlsx(payload: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    d = correlation_data(payload)
    names, r = d["names"], d["rho"]
    k = len(names)
    wb = Workbook()
    ws = wb.active
    ws.title = "Spearman matrix"
    ws.cell(1, 1, "Spearman rho")
    for j, nm in enumerate(names, start=2):
        ws.cell(1, j, nm).alignment = Alignment(text_rotation=90, horizontal="center")
        ws.cell(1, j).font = Font(bold=True)
    for i, nm in enumerate(names, start=2):
        ws.cell(i, 1, nm).font = Font(bold=True)
        for j in range(k):
            v = r[i - 2, j]
            ws.cell(i, j + 2, None if not np.isfinite(v) else round(float(v), 3))
    last = f"{get_column_letter(k + 1)}{k + 1}"
    ws.conditional_formatting.add(f"B2:{last}", ColorScaleRule(start_type="num", start_value=-1, start_color="2A78D6",
                                                               mid_type="num", mid_value=0, mid_color="F0EFEC",
                                                               end_type="num", end_value=1, end_color="E34948"))
    gold = PatternFill("solid", fgColor="FFF3CC")
    for i in d["outcome"]:
        ws.cell(i + 2, 1).fill = gold
        ws.cell(1, i + 2).fill = gold
    ws.column_dimensions["A"].width = 42
    for j in range(2, k + 2):
        ws.column_dimensions[get_column_letter(j)].width = 6.5
    ws.row_dimensions[1].height = 170
    ws.freeze_panes = "B2"

    ws = wb.create_sheet("Pairs")
    ws.append(["Variable 1", "Variable 2", "Spearman rho", "|rho|", "Segments used", "Type"])
    for c in ws[1]:
        c.font = Font(bold=True)
    rows = []
    for i in range(k):
        for j in range(i):
            v = r[i, j]
            if not np.isfinite(v):
                continue
            typ = "with crash outcome" if (i in d["outcome"] or j in d["outcome"]) else "between variables"
            rows.append([names[i], names[j], round(float(v), 3), round(abs(float(v)), 3), int(d["n"][i, j]), typ])
    rows.sort(key=lambda x: -x[3])
    for row in rows:
        ws.append(row)
    ws.auto_filter.ref = f"A1:F{max(2, ws.max_row)}"
    for col, w in zip("ABCDEF", [42, 42, 13, 8, 14, 20]):
        ws.column_dimensions[col].width = w

    ws = wb.create_sheet("Variables")
    ws.append(["#", "Variable", "Source", "How it is ranked"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for i, (nm, src, cd) in enumerate(zip(names, d["sources"], d["codings"]), start=1):
        ws.append([i, nm, src, cd])
    for sk in d["skipped"]:
        ws.append([None, sk["label"], "left out", sk["reason"] + ("" if sk["reason"] in ("only one value", "no values")
                                                                  else " - give an order in the tool to include it")])
    for col, w in zip("ABCD", [5, 40, 10, 70]):
        ws.column_dimensions[col].width = w

    ws = wb.create_sheet("Segment data")
    names_col = STATE["road_attr"][STATE["road_name_col"]].tolist() if STATE.get("road_name_col") else None
    lengths = STATE["road_len"]
    ws.append(["Road id", "Name", "Length (mi)"] + names)
    for c in ws[1]:
        c.font = Font(bold=True)
    df = d["df"]
    for pos, sid in enumerate(d["seg_ids"]):
        nm = names_col[sid] if names_col else None
        if isinstance(nm, float) and math.isnan(nm):
            nm = None
        vals = [None if not np.isfinite(x) else round(float(x), 4) for x in df.iloc[pos].to_numpy(dtype=float)]
        ws.append([int(sid), None if nm is None else str(nm), round(float(lengths[sid]), 3)] + vals)
    ws.freeze_panes = "D2"

    ws = wb.create_sheet("Notes", 0)
    for line in [["Spearman correlation matrix across road segments"],
                 ["Crash file", STATE.get("crash_name") or "-"], ["Road file", STATE.get("road_name") or "-"],
                 ["Segments used", f"{d['n_segments']:,} (segments shorter than {d['min_len']} mi left out: {d['n_dropped']:,})"],
                 [],
                 ["Variables", "one per column shown in the factor table (see the Variables sheet for how each is ranked)"],
                 ["Road column", "the segment's own value: a number, the leading number of the value, or 1/0 for a two-value column"],
                 ["Crash column", "average over the segment's focus crashes (two-value column: share of crashes with the '1' value); segments with crashes only"],
                 ["Unordered columns", "lighting, weather, vehicle type ... have no natural order and are left out unless you give one"],
                 ["Crashes / mi", "crashes on the segment / segment length (all years in the file together)"],
                 ["Spearman rho", "+1 = segments rank in the same order, 0 = no relation, -1 = opposite order"],
                 ["Redundancy", "two variables with |rho| >= 0.7 carry mostly the same information; use one as a factor"],
                 ["Segments used (pairs)", "each pair uses the segments where both variables have a value"]]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_segment_rates_xlsx(payload: dict) -> bytes:
    """One row per road segment: length, focus crashes and crashes / mi, then for every ticked
    condition the focus crashes on that segment that meet it and their crashes / mi.
    Crash attributes: crashes on the segment with that value (e.g. driver age 25-34).
    Road attributes: all focus crashes on the segment when the segment has the attribute, else blank."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    need_state("combined", "roads")
    comb: pd.DataFrame = STATE["combined"]
    attr: pd.DataFrame = STATE["road_attr"]
    lengths = STATE.get("road_len")
    n_roads = len(STATE["roads"])
    sets = payload.get("sets") or []
    if not any(st.get("conditions") for st in sets):
        raise ValueError("Tick at least one row in the table first.")
    universe = apply_value_filter(comb, payload.get("extra"))
    fcol = (payload.get("focus") or {}).get("column")
    names = attr[STATE["road_name_col"]].tolist() if STATE.get("road_name_col") else [None] * n_roads
    wb = Workbook()
    wb.remove(wb.active)
    used: set = set()
    head_fill = PatternFill("solid", fgColor="44546A")
    white = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="C5CCD4")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    for st in sets:
        conds = st.get("conditions") or []
        if not conds:
            continue
        fc = comb.loc[universe & apply_value_filter(comb, {"column": fcol, "values": st["types"]})]
        rid = fc["__road_id"].to_numpy(dtype=int)
        total = np.bincount(rid, minlength=n_roads)
        cols_counts, cols_apply = [], []
        for c in conds:
            col = c["column"]
            if col.startswith("road:"):
                road_m = condition_mask(attr[col[5:]], c)
                cols_counts.append(np.where(road_m, total, 0))
                cols_apply.append(road_m)
            else:
                m = condition_mask(fc[col], c)
                cols_counts.append(np.bincount(rid[m], minlength=n_roads) if m.any() else np.zeros(n_roads, dtype=int))
                cols_apply.append(np.ones(n_roads, dtype=bool))
        ws = wb.create_sheet(_sheet_title(st.get("name") or "Segments", used))
        head = ["Road id", "Name", "Length (mi)", "Focus crashes", "Focus crashes / mi"]
        for c in conds:
            lb = cond_label(c)
            head += [f"{lb} - crashes", f"{lb} - crashes / mi"]
        ws.append(head)
        for i in range(1, len(head) + 1):
            cell = ws.cell(1, i)
            cell.fill, cell.font, cell.border = head_fill, white, box
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[1].height = 45
        order = np.lexsort((np.arange(n_roads), -total))
        for r in order:
            mi = float(lengths[r]) if lengths is not None and np.isfinite(lengths[r]) else None
            nm = names[r]
            if isinstance(nm, float) and math.isnan(nm):
                nm = None
            row = [int(r), None if nm is None else str(nm), None if mi is None else round(mi, 3), int(total[r]),
                   None if not mi else round(total[r] / mi, 2)]
            for cnt, ap in zip(cols_counts, cols_apply):
                if not ap[r]:
                    row += [None, None]  # segment does not have this road attribute
                else:
                    row += [int(cnt[r]), None if not mi else round(cnt[r] / mi, 2)]
            ws.append(row)
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"
        for i, w in enumerate([8, 26, 9, 9, 10], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for i in range(6, len(head) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 14
    ws = wb.create_sheet("Notes", 0)
    for line in [["Segment crash rates"],
                 ["Crash file", STATE.get("crash_name") or "-"], ["Road file", STATE.get("road_name") or "-"],
                 [],
                 ["Length (mi)", "segment length from the road geometry"],
                 ["Focus crashes / mi", "focus crashes on the segment / segment length (all years in the file together)"],
                 ["Crash attribute", "crashes on the segment that have the value (e.g. 10 of 20 crashes with driver age 25-34) / length"],
                 ["Road attribute", "all focus crashes on the segment when the segment has the attribute; blank when it does not"],
                 ["Very short segments", "a few crashes on a very short segment give a very high crashes / mi - check Length"]]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _factor_text(q: dict) -> str:
    if q["op"] in ("eq", "rng", "age"):
        return str(q["value"])
    return {"ge": "\u2265 ", "le": "\u2264 "}[q["op"]] + str(q["value"])


# ----------------------------------------------------------------------------
# Step 4: scoring
# ----------------------------------------------------------------------------

def score_network(payload: dict) -> dict:
    """Systemic scoring of the whole network (FHWA systemic approach, as used by MnDOT, Iowa DOT,
    Boston MPO): crashes pick the risk factors, then the factors are applied to EVERY segment as road
    features - a segment scores whether or not it has crashes. Only road-attribute factors are scored;
    crash-condition factors (lighting at the crash, driver, mode ...) cannot be applied to a road
    without crashes, so they are returned as `crash_factors` (crash profile, not scored).

    payload["sets"] = [{"name", "types", "factors": [{column, op, value, weight}]}]. Each factor has a
    weight (default 1). Within a set, one column counts once: a segment gets the highest weight among
    the values of that column it meets. Score = sum over sets and columns; Max Score = the same sum
    of the highest weights. payload["hrn"] = {mode: "top" | "score", primary, secondary, within}:
    top = the highest-scoring primary% (and the next secondary%) of segments, ranked within each value
    of the `within` road column (facility type) or over the whole network; score = Max-normalised
    score >= primary (and >= secondary). Ties at a cut-off are all included."""
    need_state("combined", "roads")
    comb: pd.DataFrame = STATE["combined"]
    attr: pd.DataFrame = STATE["road_attr"]
    n_roads = len(STATE["roads"])
    focus = payload.get("focus") or {}
    fcol = focus.get("column")
    if fcol not in comb.columns:
        raise ValueError("Choose a crash type in Step 2.")
    sets = payload.get("sets")
    if not sets:
        sets = [{"name": "", "types": payload.get("crash_types") or focus.get("values") or [],
                 "factors": payload.get("factors") or []}]
    sets = [dict(st, types=[str(t) for t in st.get("types") or []]) for st in sets if st.get("factors") and st.get("types")]
    if not sets:
        raise ValueError("Select at least one factor in Step 3.")
    multi = len(sets) > 1

    types: list[str] = []
    for st in sets:
        types += [t for t in st["types"] if t not in types]
    universe = apply_value_filter(comb, payload.get("extra"))
    fc = comb.loc[universe & apply_value_filter(comb, {"column": fcol, "values": types})]
    type_series = fc[fcol].astype("string").str.strip()
    road_ids = fc["__road_id"].to_numpy(dtype=int)
    crash_total = np.bincount(road_ids, minlength=n_roads) if len(fc) else np.zeros(n_roads, dtype=int)
    type_counts = np.zeros((n_roads, len(types)), dtype=int)
    for j, t in enumerate(types):
        sel = (type_series == t).fillna(False).to_numpy(dtype=bool)
        if sel.any():
            type_counts[:, j] = np.bincount(road_ids[sel], minlength=n_roads)

    labels, masks, weights, groups, crash_factors = [], [], [], [], []
    for si, st in enumerate(sets):
        prefix = (f"{st.get('name') or ', '.join(st['types'])}: " if multi else "")
        for f in st["factors"]:
            col = f["column"]
            if not col.startswith("road:"):
                crash_factors.append(prefix + cond_label(f))
                continue
            name = col[5:]
            if name not in attr.columns:
                raise ValueError(f"Road column not found: {name}")
            try:
                w = float(f.get("weight", 1))
            except (TypeError, ValueError):
                w = 1.0
            if not math.isfinite(w) or w < 0:
                w = 0.0
            labels.append(prefix + cond_label(f))
            masks.append(np.asarray(condition_mask(attr[name], f), dtype=bool))
            weights.append(w)
            groups.append((si, col))
    K = len(labels)
    if K == 0:
        raise ValueError("None of the selected factors is a road attribute. The systemic map scores road "
                         "features only - go back to Step 3 and tick road factors (green 'road' tag).")

    met = np.column_stack(masks)
    # score: per (set, column) the highest weight met; max score: per (set, column) the highest weight
    gkeys = list(dict.fromkeys(groups))
    score = np.zeros(n_roads)
    max_score = 0.0
    for g in gkeys:
        idx = [i for i, x in enumerate(groups) if x == g]
        wv = np.array([weights[i] for i in idx])
        part = np.where(met[:, idx], wv[None, :], 0.0).max(axis=1)
        score += part
        max_score += float(wv.max()) if len(wv) else 0.0
    n_met = met.sum(axis=1)
    norm = score / max_score if max_score > 0 else np.zeros(n_roads)

    # HRN tiers
    hrn = payload.get("hrn") or {}
    mode = hrn.get("mode") or "top"
    within = hrn.get("within") or None
    lengths = STATE.get("road_len")
    if within and within in attr.columns:
        gv = attr[within].astype("string").str.strip().fillna("(blank)").replace("", "(blank)").to_numpy(dtype=object)
    else:
        within = None
        gv = np.full(n_roads, "All segments", dtype=object)
    tier = np.zeros(n_roads, dtype=int)
    rank_pct = np.full(n_roads, np.nan)
    group_info = []
    p1 = float(hrn.get("primary") if hrn.get("primary") not in (None, "") else (5 if mode == "top" else 0.75))
    p2 = float(hrn.get("secondary") if hrn.get("secondary") not in (None, "") else (10 if mode == "top" else 0.5))
    for g in pd.unique(gv):
        ids = np.flatnonzero(gv == g)
        sc = score[ids]
        n = len(ids)
        # share of the group's segments scoring at least this much (1 = top)
        srt = np.sort(sc)[::-1]
        ge = np.searchsorted(-srt, -sc, side="right")
        rank_pct[ids] = ge / n if n else np.nan
        if mode == "score":
            t1, t2 = p1 * max_score, p2 * max_score
        else:
            k1 = max(1, math.ceil(n * p1 / 100)) if p1 > 0 else 0
            k2 = max(1, math.ceil(n * (p1 + p2) / 100)) if p2 > 0 else k1
            t1 = srt[k1 - 1] if k1 else np.inf
            t2 = srt[k2 - 1] if k2 else np.inf
        is1 = (sc >= t1) & (sc > 0)
        is2 = ~is1 & (sc >= t2) & (sc > 0)
        tier[ids[is1]] = 2
        tier[ids[is2]] = 1
        mi = lambda m: float(np.nansum(lengths[ids[m]])) if lengths is not None else None
        group_info.append({"group": str(g), "segments": int(n), "miles": mi(np.ones(n, dtype=bool)),
                           "primary_min": float(sc[is1].min()) if is1.any() else None,
                           "secondary_min": float(sc[is2].min()) if is2.any() else None,
                           "primary": int(is1.sum()), "secondary": int(is2.sum()),
                           "primary_mi": mi(is1), "secondary_mi": mi(is2)})
    group_info.sort(key=lambda x: -x["segments"])

    rows = []
    for rid in range(n_roads):
        mi = float(lengths[rid]) if lengths is not None and np.isfinite(lengths[rid]) else None
        rows.append({
            "id": rid, "n": int(n_met[rid]), "s": round(float(score[rid]), 4), "z": round(float(norm[rid]), 4),
            "tier": int(tier[rid]), "rk": None if np.isnan(rank_pct[rid]) else round(float(rank_pct[rid]), 4),
            "g": str(gv[rid]), "c": int(crash_total[rid]), "f": np.flatnonzero(met[rid]).tolist(),
            "t": type_counts[rid].tolist(), "mi": mi, "cpm": (crash_total[rid] / mi) if mi else None,
        })
    tot_mi = lambda m: float(np.nansum(lengths[m])) if lengths is not None else None
    return clean({
        "types": types, "factors": labels, "weights": weights, "crash_factors": crash_factors,
        "max_score": max_score, "roads": rows, "within": within, "mode": mode, "primary": p1, "secondary": p2,
        "groups": group_info,
        "summary": {"segments": n_roads, "miles": tot_mi(np.ones(n_roads, dtype=bool)),
                    "primary": int((tier == 2).sum()), "secondary": int((tier == 1).sum()),
                    "primary_mi": tot_mi(tier == 2), "secondary_mi": tot_mi(tier == 1),
                    "with_factor": int((score > 0).sum()),
                    "focus_on_primary": int(crash_total[tier == 2].sum()), "focus_on_secondary": int(crash_total[tier == 1].sum())},
        "n_focus_crashes": int(len(fc)), "n_roads_with_crashes": int((crash_total > 0).sum()),
        "network_miles": tot_mi(np.ones(n_roads, dtype=bool)),
    })


def road_lengths_mi(gdf) -> np.ndarray | None:
    """Length of every road segment in miles (used for pN, the network share of a road factor)."""
    try:
        return (gdf.to_crs(gdf.estimate_utm_crs()).length / 1609.344).to_numpy(dtype=float)
    except Exception:
        traceback.print_exc()
        return None


# ----------------------------------------------------------------------------
# Step 4: report (Word) and Excel export, following the Mountains HRN risk factor tables
# ----------------------------------------------------------------------------

def based_on(pF: float, pN, pC: float) -> str:
    """Network = more common than the share of road miles with the factor (pF > pN);
    Tot Crash = more common than among all crashes (pF > pC); Both = both."""
    net = pN is not None and pF > pN
    tot = pF > pC
    return "Both" if (net and tot) else "Network" if net else "Tot Crash" if tot else "Neither"


POINTS = {"Both": 2, "Network": 1, "Tot Crash": 1, "Neither": 0}


def report_data(payload: dict) -> dict:
    """Factor tables per crash type (NF, pF, pN, pC, Based On, points, Max Score) and the
    per-segment scores, for the report and Excel exports."""
    need_state("combined", "roads")
    comb: pd.DataFrame = STATE["combined"]
    attr: pd.DataFrame = STATE["road_attr"]
    lengths = STATE.get("road_len")
    total_len = float(np.nansum(lengths)) if lengths is not None else 0.0
    score = score_network(payload)
    focus = payload.get("focus") or {}
    fcol = focus.get("column")
    sets = payload.get("sets") or [{"name": "", "types": payload.get("crash_types") or focus.get("values") or [],
                                    "factors": payload.get("factors") or []}]
    sets = [dict(st, types=[str(t) for t in st.get("types") or []]) for st in sets if st.get("factors") and st.get("types")]
    universe = apply_value_filter(comb, payload.get("extra"))
    u = comb.loc[universe].reset_index(drop=True)
    N = len(u)
    type_txt = u[fcol].astype("string").str.strip()

    bopt = payload.get("binom") or {}
    alpha = float(bopt.get("alpha") or 0.05)
    min_n = max(0, int(bopt.get("min_n") or 0))
    min_ratio = float(bopt.get("min_ratio") or 0)
    lab2w = dict(zip(score["factors"], score["weights"]))
    multi = len(sets) > 1
    out_sets, offset = [], 0
    for st in sets:
        in_set = type_txt.isin(st["types"]).fillna(False).to_numpy(dtype=bool)
        F = int(in_set.sum())
        rows = []
        for k, f in enumerate(st["factors"]):
            col = f["column"]
            is_road = col.startswith("road:")
            if is_road:
                name = col[5:]
                if name not in attr.columns:
                    raise ValueError(f"Road column not found: {name}")
                m_all = condition_mask(u[col], f) if col in u.columns else np.zeros(N, dtype=bool)
                road_m = condition_mask(attr[name], f)
                pN = float(np.nansum(lengths[road_m]) / total_len) if (lengths is not None and total_len > 0) else None
                miles = float(np.nansum(lengths[road_m])) if lengths is not None else None
            else:
                m_all = condition_mask(u[col], f)
                pN, miles = None, None
            n_all = int(m_all.sum())
            NF = int((m_all & in_set).sum())
            pF = NF / F if F else 0.0
            pC = n_all / N if N else 0.0
            st_ = stat_block(n_all, NF, N, F)
            # Based On with the exact one-sided binomial test (same rules as the Column check page)
            p_net = binom_sf(NF, F, pN) if (pN is not None and F) else None
            p_tot = binom_sf(NF, F, pC) if F else None
            r_net = (pF / pN) if pN else None
            r_tot = (pF / pC) if pC else None
            enough = NF >= min_n
            net = enough and pN is not None and pF > pN and p_net is not None and p_net < alpha and (r_net or 0) >= min_ratio
            tot = enough and pF > pC and p_tot is not None and p_tot < alpha and (r_tot or 0) >= min_ratio
            bo = "Both" if (net and tot) else "Network" if net else "Tot Crash" if tot else "Neither"
            lab = ((f"{st.get('name') or ', '.join(st['types'])}: " if multi else "") + cond_label(f))
            w = lab2w.get(lab) if is_road else None
            rows.append({"idx": offset + k, "column": col, "category": pretty_col(col), "factor": _factor_text(f),
                         "label": cond_label(f), "source": "road" if is_road else "crash",
                         "based_on": bo, "points": POINTS[bo], "weight": w, "NF": NF, "pF": pF, "pN": pN, "pC": pC,
                         "p_net": p_net, "p_tot": p_tot, "r_net": r_net, "r_tot": r_tot,
                         "miles": miles, "n_all": n_all, "lift": st_["lift"], "prob": st_["prob"]})
        # Max Score: one value per column counts (the highest weight in each column); road factors only
        best: dict[str, float] = {}
        for r in rows:
            if r["weight"] is not None:
                best[r["column"]] = max(best.get(r["column"], 0), r["weight"])
        out_sets.append({"name": st.get("name") or ", ".join(st["types"]), "types": st["types"], "F": F,
                         "rows": rows, "max_score": float(sum(best.values())), "start": offset})
        offset += len(st["factors"])

    names = STATE["road_attr"][STATE["road_name_col"]].tolist() if STATE.get("road_name_col") else None
    TIER = {2: "Primary", 1: "Secondary", 0: ""}
    segs = []
    for r in score["roads"]:
        nm = names[r["id"]] if names else None
        if nm is not None and isinstance(nm, float) and math.isnan(nm):
            nm = None
        segs.append({"id": r["id"], "name": None if nm is None else str(nm), "n": r["n"], "points": r["s"],
                     "norm": r["z"], "tier": TIER[r["tier"]], "rank_pct": r["rk"], "group": r["g"],
                     "crashes": r["c"], "type_counts": r["t"], "miles": r.get("mi"), "cpm": r.get("cpm"),
                     "factors": [score["factors"][i] for i in r["f"]]})
    segs.sort(key=lambda x: (-x["points"], -x["n"], -x["crashes"]))
    return {"sets": out_sets, "segments": segs, "types": score["types"], "factor_labels": score["factors"],
            "crash_factors": score["crash_factors"], "hrn": {k: score[k] for k in ("mode", "primary", "secondary", "within", "groups", "summary")},
            "binom": {"alpha": alpha, "min_n": min_n, "min_ratio": min_ratio},
            "N": N, "network_miles": total_len if lengths is not None else None, "n_roads": len(STATE["roads"]),
            "n_focus_crashes": score["n_focus_crashes"], "max_total": score["max_score"]}


def _pct(v) -> str:
    return "" if v is None else f"{round(100 * v)}%"


REPORT_NOTES = [
    ("Approach", "systemic (FHWA): crashes are used to choose the risk factors; the road-attribute factors are then "
                 "applied to every segment of the network, whether or not it has crashes"),
    ("NF", "number of crashes of the type that have the factor"),
    ("pF", "share of the crash type that has the factor (NF / crashes of the type)"),
    ("pN", "share of the road network (miles) that has the factor; road factors only"),
    ("pC", "share of all crashes that have the factor"),
    ("Based On", "exact one-sided binomial test: Network = pF significantly above pN, Tot Crash = pF significantly "
                 "above pC, Both = both, Neither = neither"),
    ("Weight", "points a segment gets for the factor (set in Step 4; default 1). Within a crash type one value per "
               "column counts (the highest weight met)"),
    ("Max Score", "highest score a segment can reach: the highest weight in each column, added up"),
    ("Crash factors", "conditions of the crash (lighting, driver, vehicle ...) cannot be applied to roads without "
                      "crashes, so they are listed as the crash profile and not scored"),
    ("HRN", "Primary / Secondary: the highest-scoring segments (top % or score cut-off, see Info); ties at the "
            "cut-off are included"),
]


def _hrn_rule(h: dict) -> str:
    within = f", ranked within {pretty_col('road:' + h['within'])}" if h.get("within") else ", ranked over the whole network"
    if h.get("mode") == "score":
        return (f"Primary = score >= {round(100 * h['primary'])}% of Max Score, "
                f"Secondary = score >= {round(100 * h['secondary'])}% of Max Score{within}")
    return f"Primary = top {h['primary']:g}% of segments by score, Secondary = next {h['secondary']:g}%{within}"


def export_report_xlsx(payload: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    d = report_data(payload)
    h = d["hrn"]
    title = payload.get("title") or "HRN Risk Factors"
    head_fill = PatternFill("solid", fgColor="44546A")
    white = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="C5CCD4")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    tier_fill = {"Primary": PatternFill("solid", fgColor="F4C7C3"), "Secondary": PatternFill("solid", fgColor="FCE5CD")}
    wb = Workbook()

    def header(ws, cols, row=1):
        for i, t in enumerate(cols, start=1):
            c = ws.cell(row, i, t)
            c.fill, c.font, c.border = head_fill, white, box
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws = wb.active
    ws.title = "Risk factors"
    cols = ["Crash type", "Category", "Factor", "Source", "Based On", "Weight", "NF", "pF", "pN", "pC",
            "pF / pN", "p (vs pN)", "pF / pC", "p (vs pC)", "Road miles"]
    header(ws, cols)
    for st in d["sets"]:
        for r in st["rows"]:
            ws.append([st["name"], r["category"], r["factor"], r["source"], r["based_on"],
                       r["weight"] if r["weight"] is not None else "not scored", r["NF"],
                       r["pF"], r["pN"], r["pC"], None if r["r_net"] is None else round(r["r_net"], 2), r["p_net"],
                       None if r["r_tot"] is None else round(r["r_tot"], 2), r["p_tot"],
                       None if r["miles"] is None else round(r["miles"], 1)])
            for i in range(1, len(cols) + 1):
                ws.cell(ws.max_row, i).border = box
            for i in (8, 9, 10):
                ws.cell(ws.max_row, i).number_format = "0.0%"
            for i in (12, 14):
                ws.cell(ws.max_row, i).number_format = "0.0000"
        ws.append([st["name"], "Max Score", None, None, None, round(st["max_score"], 2)])
        for i in range(1, len(cols) + 1):
            ws.cell(ws.max_row, i).font = Font(bold=True)
            ws.cell(ws.max_row, i).fill = PatternFill("solid", fgColor="D9DEE5")
            ws.cell(ws.max_row, i).border = box
    for i, w in enumerate([18, 22, 26, 8, 10, 10, 7, 8, 8, 8, 8, 10, 8, 10, 10], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    ws = wb.create_sheet("Segments")
    cols = (["Rank", "Road id", "Name", "Length (mi)", "HRN", f"Score (of {d['max_total']:g})", "Score / Max",
             "Top % (in group)", "Group", "Factors met", "Focus crashes", "Crashes / mi"]
            + [f"{t} crashes" for t in d["types"]] + ["Factors met (list)"])
    header(ws, cols)
    for i, sg in enumerate(d["segments"], start=1):
        mi = sg["miles"]
        ws.append([i, sg["id"], sg["name"], None if mi is None else round(mi, 3), sg["tier"], sg["points"],
                   sg["norm"], sg["rank_pct"], sg["group"], sg["n"], sg["crashes"],
                   None if not mi else round(sg["crashes"] / mi, 2)] + list(sg["type_counts"]) + ["; ".join(sg["factors"])])
        ws.cell(ws.max_row, 7).number_format = "0%"
        ws.cell(ws.max_row, 8).number_format = "0.0%"
        if sg["tier"] in tier_fill:
            ws.cell(ws.max_row, 5).fill = tier_fill[sg["tier"]]
    for i, w in enumerate([6, 9, 28, 9, 10, 10, 9, 10, 16, 8, 9, 10], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.column_dimensions[get_column_letter(len(cols))].width = 80
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(2, ws.max_row)}"

    ws = wb.create_sheet("HRN summary")
    header(ws, ["Group", "Segments", "Miles", "Primary segments", "Primary miles", "Primary min. score",
                "Secondary segments", "Secondary miles", "Secondary min. score"])
    for g in h["groups"]:
        ws.append([g["group"], g["segments"], None if g["miles"] is None else round(g["miles"], 1),
                   g["primary"], None if g["primary_mi"] is None else round(g["primary_mi"], 1), g["primary_min"],
                   g["secondary"], None if g["secondary_mi"] is None else round(g["secondary_mi"], 1), g["secondary_min"]])
    sm = h["summary"]
    ws.append(["Total", sm["segments"], None if sm["miles"] is None else round(sm["miles"], 1), sm["primary"],
               None if sm["primary_mi"] is None else round(sm["primary_mi"], 1), None, sm["secondary"],
               None if sm["secondary_mi"] is None else round(sm["secondary_mi"], 1), None])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    ws.append([])
    header(ws, ["Score", "Segments", "Miles", "Focus crashes", "Crashes / mi"], row=ws.max_row + 1)
    agg: dict = {}
    for sg in d["segments"]:
        a_ = agg.setdefault(sg["points"], [0, 0.0, 0])
        a_[0] += 1
        a_[1] += sg["miles"] or 0
        a_[2] += sg["crashes"]
    for k in sorted(agg, reverse=True):
        ws.append([k, agg[k][0], round(agg[k][1], 1), agg[k][2], round(agg[k][2] / agg[k][1], 2) if agg[k][1] else None])
    for i, w in enumerate([22, 10, 10, 10, 10, 12, 12, 12, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    if d["crash_factors"]:
        ws = wb.create_sheet("Crash profile")
        header(ws, ["Crash factor (not scored)"])
        for lb in d["crash_factors"]:
            ws.append([lb])
        ws.column_dimensions["A"].width = 70

    ws = wb.create_sheet("Info", 0)
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    meta = payload.get("meta") or {}
    b = d["binom"]
    for k, v in [("Crash file", STATE.get("crash_name")), ("Road file", STATE.get("road_name")),
                 ("Crash-type column", meta.get("focus_column")), ("Restricted to", meta.get("extra")),
                 ("Crashes analysed", d["N"]), ("Road segments", d["n_roads"]),
                 ("Network miles", None if d["network_miles"] is None else round(d["network_miles"], 1)),
                 ("Binomial test", f"alpha {b['alpha']}, min. crashes {b['min_n']}, min. ratio {b['min_ratio'] or 'none'}"),
                 ("HRN rule", _hrn_rule(h)),
                 ("HRN result", f"Primary {h['summary']['primary']:,} segments"
                                + (f" ({h['summary']['primary_mi']:,.1f} mi)" if h['summary']['primary_mi'] is not None else "")
                                + f", Secondary {h['summary']['secondary']:,} segments"
                                + (f" ({h['summary']['secondary_mi']:,.1f} mi)" if h['summary']['secondary_mi'] is not None else ""))]:
        ws.append([k, v if v not in (None, "") else "-"])
    ws.append([])
    for k, v in REPORT_NOTES:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_report_docx(payload: dict) -> bytes:
    try:
        from docx import Document
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Pt, RGBColor, Inches
    except ImportError:
        raise ValueError("The Word report needs the python-docx package: pip install python-docx")
    d = report_data(payload)
    h = d["hrn"]
    title = payload.get("title") or "HRN Risk Factors"

    def shade(cell, hex_color):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tcPr.append(shd)

    def fix_widths(table, widths):
        """Column widths that both Word and LibreOffice respect (grid + every cell, no autofit)."""
        table.autofit = False
        grid = table._tbl.tblGrid
        for i, gc in enumerate(grid.findall(qn("w:gridCol"))):
            if i < len(widths):
                gc.set(qn("w:w"), str(int(widths[i].twips)))
        for row in table.rows:
            for i, c in enumerate(row._tr.tc_lst):
                tcPr = c.get_or_add_tcPr()
                span = tcPr.find(qn("w:gridSpan"))
                n = int(span.get(qn("w:val"))) if span is not None else 1
                w = sum(int(x.twips) for x in widths[i:i + n]) if i < len(widths) else None
                if w:
                    tcW = tcPr.find(qn("w:tcW"))
                    if tcW is None:
                        tcW = OxmlElement("w:tcW")
                        tcPr.append(tcW)
                    tcW.set(qn("w:w"), str(w))
                    tcW.set(qn("w:type"), "dxa")

    def set_text(cell, text, bold=False, color=None, align=None, size=9):
        cell.text = ""
        p = cell.paragraphs[0]
        run = p.add_run("" if text is None else str(text))
        run.bold = bold
        run.font.size = Pt(size)
        if color:
            run.font.color.rgb = RGBColor.from_string(color)
        if align:
            p.alignment = align
        p.paragraph_format.space_after = Pt(0)

    num = lambda v: f"{v:g}" if isinstance(v, (int, float)) else str(v)
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.8)
    st_ = doc.styles["Normal"]
    st_.font.name = "Calibri"
    st_.font.size = Pt(10)
    doc.add_heading(title, level=1)
    meta = payload.get("meta") or {}
    sm = h["summary"]
    doc.add_paragraph(
        f"Crashes analysed: {d['N']:,}" + (f" ({meta.get('extra')})" if meta.get("extra") else "")
        + f". Road network: {d['n_roads']:,} segments"
        + (f", {d['network_miles']:,.0f} miles" if d["network_miles"] else "") + ". "
        "Systemic approach: the crash data were used to choose the risk factors, and the road-attribute "
        "factors were then applied to every segment of the network, with or without crashes.")
    doc.add_paragraph(
        f"High-risk network ({_hrn_rule(h)}): Primary {sm['primary']:,} segments"
        + (f" ({sm['primary_mi']:,.1f} mi)" if sm["primary_mi"] is not None else "")
        + f", Secondary {sm['secondary']:,} segments"
        + (f" ({sm['secondary_mi']:,.1f} mi)" if sm["secondary_mi"] is not None else "")
        + f". {sm['focus_on_primary']:,} of {d['n_focus_crashes']:,} focus crashes occurred on Primary segments.")

    center = WD_ALIGN_PARAGRAPH.CENTER
    for st in d["sets"]:
        t = doc.add_table(rows=2, cols=7)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        top = t.rows[0].cells
        top[0].merge(top[6])
        set_text(t.rows[0].cells[0], f"{st['name']} Crashes", bold=True, color="FFFFFF", align=center, size=10)
        shade(t.rows[0].cells[0], "1F3864")
        for i, hd in enumerate(["Segment Risk Factor", "Based On", "Weight", "NF", "pF", "pN", "pC"]):
            set_text(t.rows[1].cells[i], hd, bold=True, align=center if i else None)
            shade(t.rows[1].cells[i], "D9E2F3")
        rows = sorted([r for r in st["rows"] if r["source"] == "road"], key=lambda r: (r["category"].lower(), r["factor"]))
        if not rows:
            set_text(t.add_row().cells[0], "None")
        for r in rows:
            cells = t.add_row().cells
            set_text(cells[0], r["label"])
            set_text(cells[1], r["based_on"], align=center)
            set_text(cells[2], num(r["weight"]), align=center)
            set_text(cells[3], r["NF"], align=center)
            set_text(cells[4], _pct(r["pF"]), align=center)
            set_text(cells[5], _pct(r["pN"]) if r["pN"] is not None else "-", align=center)
            set_text(cells[6], _pct(r["pC"]), align=center)
        last = t.add_row().cells
        last[0].merge(last[6])
        set_text(t.rows[-1].cells[0], f"Max Score: {num(st['max_score'])}", bold=True, align=center)
        fix_widths(t, [Inches(2.9), Inches(0.9), Inches(0.6), Inches(0.6), Inches(0.6), Inches(0.6), Inches(0.6)])
        crash_rows = [r for r in st["rows"] if r["source"] != "road"]
        if crash_rows:
            p = doc.add_paragraph()
            p.add_run("Crash profile (not scored): ").bold = True
            p.add_run("; ".join(f"{r['label']} ({r['based_on']})" for r in crash_rows))
        doc.add_paragraph()

    if h.get("within") and len(h["groups"]) > 1:
        doc.add_heading("High-risk network by group", level=2)
        t = doc.add_table(rows=1, cols=5)
        t.style = "Table Grid"
        for i, hd in enumerate([pretty_col("road:" + h["within"]), "Segments", "Miles", "Primary (mi)", "Secondary (mi)"]):
            set_text(t.rows[0].cells[i], hd, bold=True, align=center)
            shade(t.rows[0].cells[i], "D9E2F3")
        for g in h["groups"]:
            c = t.add_row().cells
            set_text(c[0], g["group"])
            set_text(c[1], f"{g['segments']:,}", align=center)
            set_text(c[2], "" if g["miles"] is None else f"{g['miles']:,.1f}", align=center)
            set_text(c[3], f"{g['primary']:,}" + ("" if g["primary_mi"] is None else f" ({g['primary_mi']:,.1f})"), align=center)
            set_text(c[4], f"{g['secondary']:,}" + ("" if g["secondary_mi"] is None else f" ({g['secondary_mi']:,.1f})"), align=center)
        fix_widths(t, [Inches(2.2), Inches(1.0), Inches(1.0), Inches(1.3), Inches(1.3)])
        doc.add_paragraph()

    doc.add_heading("Highest scoring segments", level=2)
    top = d["segments"][: int(payload.get("top_n") or 20)]
    t = doc.add_table(rows=1, cols=7)
    t.style = "Table Grid"
    for i, hd in enumerate(["Rank", "Segment", "Length (mi)", "HRN", f"Score (of {num(d['max_total'])})", "Factors met", "Focus crashes"]):
        set_text(t.rows[0].cells[i], hd, bold=True, align=center)
        shade(t.rows[0].cells[i], "D9E2F3")
    for i, sg in enumerate(top, start=1):
        c = t.add_row().cells
        set_text(c[0], i, align=center)
        set_text(c[1], (sg["name"] or "Segment") + f" (id {sg['id']})")
        set_text(c[2], "" if sg["miles"] is None else (f"{sg['miles']:.3f}" if sg["miles"] < 0.1 else f"{sg['miles']:.2f}"), align=center)
        set_text(c[3], sg["tier"] or "-", align=center)
        set_text(c[4], num(sg["points"]), align=center)
        set_text(c[5], sg["n"], align=center)
        set_text(c[6], sg["crashes"], align=center)
    if not top:
        set_text(t.add_row().cells[0], "No segments.")
    fix_widths(t, [Inches(0.5), Inches(2.6), Inches(0.75), Inches(0.8), Inches(0.9), Inches(0.75), Inches(0.75)])
    doc.add_paragraph()

    doc.add_heading("Notes", level=2)
    for k, v in REPORT_NOTES:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(k + " - ").bold = True
        p.add_run(v)
    b = d["binom"]
    doc.add_paragraph(f"Crash file: {STATE.get('crash_name') or '-'}. Road file: {STATE.get('road_name') or '-'}. "
                      f"Binomial test: alpha {b['alpha']}, min. crashes {b['min_n']}, min. ratio {b['min_ratio'] or 'none'}.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def road_detail(rid: int) -> dict:
    need_state("roads")
    attr = STATE["road_attr"]
    if not (0 <= rid < len(attr)):
        raise ValueError("Unknown road.")
    row = attr.iloc[rid]
    items = []
    for k, v in row.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        items.append([str(k), str(v)])
    n_all = 0
    comb = STATE["combined"]
    if comb is not None:
        n_all = int((comb["__road_id"] == rid).sum())
    return clean({"id": rid, "attributes": items[:25], "all_crashes": n_all})


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

def unexpected(exc: Exception) -> str:
    """Print the full traceback in the console window and name the failing line on the page."""
    traceback.print_exc()
    where = ""
    for fr in reversed(traceback.extract_tb(exc.__traceback__)):
        if Path(fr.filename).name == "app.py":
            where = f" [app.py line {fr.lineno}, in {fr.name}: {(fr.line or '').strip()[:120]}]"
            break
    return f"Unexpected error: {type(exc).__name__}: {exc}{where}"


class Handler(BaseHTTPRequestHandler):
    server_version = "HRNTool/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, body: bytes, ctype="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        self._send(json.dumps(clean(obj), separators=(",", ":")).encode("utf-8"), status=status)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD_BYTES:
            raise ValueError("File is too large.")
        return self.rfile.read(n)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                self._send((APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/health":
                self.send_json({"app": "HRN Tool simple", "ok": True})
            elif url.path == "/api/geometry":
                need_state("roads")
                self._send(STATE["road_geojson"].encode("utf-8"), "application/json")
            elif url.path == "/api/crash-points":
                need_state("combined")
                self.send_json(STATE["points"])
            elif url.path == "/api/focus-columns":
                self.send_json(focus_columns())
            elif url.path == "/api/road":
                rid = int(parse_qs(url.query).get("id", ["-1"])[0])
                self.send_json(road_detail(rid))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": unexpected(exc)}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/upload":
                kind = parse_qs(url.query).get("kind", [""])[0]
                filename = unquote(self.headers.get("X-Filename", "upload"))
                data = self._body()
                with LOCK:
                    self.send_json(handle_upload(kind, filename, data))
                return
            payload = json.loads(self._body().decode("utf-8") or "{}")
            with LOCK:
                if url.path == "/api/build-map":
                    self.send_json(build_map(payload))
                elif url.path == "/api/values":
                    self.send_json(column_values(payload.get("column"), payload.get("filter")))
                elif url.path == "/api/scan":
                    self.send_json(scan_factors(payload))
                elif url.path == "/api/report-docx":
                    self._send(export_report_docx(payload),
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                elif url.path == "/api/report-xlsx":
                    self._send(export_report_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/column-check":
                    self.send_json(column_check(payload))
                elif url.path == "/api/compare":
                    self.send_json(compare_methods(payload))
                elif url.path == "/api/compare-xlsx":
                    self._send(export_compare_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/column-check-xlsx":
                    self._send(export_column_check_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/cramers":
                    self.send_json(cramers_json(payload))
                elif url.path == "/api/crosstab":
                    self.send_json(crosstab_json(payload))
                elif url.path == "/api/cramers-xlsx":
                    self._send(export_cramers_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/correlation":
                    self.send_json(correlation_json(payload))
                elif url.path == "/api/correlation-xlsx":
                    self._send(export_correlation_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/segment-rates":
                    self._send(export_segment_rates_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/scan-export":
                    self._send(export_scan_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/score":
                    self.send_json(score_network(payload))
                elif url.path == "/api/basis":
                    # Based On (binomial test) of every selected factor, for the weights panel
                    d = report_data(payload)
                    self.send_json(clean({"sets": [{"name": st["name"], "rows": [
                        {"column": r["column"], "label": r["label"], "source": r["source"], "based_on": r["based_on"],
                         "NF": r["NF"], "r_net": r["r_net"], "r_tot": r["r_tot"]} for r in st["rows"]]} for st in d["sets"]]}))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": unexpected(exc)}, 500)


def handle_upload(kind: str, filename: str, data: bytes) -> dict:
    if kind == "crash":
        df = read_crash_file(data, filename)
        STATE.update(crashes=df, crash_name=filename, combined=None, points=None, map_stats=None)
        return {"name": filename, "rows": int(len(df)), "columns": column_summary(df), "sheet": LAST_SHEET_NOTE["text"],
                "merged": MERGE_NOTE["crash"],
                "latlon": safe_latlon_guess(df)}
    if kind == "road":
        gdf, attr = read_road_file(data, filename)
        name_col = guess_name_column(list(attr.columns))
        STATE.update(roads=gdf, road_attr=attr, road_name=filename, road_name_col=name_col, road_len=road_lengths_mi(gdf),
                     road_geojson=build_road_geojson(gdf, name_col), combined=None, points=None, map_stats=None)
        b = gdf.total_bounds
        return {"name": filename, "rows": int(len(gdf)), "columns": [c for c in attr.columns], "merged": MERGE_NOTE["road"],
                "name_column": name_col, "bounds": [[float(b[1]), float(b[0])], [float(b[3]), float(b[2])]]}
    raise ValueError("Unknown upload type.")


def main():
    host = os.environ.get("HRN_HOST", "127.0.0.1")
    port = int(os.environ.get("HRN_PORT", "8770"))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"HRN Tool is running at {url}  (Ctrl+C to stop)")
    if os.environ.get("HRN_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
