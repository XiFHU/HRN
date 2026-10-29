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
        out[name] = pd.Series(vals, dtype=object)
    t = pd.DataFrame(out)
    if t.empty or t.shape[1] == 0:
        return t
    keep = [any(v is not None for v in row) for row in t.itertuples(index=False)]
    return t.loc[keep].reset_index(drop=True)


LAST_SHEET_NOTE = {"text": ""}


def read_crash_file(data: bytes, filename: str) -> pd.DataFrame:
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
    a, x = k + 1.0, float(lam)
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

    results, skipped = [], []
    for col in u.columns:
        if col.startswith("__") or col in skip_cols:
            continue
        if is_excluded_name(col):
            skipped.append({"column": pretty_col(col), "reason": "identifier / date / location field"})
            continue
        conds, info = scan_column(col, u[col], fm, N, F, min_focus, max_levels, ctx)
        if conds is None:
            skipped.append({"column": pretty_col(col), "reason": info})
            continue
        if not conds:
            skipped.append({"column": pretty_col(col), "reason": "no over-represented value with enough crashes"})
            continue
        for c in conds:
            c["label"] = cond_label(c)
        over = [c for c in conds if c["over"]]
        results.append({
            "column": col, "label": pretty_col(col), "source": "road" if col.startswith("road:") else "crash",
            "kind": info, "best_excess": over[0]["excess"] if over else 0, "has_over": bool(over),
            "conditions": conds,
        })
    results.sort(key=lambda r: (not r["has_over"], -r["best_excess"]))
    all_row = {"n_all": N, "n_focus": F}
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
        groups += [("Total", 2), (name, 6)]
        if G2:
            groups.append((g2_name, 4))
        subs = ["", "", ""] + list(B) + ["#", "%", "#", "%", "Prob.", "Expected", "x normal", "Extra"]
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
        row = ["All", "", ""] + list(A.get("by") or []) + [A["n_all"], 1, A["n_focus"], 1, None, None, None, None]
        row += ([A.get("n_g2"), 1, None, None] if G2 else []) + [None]
        ws.append(row)
        for i in range(1, len(subs) + 1):
            ws.cell(3, i).fill = all_fill
            ws.cell(3, i).font = Font(bold=True)
            ws.cell(3, i).border = box

        nB = len(B)
        pct_cols = [3 + nB + 2, 3 + nB + 4, 3 + nB + 5]
        if G2:
            pct_cols += [3 + nB + 10, 3 + nB + 12]
        for pc in pct_cols:
            ws.cell(3, pc).number_format = "0%"
        for nc in list(range(4, 4 + nB)) + [3 + nB + 1, 3 + nB + 3] + ([3 + nB + 9] if G2 else []):
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
                         round(q["lift"], 2) if q["lift"] is not None else None, round(q["excess"], 1)]
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
                for pc in [3 + nB + 5] + ([3 + nB + 12] if G2 else []):
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


def _factor_text(q: dict) -> str:
    if q["op"] in ("eq", "rng", "age"):
        return str(q["value"])
    return {"ge": "\u2265 ", "le": "\u2264 "}[q["op"]] + str(q["value"])


# ----------------------------------------------------------------------------
# Step 4: scoring
# ----------------------------------------------------------------------------

def score_network(payload: dict) -> dict:
    """Per-road factor counts.

    payload["sets"] = [{"name", "types": [...], "factors": [...]}, ...]: each set is scored with
    the crashes of its own types (e.g. Rear End uses only rear-end crashes for its crash-based
    factors). A road's score is the number of factors met over all sets. The older single-set
    form (crash_types + factors) is still accepted."""
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
    min_crashes = max(1, int(payload.get("min_crashes") or 1))
    include_empty = bool(payload.get("include_empty"))
    multi = len(sets) > 1

    types: list[str] = []
    for st in sets:
        types += [t for t in st["types"] if t not in types]
    universe = apply_value_filter(comb, payload.get("extra"))
    fc = comb.loc[universe & apply_value_filter(comb, {"column": fcol, "values": types})]
    type_series = fc[fcol].astype("string").str.strip()
    road_ids = fc["__road_id"].to_numpy(dtype=int)

    crash_total = np.bincount(road_ids, minlength=n_roads) if len(fc) else np.zeros(n_roads, dtype=int)
    type_sel = {}
    type_counts = np.zeros((n_roads, len(types)), dtype=int)
    for j, t in enumerate(types):
        sel = (type_series == t).fillna(False).to_numpy(dtype=bool)
        type_sel[t] = sel
        if sel.any():
            type_counts[:, j] = np.bincount(road_ids[sel], minlength=n_roads)

    labels, cols = [], []
    for st in sets:
        in_set = np.zeros(len(fc), dtype=bool)
        for t in st["types"]:
            in_set |= type_sel.get(t, np.zeros(len(fc), dtype=bool))
        for f in st["factors"]:
            col = f["column"]
            if col.startswith("road:"):
                name = col[5:]
                if name not in attr.columns:
                    raise ValueError(f"Road column not found: {name}")
                cols.append(condition_mask(attr[name], f))
            else:
                if col not in fc.columns:
                    raise ValueError(f"Crash column not found: {col}")
                m = condition_mask(fc[col], f) & in_set
                cnt = np.bincount(road_ids[m], minlength=n_roads) if m.any() else np.zeros(n_roads, dtype=int)
                cols.append(cnt >= min_crashes)
            labels.append((f"{st.get('name') or ', '.join(st['types'])}: " if multi else "") + cond_label(f))
    met = np.column_stack(cols) if cols else np.zeros((n_roads, 0), dtype=bool)
    n_met = met.sum(axis=1)
    K = len(labels)

    include = (crash_total > 0) | (include_empty & (n_met > 0))
    ids = np.flatnonzero(include)
    rows = []
    for rid in ids:
        rows.append({
            "id": int(rid), "n": int(n_met[rid]), "c": int(crash_total[rid]),
            "f": np.flatnonzero(met[rid]).tolist(), "t": type_counts[rid].tolist(),
        })
    dist = np.bincount(n_met[ids], minlength=K + 1).tolist() if len(ids) else [0] * (K + 1)
    return clean({
        "types": types, "factors": labels,
        "roads": rows, "distribution": dist,
        "n_focus_crashes": int(len(fc)), "n_roads_with_crashes": int((crash_total > 0).sum()),
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
            bo = based_on(pF, pN, pC)
            rows.append({"idx": offset + k, "column": col, "category": pretty_col(col), "factor": _factor_text(f),
                         "label": cond_label(f), "source": "road" if is_road else "crash",
                         "based_on": bo, "points": POINTS[bo], "NF": NF, "pF": pF, "pN": pN, "pC": pC,
                         "miles": miles, "n_all": n_all, "lift": st_["lift"], "prob": st_["prob"]})
        # Max Score: alternatives of the same category count once (best points in each category)
        best: dict[str, int] = {}
        for r in rows:
            best[r["column"]] = max(best.get(r["column"], 0), r["points"])
        out_sets.append({"name": st.get("name") or ", ".join(st["types"]), "types": st["types"], "F": F,
                         "rows": rows, "max_score": int(sum(best.values())), "start": offset})
        offset += len(st["factors"])

    # per-segment points: best points per category within each crash type, added up
    by_idx = {r["idx"]: (si, r) for si, s_ in enumerate(out_sets) for r in s_["rows"]}
    names = STATE["road_attr"][STATE["road_name_col"]].tolist() if STATE.get("road_name_col") else None
    segs = []
    for r in score["roads"]:
        per_set = [dict() for _ in out_sets]
        for i in r["f"]:
            si, row = by_idx[i]
            per_set[si][row["column"]] = max(per_set[si].get(row["column"], 0), row["points"])
        pts = [int(sum(d.values())) for d in per_set]
        nm = names[r["id"]] if names else None
        if nm is not None and isinstance(nm, float) and math.isnan(nm):
            nm = None
        segs.append({"id": r["id"], "name": None if nm is None else str(nm), "n": r["n"], "points": int(sum(pts)),
                     "set_points": pts, "crashes": r["c"], "type_counts": r["t"],
                     "miles": float(lengths[r["id"]]) if lengths is not None else None,
                     "factors": [score["factors"][i] for i in r["f"]]})
    segs.sort(key=lambda x: (-x["points"], -x["n"], -x["crashes"]))
    return {"sets": out_sets, "segments": segs, "types": score["types"], "factor_labels": score["factors"],
            "N": N, "network_miles": total_len if lengths is not None else None, "n_roads": len(STATE["roads"]),
            "n_focus_crashes": score["n_focus_crashes"], "max_total": int(sum(s_["max_score"] for s_ in out_sets))}


def _pct(v) -> str:
    return "" if v is None else f"{round(100 * v)}%"


REPORT_NOTES = [
    ("NF", "number of crashes of the type that have the factor"),
    ("pF", "share of the crash type that has the factor (NF / crashes of the type)"),
    ("pN", "share of the road network (miles) that has the factor; road factors only"),
    ("pC", "share of all crashes that have the factor"),
    ("Based On", "Network = pF > pN, Tot Crash = pF > pC, Both = both are true"),
    ("Points", "Both = 2, Network or Tot Crash = 1, Neither = 0 (the factor is not over-represented either way)"),
    ("Max Score", "highest score a segment can reach: the best points of each category, added up "
                  "(alternatives of the same category, e.g. two speed limits, count once)"),
]


def export_report_xlsx(payload: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    d = report_data(payload)
    title = payload.get("title") or "HRN Risk Factors"
    head_fill = PatternFill("solid", fgColor="44546A")
    white = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="C5CCD4")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()

    def header(ws, cols, row=1):
        for i, t in enumerate(cols, start=1):
            c = ws.cell(row, i, t)
            c.fill, c.font, c.border = head_fill, white, box
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws = wb.active
    ws.title = "Risk factors"
    cols = ["Crash type", "Category", "Factor", "Source", "Based On", "Points", "NF", "pF", "pN", "pC",
            "Road miles", "x normal", "Prob."]
    header(ws, cols)
    for st in d["sets"]:
        first = ws.max_row + 1
        for r in st["rows"]:
            ws.append([st["name"], r["category"], r["factor"], r["source"], r["based_on"], r["points"], r["NF"],
                       r["pF"], r["pN"], r["pC"], None if r["miles"] is None else round(r["miles"], 1),
                       None if r["lift"] is None else round(r["lift"], 2), r["prob"]])
            for i in range(1, len(cols) + 1):
                ws.cell(ws.max_row, i).border = box
            for i in (8, 9, 10, 13):
                ws.cell(ws.max_row, i).number_format = "0%"
        ws.append([st["name"], "Max Score", None, None, None, st["max_score"]])
        for i in range(1, len(cols) + 1):
            ws.cell(ws.max_row, i).font = Font(bold=True)
            ws.cell(ws.max_row, i).fill = PatternFill("solid", fgColor="D9DEE5")
            ws.cell(ws.max_row, i).border = box
    for i, w in enumerate([18, 22, 26, 8, 10, 7, 7, 7, 7, 7, 10, 9, 8], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    ws = wb.create_sheet("Segments")
    multi = len(d["sets"]) > 1
    cols = ["Rank", "Road id", "Name", "Miles", "Score (points)", f"Max ({d['max_total']})", "Factors met",
            "Focus crashes"] + ([f"{s_['name']} points" for s_ in d["sets"]] if multi else [])         + [f"{t} crashes" for t in d["types"]] + ["Factors"]
    header(ws, cols)
    for i, sg in enumerate(d["segments"], start=1):
        ws.append([i, sg["id"], sg["name"], None if sg["miles"] is None else round(sg["miles"], 2), sg["points"],
                   d["max_total"], sg["n"], sg["crashes"]] + (sg["set_points"] if multi else [])
                  + list(sg["type_counts"]) + ["; ".join(sg["factors"])])
    for i, w in enumerate([6, 9, 28, 7, 9, 8, 8, 9], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.column_dimensions[get_column_letter(len(cols))].width = 80
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(2, ws.max_row)}"

    ws = wb.create_sheet("Score summary")
    header(ws, ["Score (points)", "Segments", "Miles", "Focus crashes"])
    agg: dict[int, list] = {}
    for sg in d["segments"]:
        a = agg.setdefault(sg["points"], [0, 0.0, 0])
        a[0] += 1
        a[1] += sg["miles"] or 0
        a[2] += sg["crashes"]
    for k in sorted(agg, reverse=True):
        ws.append([k, agg[k][0], round(agg[k][1], 1), agg[k][2]])
    for c in "ABCD":
        ws.column_dimensions[c].width = 15

    ws = wb.create_sheet("Info", 0)
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    meta = payload.get("meta") or {}
    for k, v in [("Crash file", STATE.get("crash_name")), ("Road file", STATE.get("road_name")),
                 ("Crash-type column", meta.get("focus_column")), ("Restricted to", meta.get("extra")),
                 ("Min. crashes for a crash-based factor", payload.get("min_crashes") or 1),
                 ("Crashes analysed", d["N"]), ("Road segments", d["n_roads"]),
                 ("Network miles", None if d["network_miles"] is None else round(d["network_miles"], 1))]:
        ws.append([k, v if v not in (None, "") else "-"])
    ws.append([])
    for k, v in REPORT_NOTES:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 100
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

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.8)
    st_ = doc.styles["Normal"]
    st_.font.name = "Calibri"
    st_.font.size = Pt(10)
    doc.add_heading(title, level=1)
    meta = payload.get("meta") or {}
    intro = (f"Crashes analysed: {d['N']:,}"
             + (f" ({meta.get('extra')})" if meta.get("extra") else "")
             + f". Road network: {d['n_roads']:,} segments"
             + (f", {d['network_miles']:,.0f} miles" if d["network_miles"] else "") + ".")
    doc.add_paragraph(intro)

    center = WD_ALIGN_PARAGRAPH.CENTER
    for st in d["sets"]:
        t = doc.add_table(rows=2, cols=6)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        top = t.rows[0].cells
        top[0].merge(top[5])
        set_text(t.rows[0].cells[0], f"{st['name']} Crashes", bold=True, color="FFFFFF", align=center, size=10)
        shade(t.rows[0].cells[0], "1F3864")
        for i, h in enumerate(["Segment Risk Factor", "Based On", "NF", "pF", "pN", "pC"]):
            set_text(t.rows[1].cells[i], h, bold=True, align=center if i else None)
            shade(t.rows[1].cells[i], "D9E2F3")
        rows = sorted(st["rows"], key=lambda r: (r["category"].lower(), r["factor"]))
        if not rows:
            set_text(t.add_row().cells[0], "None")
        for r in rows:
            cells = t.add_row().cells
            set_text(cells[0], r["label"])
            set_text(cells[1], r["based_on"], align=center)
            set_text(cells[2], r["NF"], align=center)
            set_text(cells[3], _pct(r["pF"]), align=center)
            set_text(cells[4], _pct(r["pN"]) if r["pN"] is not None else "-", align=center)
            set_text(cells[5], _pct(r["pC"]), align=center)
        last = t.add_row().cells
        last[0].merge(last[5])
        set_text(t.rows[-1].cells[0], f"Max Score: {st['max_score']}", bold=True, align=center)
        fix_widths(t, [Inches(3.2), Inches(1.0), Inches(0.65), Inches(0.65), Inches(0.65), Inches(0.65)])
        doc.add_paragraph()

    doc.add_heading("Highest scoring segments", level=2)
    top = d["segments"][: int(payload.get("top_n") or 20)]
    t = doc.add_table(rows=1, cols=6)
    t.style = "Table Grid"
    for i, h in enumerate(["Rank", "Segment", "Miles", f"Score (of {d['max_total']})", "Factors met", "Focus crashes"]):
        set_text(t.rows[0].cells[i], h, bold=True, align=center)
        shade(t.rows[0].cells[i], "D9E2F3")
    for i, sg in enumerate(top, start=1):
        c = t.add_row().cells
        set_text(c[0], i, align=center)
        set_text(c[1], (sg["name"] or "Segment") + f" (id {sg['id']})")
        set_text(c[2], "" if sg["miles"] is None else f"{sg['miles']:.2f}", align=center)
        set_text(c[3], sg["points"], align=center)
        set_text(c[4], sg["n"], align=center)
        set_text(c[5], sg["crashes"], align=center)
    if not top:
        set_text(t.add_row().cells[0], "No segments.")
    fix_widths(t, [Inches(0.5), Inches(2.9), Inches(0.6), Inches(1.0), Inches(0.9), Inches(0.9)])
    doc.add_paragraph()

    doc.add_heading("Notes", level=2)
    for k, v in REPORT_NOTES:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(k + " - ").bold = True
        p.add_run(v)
    doc.add_paragraph(f"Crash file: {STATE.get('crash_name') or '-'}. Road file: {STATE.get('road_name') or '-'}. "
                      f"A crash-based factor is met on a segment with at least {payload.get('min_crashes') or 1} "
                      "crash(es) of the type that have it; a road factor is met by the segment's attribute.")
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
                elif url.path == "/api/scan-export":
                    self._send(export_scan_xlsx(payload),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif url.path == "/api/score":
                    self.send_json(score_network(payload))
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
                "latlon": safe_latlon_guess(df)}
    if kind == "road":
        gdf, attr = read_road_file(data, filename)
        name_col = guess_name_column(list(attr.columns))
        STATE.update(roads=gdf, road_attr=attr, road_name=filename, road_name_col=name_col, road_len=road_lengths_mi(gdf),
                     road_geojson=build_road_geojson(gdf, name_col), combined=None, points=None, map_stats=None)
        b = gdf.total_bounds
        return {"name": filename, "rows": int(len(gdf)), "columns": [c for c in attr.columns],
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
