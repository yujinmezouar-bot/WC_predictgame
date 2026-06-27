# =============================================================================
# WORLD CUP PREDICTION APP - Single File Streamlit Application
# Storage: CSV files via pandas (no database required)
# =============================================================================
# Sections:
#   1. Imports & Configuration
#   2. CSV Storage Layer (replaces SQLAlchemy ORM)
#   3. CSV Initialization
#   4. Auth Helpers (bcrypt)
#   5. Football API Integration  ← MODIFIED: footballdata.io
#   6. Scoring Engine
#   7. Leaderboard Helpers
#   8. CSS & Theme
#   9. Page: Login
#  10. Page: Dashboard
#  11. Page: Predictions
#  12. Page: Match History
#  13. Page: Leaderboard
#  14. Page: Statistics
#  15. Page: Admin Panel
#  16. Main Router
# =============================================================================

# ─── 1. IMPORTS & CONFIGURATION ──────────────────────────────────────────────
import os
import io
import csv
import datetime
import threading
import requests
import bcrypt
import pandas as pd
import streamlit as st
import altair as alt

# ── App-wide constants ──────────────────────────────────────────────────────
APP_TITLE      = "⚽ World Cup Predictor"
ADMIN_USERNAME = "admin"
DATA_DIR       = "data"   # all CSV files live here

# Footballdata.io — set your key as env var:
#   export FOOTBALL_API_KEY="your_key_here"
FOOTBALL_API_KEY  = os.getenv("FOOTBALL_API_KEY", "")
FOOTBALL_API_BASE = "https://api.footballdata.io/v1"   # ← footballdata.io base URL

# Scoring constants
POINTS_EXACT     = 2
POINTS_GOAL_DIFF = 1
POINTS_WRONG     = 0

# ─── 2. CSV STORAGE LAYER ────────────────────────────────────────────────────
# Each CSV has a fixed schema.  All I/O goes through these helpers so the rest
# of the app can treat data the same way it used SQLAlchemy sessions before.

_lock = threading.Lock()   # coarse file-level lock for concurrent Streamlit threads

# ── Schema definitions (column names + default dtypes) ──────────────────────
SCHEMA = {
    "users": {
        "path": os.path.join(DATA_DIR, "users.csv"),
        "columns": ["id", "username", "password_hash", "is_admin", "imported_points", "created_at"],
    },
    "matches": {
        "path": os.path.join(DATA_DIR, "matches.csv"),
        "columns": [
            "id", "api_fixture_id", "home_team", "away_team",
            "home_logo", "away_logo", "kickoff_utc", "competition",
            "status", "home_goals", "away_goals", "scores_updated",
        ],
    },
    "predictions": {
        "path": os.path.join(DATA_DIR, "predictions.csv"),
        "columns": [
            "id", "user_id", "match_id",
            "predicted_home", "predicted_away",
            "points_earned", "submitted_at",
        ],
    },
}


def _csv_path(table: str) -> str:
    return SCHEMA[table]["path"]


def _read(table: str) -> pd.DataFrame:
    """Read a CSV table into a DataFrame.  Always returns correct columns."""
    path = _csv_path(table)
    cols = SCHEMA[table]["columns"]
    if not os.path.exists(path):
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_csv(path, dtype=str)
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        return df[cols]
    except Exception:
        return pd.DataFrame(columns=cols)


def _write(table: str, df: pd.DataFrame):
    """Persist a DataFrame back to its CSV file."""
    path = _csv_path(table)
    with _lock:
        df.to_csv(path, index=False)


def _next_id(df: pd.DataFrame) -> int:
    """Auto-increment integer PK."""
    if df.empty or df["id"].isna().all():
        return 1
    return int(pd.to_numeric(df["id"], errors="coerce").max()) + 1


# ── User helpers ─────────────────────────────────────────────────────────────

def users_df() -> pd.DataFrame:
    return _read("users")


def get_user_by_username(username: str):
    df = users_df()
    row = df[df["username"] == username]
    return row.iloc[0].to_dict() if not row.empty else None


def get_user_by_id(uid):
    df = users_df()
    row = df[df["id"] == str(uid)]
    return row.iloc[0].to_dict() if not row.empty else None


def create_user(username: str, password_hash: str, is_admin: bool = False) -> bool:
    df = users_df()
    if not df[df["username"] == username].empty:
        return False
    new_row = {
        "id": _next_id(df),
        "username": username,
        "password_hash": password_hash,
        "is_admin": str(is_admin),
        "imported_points": "0",
        "created_at": datetime.datetime.utcnow().isoformat(),
    }
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    _write("users", df)
    return True


def update_user_password(user_id, new_hash: str):
    df = users_df()
    df.loc[df["id"] == str(user_id), "password_hash"] = new_hash
    _write("users", df)


def delete_user(user_id):
    df = users_df()
    df = df[df["id"] != str(user_id)]
    _write("users", df)
    pdf = _read("predictions")
    pdf = pdf[pdf["user_id"] != str(user_id)]
    _write("predictions", pdf)


def set_imported_points(username: str, points: int):
    df = users_df()
    df.loc[df["username"] == username, "imported_points"] = str(points)
    _write("users", df)


# ── Match helpers ─────────────────────────────────────────────────────────────

def matches_df() -> pd.DataFrame:
    return _read("matches")


def get_match_by_id(mid) -> dict | None:
    df = matches_df()
    row = df[df["id"] == str(mid)]
    return row.iloc[0].to_dict() if not row.empty else None


def get_match_by_api_id(api_id) -> dict | None:
    df = matches_df()
    row = df[df["api_fixture_id"] == str(api_id)]
    return row.iloc[0].to_dict() if not row.empty else None


def upsert_match(api_fixture_id, home_team, away_team, home_logo, away_logo,
                 kickoff_utc, competition, status,
                 home_goals=None, away_goals=None) -> str:
    """Insert or update a match row. Returns the match id."""
    df = matches_df()
    mask = df["api_fixture_id"] == str(api_fixture_id)

    if mask.any():
        mid = df.loc[mask, "id"].values[0]
    else:
        mid = str(_next_id(df))
        new_row = pd.DataFrame([{c: "" for c in SCHEMA["matches"]["columns"]}])
        new_row["id"] = mid
        new_row["api_fixture_id"] = str(api_fixture_id)
        new_row["scores_updated"] = "False"
        df = pd.concat([df, new_row], ignore_index=True)
        mask = df["api_fixture_id"] == str(api_fixture_id)

    df.loc[mask, "home_team"]   = home_team
    df.loc[mask, "away_team"]   = away_team
    df.loc[mask, "home_logo"]   = home_logo or ""
    df.loc[mask, "away_logo"]   = away_logo or ""
    df.loc[mask, "kickoff_utc"] = str(kickoff_utc) if kickoff_utc else ""
    df.loc[mask, "competition"] = competition or ""
    df.loc[mask, "status"]      = status or "NS"
    if home_goals is not None:
        df.loc[mask, "home_goals"] = str(home_goals)
    if away_goals is not None:
        df.loc[mask, "away_goals"] = str(away_goals)

    _write("matches", df)
    return mid


def update_match_score(mid, home_goals, away_goals, status):
    df = matches_df()
    mask = df["id"] == str(mid)
    df.loc[mask, "home_goals"]     = str(home_goals)
    df.loc[mask, "away_goals"]     = str(away_goals)
    df.loc[mask, "status"]         = status
    df.loc[mask, "scores_updated"] = "True"
    _write("matches", df)


def update_match_fields(mid: str, fields: dict):
    """Patch arbitrary columns of a match row."""
    df = matches_df()
    for col, val in fields.items():
        df.loc[df["id"] == str(mid), col] = str(val)
    _write("matches", df)


# ── Prediction helpers ────────────────────────────────────────────────────────

def predictions_df() -> pd.DataFrame:
    return _read("predictions")


def get_prediction(user_id, match_id) -> dict | None:
    df = predictions_df()
    row = df[(df["user_id"] == str(user_id)) & (df["match_id"] == str(match_id))]
    return row.iloc[0].to_dict() if not row.empty else None


def save_prediction(user_id, match_id, predicted_home: int, predicted_away: int):
    df = predictions_df()
    mask = (df["user_id"] == str(user_id)) & (df["match_id"] == str(match_id))
    if mask.any():
        df.loc[mask, "predicted_home"] = str(predicted_home)
        df.loc[mask, "predicted_away"] = str(predicted_away)
        df.loc[mask, "submitted_at"]   = datetime.datetime.utcnow().isoformat()
    else:
        new_row = {
            "id":             _next_id(df),
            "user_id":        str(user_id),
            "match_id":       str(match_id),
            "predicted_home": str(predicted_home),
            "predicted_away": str(predicted_away),
            "points_earned":  "",
            "submitted_at":   datetime.datetime.utcnow().isoformat(),
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    _write("predictions", df)


def set_prediction_points(pred_id, points: int):
    df = predictions_df()
    df.loc[df["id"] == str(pred_id), "points_earned"] = str(points)
    _write("predictions", df)


# ─── 3. CSV INITIALIZATION ───────────────────────────────────────────────────

def init_csv_store():
    """Create data directory + empty CSVs with correct headers if missing."""
    os.makedirs(DATA_DIR, exist_ok=True)
    for table, meta in SCHEMA.items():
        path = meta["path"]
        if not os.path.exists(path):
            pd.DataFrame(columns=meta["columns"]).to_csv(path, index=False)


def ensure_admin():
    """Create default admin account on first run."""
    if get_user_by_username(ADMIN_USERNAME) is None:
        pw_hash = bcrypt.hashpw("admin123".encode(), bcrypt.gensalt()).decode()
        create_user(ADMIN_USERNAME, pw_hash, is_admin=True)


# ─── 4. AUTH HELPERS ─────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def authenticate(username: str, password: str):
    """Return user dict or None."""
    user = get_user_by_username(username.strip())
    if user and verify_password(password, user["password_hash"]):
        return user
    return None


# ─── 5. FOOTBALL API INTEGRATION (footballdata.io) ───────────────────────────
#
# Base URL : https://api.footballdata.io/v1
# Auth     : ?api_key=YOUR_KEY  (query-string parameter)
# Endpoints used:
#   GET /matches?api_key=…&league_id=…&season_id=…   → fetch fixtures
#   GET /matches?api_key=…&date=YYYY-MM-DD            → today's results
#
# Response envelope:
#   { "data": [ <match>, … ] }
#
# Match object fields we consume:
#   match_id                        – unique fixture identifier
#   match_date                      – ISO-8601 datetime string
#   status                          – "upcoming" | "live" | "finished"
#   home_team.team_name
#   away_team.team_name
#   score.home  / score.away        – null when not yet played
#   league.league_id  (or league_name)
#   season.season_id
#
# Internal status mapping → stored in matches.csv as the short codes the
# rest of the app already understands:
#   "upcoming"  → "NS"
#   "live"      → "1H"   (we store live as in-progress; exact half unknown)
#   "finished"  → "FT"
#   anything else stored verbatim (e.g. "AET", "PEN" if the API sends them)
#
# Finished-match detection used by update_live_results():
#   status in ("FT", "AET", "PEN")   ← same set used everywhere else in app
# =============================================================================

# ── Status normalisation ─────────────────────────────────────────────────────
_FD_STATUS_MAP = {
    "upcoming":   "NS",
    "scheduled":  "NS",
    "tbd":        "NS",
    "live":       "1H",
    "in_play":    "1H",
    "halftime":   "HT",
    "finished":   "FT",
    "completed":  "FT",
    "aet":        "AET",
    "pen":        "PEN",
    "postponed":  "PST",
    "cancelled":  "CANC",
    "suspended":  "SUSP",
}

FINISHED_STATUSES = {"FT", "AET", "PEN"}
LIVE_STATUSES     = {"1H", "2H", "HT", "ET", "P"}


def _normalise_status(raw: str) -> str:
    """Map footballdata.io status string to the short code used internally."""
    if not raw:
        return "NS"
    return _FD_STATUS_MAP.get(raw.lower().strip(), raw.upper())


# ── Low-level HTTP helper ─────────────────────────────────────────────────────

def _api_get(endpoint: str, params: dict):
    """
    GET {FOOTBALL_API_BASE}/{endpoint} with api_key injected.
    Returns (list_of_match_dicts, error_message_or_None).
    """
    if not FOOTBALL_API_KEY:
        return None, "⚠️ No FOOTBALL_API_KEY set. Please configure your API key."
    try:
        url = f"{FOOTBALL_API_BASE}/{endpoint}"
        params = {**params, "api_key": FOOTBALL_API_KEY}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        body = r.json()

        # footballdata.io wraps results in a "data" key
        if isinstance(body, dict):
            if "error" in body:
                return None, f"API error: {body['error']}"
            data = body.get("data") or body.get("matches") or body.get("results") or []
        elif isinstance(body, list):
            data = body          # some endpoints return a bare list
        else:
            data = []

        return data, None

    except requests.exceptions.ConnectionError:
        return None, "🔌 Cannot reach the football API. Check your internet connection."
    except requests.exceptions.Timeout:
        return None, "⏱️ Football API request timed out. Try again in a moment."
    except Exception as ex:
        return None, f"❌ API error: {ex}"


# ── Parse a single match dict from footballdata.io ───────────────────────────

def _parse_match(item: dict) -> dict | None:
    """
    Extract the fields we need from one footballdata.io match object.
    Returns a normalised dict or None if the item is unusable.
    """
    match_id = item.get("match_id")
    if not match_id:
        return None

    # ── kickoff datetime
    raw_date = item.get("match_date") or item.get("date") or ""
    kickoff = None
    if raw_date:
        try:
            # Handle both "2026-06-15T20:00:00Z" and "2026-06-15 20:00:00"
            kickoff = datetime.datetime.fromisoformat(
                raw_date.replace("Z", "+00:00").replace(" ", "T")
            )
        except ValueError:
            kickoff = None

    # ── teams
    home_obj = item.get("home_team") or {}
    away_obj = item.get("away_team") or {}
    home_team = home_obj.get("team_name") or home_obj.get("name") or ""
    away_team = away_obj.get("team_name") or away_obj.get("name") or ""
    home_logo = home_obj.get("logo") or home_obj.get("team_logo") or ""
    away_logo = away_obj.get("logo") or away_obj.get("team_logo") or ""

    # ── score
    score_obj  = item.get("score") or {}
    home_goals = score_obj.get("home")   # None when not played yet
    away_goals = score_obj.get("away")

    # ── status
    raw_status = item.get("status") or ""
    status     = _normalise_status(raw_status)

    # ── competition / league name
    league_obj   = item.get("league")  or {}
    season_obj   = item.get("season")  or {}
    competition  = (
        league_obj.get("league_name")
        or league_obj.get("name")
        or str(league_obj.get("league_id", ""))
        or "World Cup"
    )

    return {
        "match_id":   str(match_id),
        "home_team":  home_team,
        "away_team":  away_team,
        "home_logo":  home_logo,
        "away_logo":  away_logo,
        "kickoff":    kickoff,
        "competition": competition,
        "status":     status,
        "home_goals": home_goals,
        "away_goals": away_goals,
    }


# ── Public API functions called from the Admin Panel and auto-refresh ─────────

def fetch_and_store_fixtures(league_id: int = 1, season: int = 2026) -> str:
    """
    Download all fixtures for a league + season from footballdata.io and
    upsert them into matches.csv.

    Endpoint: GET /matches?league_id=<id>&season_id=<year>
    """
    data, err = _api_get("matches", {"league_id": league_id, "season_id": season})
    if err:
        return err
    if not data:
        return "No fixtures returned by the API."

    count = 0
    for item in data:
        parsed = _parse_match(item)
        if not parsed:
            continue
        upsert_match(
            api_fixture_id = parsed["match_id"],
            home_team      = parsed["home_team"],
            away_team      = parsed["away_team"],
            home_logo      = parsed["home_logo"],
            away_logo      = parsed["away_logo"],
            kickoff_utc    = parsed["kickoff"],
            competition    = parsed["competition"],
            status         = parsed["status"],
            home_goals     = parsed["home_goals"],
            away_goals     = parsed["away_goals"],
        )
        count += 1

    return f"✅ Synced {count} fixtures."


def update_live_results() -> str:
    """
    Fetch today's matches from footballdata.io, update scores/status in
    matches.csv, and trigger point calculation for newly-finished games.

    Endpoint: GET /matches?date=YYYY-MM-DD
    """
    today = datetime.date.today().isoformat()
    data, err = _api_get("matches", {"date": today})
    if err:
        return err

    mdf     = matches_df()
    updated = 0

    for item in data:
        parsed = _parse_match(item)
        if not parsed:
            continue

        api_id = parsed["match_id"]
        row    = mdf[mdf["api_fixture_id"] == api_id]
        if row.empty:
            # match not in our DB yet — store it so predictions can be made
            upsert_match(
                api_fixture_id = api_id,
                home_team      = parsed["home_team"],
                away_team      = parsed["away_team"],
                home_logo      = parsed["home_logo"],
                away_logo      = parsed["away_logo"],
                kickoff_utc    = parsed["kickoff"],
                competition    = parsed["competition"],
                status         = parsed["status"],
                home_goals     = parsed["home_goals"],
                away_goals     = parsed["away_goals"],
            )
            # reload to get the new row's internal id
            mdf = matches_df()
            row = mdf[mdf["api_fixture_id"] == api_id]

        mid          = row.iloc[0]["id"]
        already_done = str(row.iloc[0].get("scores_updated", "False")) == "True"

        # always update status and score
        fields: dict = {"status": parsed["status"]}
        if parsed["home_goals"] is not None:
            fields["home_goals"] = parsed["home_goals"]
        if parsed["away_goals"] is not None:
            fields["away_goals"] = parsed["away_goals"]
        update_match_fields(mid, fields)

        # score predictions once when a match first reaches a finished status
        if parsed["status"] in FINISHED_STATUSES and not already_done:
            m_dict = get_match_by_id(mid)
            if m_dict:
                _calculate_points_for_match(m_dict)
                update_match_fields(mid, {"scores_updated": "True"})
                updated += 1

    return f"✅ Updated results for today. Scored {updated} completed matches."


# ─── 6. SCORING ENGINE ───────────────────────────────────────────────────────

def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _calculate_points_for_match(match: dict):
    """Award points for all predictions on a finished match."""
    hg = _safe_int(match.get("home_goals"))
    ag = _safe_int(match.get("away_goals"))
    if hg is None or ag is None:
        return

    actual_diff = hg - ag
    pdf = predictions_df()
    mid = str(match["id"])
    match_preds = pdf[pdf["match_id"] == mid]

    for _, pred in match_preds.iterrows():
        ph = _safe_int(pred["predicted_home"])
        pa = _safe_int(pred["predicted_away"])
        if ph is None or pa is None:
            pts = POINTS_WRONG
        elif ph == hg and pa == ag:
            pts = POINTS_EXACT
        elif (ph - pa) == actual_diff:
            pts = POINTS_GOAL_DIFF
        else:
            pts = POINTS_WRONG
        set_prediction_points(pred["id"], pts)


def force_recalculate_all() -> str:
    mdf      = matches_df()
    finished = mdf[mdf["status"].isin(list(FINISHED_STATUSES))]
    count    = 0
    for _, m in finished.iterrows():
        if _safe_int(m.get("home_goals")) is not None:
            _calculate_points_for_match(m.to_dict())
            count += 1
    return f"✅ Recalculated points for {count} finished matches."


# ─── 7. LEADERBOARD HELPERS ──────────────────────────────────────────────────

def user_total_points(user_id) -> int:
    user     = get_user_by_id(user_id)
    imported = _safe_int(user.get("imported_points")) or 0 if user else 0
    pdf      = predictions_df()
    earned_rows = pdf[
        (pdf["user_id"] == str(user_id)) &
        (pdf["points_earned"].notna()) &
        (pdf["points_earned"] != "")
    ]["points_earned"]
    earned = int(pd.to_numeric(earned_rows, errors="coerce").sum())
    return imported + earned


def user_today_points(user_id) -> int:
    today_start = datetime.datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0)
    mdf  = matches_df()
    pdf  = predictions_df()
    merged = pdf[pdf["user_id"] == str(user_id)].merge(
        mdf[["id", "kickoff_utc"]], left_on="match_id", right_on="id", suffixes=("", "_m")
    )
    merged["kickoff_dt"] = pd.to_datetime(merged["kickoff_utc"], errors="coerce", utc=False)
    today_preds = merged[merged["kickoff_dt"] >= pd.Timestamp(today_start)]
    earned = pd.to_numeric(today_preds["points_earned"], errors="coerce").sum()
    return int(earned) if not pd.isna(earned) else 0


def build_overall_leaderboard() -> pd.DataFrame:
    udf       = users_df()
    non_admins = udf[udf["is_admin"] != "True"]
    rows = []
    for _, u in non_admins.iterrows():
        rows.append({"Username": u["username"], "Total Points": user_total_points(u["id"])})
    if not rows:
        return pd.DataFrame(columns=["Username", "Total Points"])
    df = pd.DataFrame(rows).sort_values("Total Points", ascending=False).reset_index(drop=True)
    df.index += 1
    return df


def build_daily_leaderboard() -> pd.DataFrame:
    udf       = users_df()
    non_admins = udf[udf["is_admin"] != "True"]
    rows = []
    for _, u in non_admins.iterrows():
        rows.append({"Username": u["username"], "Today's Points": user_today_points(u["id"])})
    if not rows:
        return pd.DataFrame(columns=["Username", "Today's Points"])
    df = pd.DataFrame(rows).sort_values("Today's Points", ascending=False).reset_index(drop=True)
    df.index += 1
    return df


def medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, str(rank))


# ─── 8. CSS & THEME ──────────────────────────────────────────────────────────
CUSTOM_CSS = """
<style>
html, body, [class*="css"] { font-family: 'Segoe UI', sans-serif; }
.main { background: #0a1628; color: #e8eaf6; }

section[data-testid="stSidebar"] {
    background: linear-gradient(180deg,#0d2137 0%,#0a1628 100%);
    border-right: 1px solid #1e3a5f;
}
div[data-testid="metric-container"] {
    background: linear-gradient(135deg,#0d2137,#1a3a5c);
    border: 1px solid #1e3a5f;
    border-radius: 12px;
    padding: 16px;
}
.match-card {
    background: linear-gradient(135deg,#0d2137 0%,#1a3a5c 100%);
    border: 1px solid #1e4a7a;
    border-radius: 14px;
    padding: 18px 22px;
    margin-bottom: 14px;
    box-shadow: 0 4px 15px rgba(0,0,0,0.3);
}
.match-card:hover { border-color: #4fc3f7; }
.match-teams { font-size:1.2rem; font-weight:700; color:#e8eaf6; letter-spacing:.5px; }
.match-meta  { font-size:.8rem; color:#90caf9; margin-top:4px; }
.score-badge { background:#1565c0; color:#fff; border-radius:8px; padding:4px 12px; font-weight:700; font-size:1.1rem; display:inline-block; }
.points-badge-2 { background:#2e7d32; color:#fff; border-radius:8px; padding:3px 10px; font-weight:700; }
.points-badge-1 { background:#e65100; color:#fff; border-radius:8px; padding:3px 10px; font-weight:700; }
.points-badge-0 { background:#b71c1c; color:#fff; border-radius:8px; padding:3px 10px; font-weight:700; }
.section-title { font-size:1.4rem; font-weight:700; color:#4fc3f7; border-bottom:2px solid #1e3a5f; padding-bottom:8px; margin:24px 0 16px; }
button[data-baseweb="tab"] { color:#90caf9 !important; }
button[data-baseweb="tab"][aria-selected="true"] { color:#4fc3f7 !important; border-bottom:2px solid #4fc3f7 !important; }
.stButton>button { background:linear-gradient(135deg,#1565c0,#0d47a1); color:#fff; border:none; border-radius:8px; font-weight:600; transition:all .2s; }
.stButton>button:hover { background:linear-gradient(135deg,#1976d2,#1565c0); transform:translateY(-1px); }
.lb-row { display:flex; align-items:center; background:#0d2137; border:1px solid #1e3a5f; border-radius:10px; padding:10px 16px; margin-bottom:8px; }
.lb-rank { font-size:1.3rem; width:50px; }
.lb-name { flex:1; font-weight:600; color:#e8eaf6; }
.lb-pts  { font-size:1.1rem; font-weight:700; color:#4fc3f7; }
</style>
"""


def inject_css():
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ─── 9. PAGE: LOGIN ──────────────────────────────────────────────────────────

def page_login():
    inject_css()
    st.markdown("<h1 style='text-align:center;color:#4fc3f7;'>⚽ World Cup Predictor</h1>",
                unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;color:#90caf9;'>Sign in to make your predictions</p>",
                unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    col = st.columns([1, 1.4, 1])[1]
    with col:
        with st.form("login_form"):
            username  = st.text_input("👤 Username")
            password  = st.text_input("🔒 Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)

        if submitted:
            user = authenticate(username, password)
            if user:
                st.session_state["user_id"]  = str(user["id"])
                st.session_state["username"] = user["username"]
                st.session_state["is_admin"] = str(user["is_admin"]) == "True"
                st.rerun()
            else:
                st.error("Invalid username or password.")


# ─── 10. PAGE: DASHBOARD ─────────────────────────────────────────────────────

def page_dashboard():
    inject_css()
    uid       = st.session_state["user_id"]
    total_pts = user_total_points(uid)
    today_pts = user_today_points(uid)

    st.markdown(f"<h2 style='color:#4fc3f7;'>👋 Welcome, {st.session_state['username']}!</h2>",
                unsafe_allow_html=True)

    pdf = predictions_df()
    user_preds = pdf[
        (pdf["user_id"] == str(uid)) &
        (pdf["points_earned"].notna()) &
        (pdf["points_earned"] != "")
    ]
    total_pred = len(user_preds)
    exact_pred = (pd.to_numeric(user_preds["points_earned"], errors="coerce") == POINTS_EXACT).sum()
    accuracy   = f"{(exact_pred / total_pred * 100):.0f}%" if total_pred else "—"

    c1, c2, c3 = st.columns(3)
    c1.metric("🏆 Total Points",   total_pts)
    c2.metric("📅 Today's Points", today_pts)
    c3.metric("🎯 Exact Score %",  accuracy)

    now_utc     = datetime.datetime.utcnow()
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + datetime.timedelta(days=1)

    mdf = matches_df()
    mdf["kickoff_dt"] = pd.to_datetime(mdf["kickoff_utc"], errors="coerce")

    today_matches = mdf[
        (mdf["kickoff_dt"] >= pd.Timestamp(today_start)) &
        (mdf["kickoff_dt"] <  pd.Timestamp(today_end))
    ].sort_values("kickoff_dt")

    st.markdown("<div class='section-title'>📅 Today's Matches</div>", unsafe_allow_html=True)
    if not today_matches.empty:
        for _, m in today_matches.iterrows():
            _render_match_card(m.to_dict())
    else:
        st.info("No matches today.")

    upcoming = mdf[
        (mdf["kickoff_dt"] >= pd.Timestamp(today_end)) &
        (mdf["kickoff_dt"] <  pd.Timestamp(today_end + datetime.timedelta(days=7)))
    ].sort_values("kickoff_dt").head(6)

    st.markdown("<div class='section-title'>🔮 Upcoming Matches</div>", unsafe_allow_html=True)
    if not upcoming.empty:
        cols = st.columns(2)
        for i, (_, m) in enumerate(upcoming.iterrows()):
            with cols[i % 2]:
                _render_match_card(m.to_dict(), compact=True)
    else:
        st.info("No upcoming matches in the next 7 days.")

    st.markdown("<div class='section-title'>🏅 Leaderboards</div>", unsafe_allow_html=True)
    l1, l2 = st.columns(2)
    with l1:
        st.markdown("**Daily Ranking**")
        _render_leaderboard(build_daily_leaderboard(), "Today's Points")
    with l2:
        st.markdown("**Overall Ranking**")
        _render_leaderboard(build_overall_leaderboard(), "Total Points")


def _render_match_card(m: dict, compact: bool = False):
    ko_raw = m.get("kickoff_utc", "")
    try:
        ko     = datetime.datetime.fromisoformat(str(ko_raw).replace("+00:00", ""))
        ko_str = ko.strftime("%d %b %Y  %H:%M UTC")
    except Exception:
        ko     = None
        ko_str = "TBD"

    status = m.get("status", "NS")
    hg     = _safe_int(m.get("home_goals"))
    ag     = _safe_int(m.get("away_goals"))

    if status in FINISHED_STATUSES and hg is not None:
        score        = f"{hg} – {ag}"
        status_label = f"<span style='color:#66bb6a;'>✅ {status}</span>"
    elif status in LIVE_STATUSES:
        score        = f"{hg or 0} – {ag or 0}"
        status_label = "<span style='color:#ffca28;'>🔴 LIVE</span>"
    else:
        score        = "vs"
        status_label = "<span style='color:#90caf9;'>🕒 Scheduled</span>"

    html = f"""
    <div class="match-card">
      <div class="match-teams">{m.get('home_team','')} <span class="score-badge">{score}</span> {m.get('away_team','')}</div>
      <div class="match-meta">{status_label} &nbsp;|&nbsp; 📅 {ko_str} &nbsp;|&nbsp; 🏆 {m.get('competition','')}</div>
    </div>"""
    st.markdown(html, unsafe_allow_html=True)


def _render_leaderboard(df: pd.DataFrame, pts_col: str):
    if df.empty:
        st.info("No data yet.")
        return
    for rank, row in df.iterrows():
        html = f"""
        <div class="lb-row">
          <div class="lb-rank">{medal(rank)}</div>
          <div class="lb-name">{row['Username']}</div>
          <div class="lb-pts">{row[pts_col]} pts</div>
        </div>"""
        st.markdown(html, unsafe_allow_html=True)


# ─── 11. PAGE: PREDICTIONS ───────────────────────────────────────────────────

def page_predictions():
    inject_css()
    uid     = st.session_state["user_id"]
    now_utc = datetime.datetime.utcnow()

    st.markdown("<h2 style='color:#4fc3f7;'>🎯 Match Predictions</h2>", unsafe_allow_html=True)

    mdf = matches_df()
    if mdf.empty:
        st.info("No matches available. Ask an admin to sync the API.")
        return

    mdf["kickoff_dt"] = pd.to_datetime(mdf["kickoff_utc"], errors="coerce")
    cutoff = pd.Timestamp(now_utc - datetime.timedelta(days=1))
    mdf    = mdf[mdf["kickoff_dt"] >= cutoff].sort_values("kickoff_dt")

    if mdf.empty:
        st.info("No upcoming matches found.")
        return

    by_date: dict = {}
    for _, m in mdf.iterrows():
        d = m["kickoff_dt"].date() if pd.notna(m["kickoff_dt"]) else datetime.date.today()
        by_date.setdefault(d, []).append(m.to_dict())

    for date, ms in sorted(by_date.items()):
        with st.expander(
            f"📅  {date.strftime('%A, %d %B %Y')}  ({len(ms)} matches)",
            expanded=(date == datetime.date.today())
        ):
            for m in ms:
                ko_raw = m.get("kickoff_utc", "")
                try:
                    ko = datetime.datetime.fromisoformat(str(ko_raw).replace("+00:00", ""))
                except Exception:
                    ko = None

                locked = (ko is not None and ko <= now_utc)
                pred   = get_prediction(uid, m["id"])
                ph     = _safe_int((pred or {}).get("predicted_home")) or 0
                pa     = _safe_int((pred or {}).get("predicted_away")) or 0

                lock_label = "&nbsp;|&nbsp; 🔒 Locked" if locked else ""
                st.markdown(f"""
                <div class="match-card">
                  <div class="match-teams">{m.get('home_team','')} vs {m.get('away_team','')}</div>
                  <div class="match-meta">
                    🏆 {m.get('competition','')} &nbsp;|&nbsp;
                    🕒 {ko.strftime('%H:%M UTC') if ko else 'TBD'}
                    {lock_label}
                  </div>
                </div>""", unsafe_allow_html=True)

                col1, col2, col3 = st.columns([2, 2, 2])
                with col1:
                    h_val = st.number_input(
                        f"{m.get('home_team','')} Goals",
                        min_value=0, max_value=20, value=ph, step=1,
                        key=f"ph_{m['id']}", disabled=locked
                    )
                with col2:
                    a_val = st.number_input(
                        f"{m.get('away_team','')} Goals",
                        min_value=0, max_value=20, value=pa, step=1,
                        key=f"pa_{m['id']}", disabled=locked
                    )
                with col3:
                    if locked:
                        if pred:
                            pts         = _safe_int(pred.get("points_earned"))
                            hg          = _safe_int(m.get("home_goals"))
                            ag          = _safe_int(m.get("away_goals"))
                            result_str  = f"{hg}–{ag}" if hg is not None else "pending"
                            badge_class = (
                                "points-badge-2" if pts == 2 else
                                "points-badge-1" if pts == 1 else
                                "points-badge-0"
                            )
                            pts_display = pts if pts is not None else "?"
                            st.markdown(
                                f"Your prediction: **{ph}–{pa}** | Result: **{result_str}** "
                                f"<span class='{badge_class}'>{pts_display} pts</span>",
                                unsafe_allow_html=True
                            )
                        else:
                            st.warning("No prediction made (0 pts)")
                    else:
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.button("💾 Save", key=f"save_{m['id']}"):
                            save_prediction(uid, m["id"], h_val, a_val)
                            st.success("Saved!")
                            st.rerun()


# ─── 12. PAGE: MATCH HISTORY ─────────────────────────────────────────────────

def page_history():
    inject_css()
    uid = st.session_state["user_id"]
    st.markdown("<h2 style='color:#4fc3f7;'>📜 My Match History</h2>", unsafe_allow_html=True)

    pdf      = predictions_df()
    mdf      = matches_df()
    user_preds = pdf[pdf["user_id"] == str(uid)]
    finished   = mdf[mdf["status"].isin(list(FINISHED_STATUSES))]
    merged     = user_preds.merge(finished, left_on="match_id", right_on="id", suffixes=("", "_m"))

    if merged.empty:
        st.info("No completed matches with predictions yet.")
        return

    merged["kickoff_dt"] = pd.to_datetime(merged["kickoff_utc"], errors="coerce")
    merged = merged.sort_values("kickoff_dt", ascending=False)

    rows = []
    for _, row in merged.iterrows():
        hg = _safe_int(row.get("home_goals"))
        ag = _safe_int(row.get("away_goals"))
        rows.append({
            "Date":            row["kickoff_dt"].strftime("%d %b %Y") if pd.notna(row["kickoff_dt"]) else "—",
            "Competition":     row.get("competition", "—"),
            "Match":           f"{row.get('home_team','')} vs {row.get('away_team','')}",
            "Your Prediction": f"{row.get('predicted_home','')}–{row.get('predicted_away','')}",
            "Result":          f"{hg}–{ag}" if hg is not None else "—",
            "Points":          row.get("points_earned", "—"),
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ─── 13. PAGE: LEADERBOARD ───────────────────────────────────────────────────

def page_leaderboard():
    inject_css()
    st.markdown("<h2 style='color:#4fc3f7;'>🏆 Leaderboards</h2>", unsafe_allow_html=True)

    tab_daily, tab_overall = st.tabs(["📅 Daily", "🌍 Overall"])
    with tab_daily:
        _render_leaderboard(build_daily_leaderboard(), "Today's Points")
    with tab_overall:
        _render_leaderboard(build_overall_leaderboard(), "Total Points")


# ─── 14. PAGE: STATISTICS ────────────────────────────────────────────────────

def page_statistics():
    inject_css()
    uid = st.session_state["user_id"]
    st.markdown("<h2 style='color:#4fc3f7;'>📊 My Statistics</h2>", unsafe_allow_html=True)

    pdf = predictions_df()
    scored_preds = pdf[
        (pdf["user_id"] == str(uid)) &
        (pdf["points_earned"].notna()) &
        (pdf["points_earned"] != "")
    ].copy()
    scored_preds["pts_num"] = pd.to_numeric(scored_preds["points_earned"], errors="coerce")

    total = len(scored_preds)
    exact = int((scored_preds["pts_num"] == POINTS_EXACT).sum())
    diff  = int((scored_preds["pts_num"] == POINTS_GOAL_DIFF).sum())
    wrong = int((scored_preds["pts_num"] == POINTS_WRONG).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Scored", total)
    c2.metric("🎯 Exact",     exact)
    c3.metric("↔️ Goal Diff", diff)
    c4.metric("❌ Wrong",     wrong)

    if total:
        pie_data = pd.DataFrame({
            "Category": ["Exact (2pts)", "Goal Diff (1pt)", "Wrong (0pts)"],
            "Count":    [exact, diff, wrong],
            "Color":    ["#2e7d32", "#e65100", "#b71c1c"],
        })
        chart = (
            alt.Chart(pie_data)
            .mark_arc(innerRadius=50)
            .encode(
                theta=alt.Theta("Count:Q"),
                color=alt.Color("Category:N",
                                scale=alt.Scale(
                                    domain=pie_data["Category"].tolist(),
                                    range=pie_data["Color"].tolist()
                                )),
                tooltip=["Category", "Count"]
            )
            .properties(title="Prediction Breakdown", height=300)
        )
        st.altair_chart(chart, use_container_width=True)

    mdf    = matches_df()
    merged = scored_preds.merge(mdf[["id", "kickoff_utc"]], left_on="match_id", right_on="id")
    merged["kickoff_dt"] = pd.to_datetime(merged["kickoff_utc"], errors="coerce")
    merged = merged.sort_values("kickoff_dt")

    if not merged.empty:
        user    = get_user_by_id(uid)
        running = _safe_int((user or {}).get("imported_points")) or 0
        cumulative = []
        for _, row in merged.iterrows():
            running += int(row["pts_num"])
            cumulative.append({
                "Date": row["kickoff_dt"].date().isoformat() if pd.notna(row["kickoff_dt"]) else "",
                "Cumulative Points": running,
            })
        ts_df = pd.DataFrame(cumulative)
        line  = (
            alt.Chart(ts_df)
            .mark_line(point=True, color="#4fc3f7")
            .encode(
                x=alt.X("Date:T", title="Date"),
                y=alt.Y("Cumulative Points:Q"),
                tooltip=["Date", "Cumulative Points"]
            )
            .properties(title="Points Over Time", height=300)
        )
        st.altair_chart(line, use_container_width=True)


# ─── 15. PAGE: ADMIN PANEL ───────────────────────────────────────────────────

def page_admin():
    inject_css()
    if not st.session_state.get("is_admin"):
        st.error("Access denied.")
        return

    st.markdown("<h2 style='color:#f44336;'>🛠️ Admin Panel</h2>", unsafe_allow_html=True)

    tab_users, tab_api, tab_scores, tab_matches, tab_predictions = st.tabs(
        ["👥 Users", "🌐 API Sync", "📥 Import Scores", "⚽ Matches", "🔍 User Predictions"]
    )

    # ── Users ──────────────────────────────────────────────────────────────
    with tab_users:
        st.markdown("### Create User")
        with st.form("create_user"):
            nu   = st.text_input("Username")
            np_  = st.text_input("Password", type="password")
            is_a = st.checkbox("Admin")
            if st.form_submit_button("Create"):
                if nu and np_:
                    ok = create_user(nu.strip(), hash_password(np_), is_admin=is_a)
                    if ok:
                        st.success(f"User '{nu}' created.")
                    else:
                        st.error("Username already taken.")
                else:
                    st.warning("Please fill in both fields.")

        st.markdown("### Existing Users")
        udf = users_df()
        for _, u in udf.iterrows():
            cols = st.columns([3, 2, 2, 2])
            cols[0].write(u["username"])
            cols[1].write("Admin" if str(u["is_admin"]) == "True" else "User")

            if cols[2].button("Reset PW", key=f"rpw_{u['id']}"):
                st.session_state[f"reset_{u['id']}"] = True

            if st.session_state.get(f"reset_{u['id']}"):
                new_pw = st.text_input("New password", key=f"npw_{u['id']}", type="password")
                if st.button("Apply", key=f"apw_{u['id']}"):
                    update_user_password(u["id"], hash_password(new_pw))
                    st.session_state[f"reset_{u['id']}"] = False
                    st.success("Password reset.")

            if u["username"] != ADMIN_USERNAME:
                if cols[3].button("Delete", key=f"del_{u['id']}"):
                    delete_user(u["id"])
                    st.success(f"Deleted '{u['username']}'.")
                    st.rerun()

    # ── API Sync ───────────────────────────────────────────────────────────
    with tab_api:
        st.markdown("### Footballdata.io Sync")
        st.caption("Fetches fixtures and results from api.footballdata.io")
        league_id = st.number_input("League ID",  value=1,    step=1)
        season    = st.number_input("Season Year", value=2026, step=1)

        if st.button("⬇️ Fetch Fixtures"):
            msg = fetch_and_store_fixtures(int(league_id), int(season))
            st.info(msg)
        if st.button("🔄 Update Today's Results"):
            msg = update_live_results()
            st.info(msg)
        if st.button("♻️ Force Recalculate All Points"):
            msg = force_recalculate_all()
            st.info(msg)

    # ── Import Scores ──────────────────────────────────────────────────────
    with tab_scores:
        st.markdown("### Import Starting Scores (CSV)")
        st.markdown("Upload a CSV with columns: `username,total_points`")
        uploaded = st.file_uploader("CSV file", type=["csv"])
        if uploaded:
            try:
                text_data = uploaded.read().decode("utf-8")
                reader    = csv.DictReader(io.StringIO(text_data))
                applied   = 0
                for row in reader:
                    un  = row.get("username", "").strip()
                    pts = int(row.get("total_points", 0))
                    if get_user_by_username(un):
                        set_imported_points(un, pts)
                        applied += 1
                st.success(f"Imported starting scores for {applied} users.")
            except Exception as ex:
                st.error(f"Error: {ex}")

    # ── Manage Matches ─────────────────────────────────────────────────────
    with tab_matches:
        st.markdown("### All Matches")
        mdf = matches_df()
        if not mdf.empty:
            display_cols = ["id", "home_team", "away_team", "kickoff_utc",
                            "status", "home_goals", "away_goals", "competition"]
            st.dataframe(mdf[[c for c in display_cols if c in mdf.columns]],
                         use_container_width=True, hide_index=True)
        else:
            st.info("No matches in the database yet.")

        with st.expander("➕ Add New Match"):
            with st.form("add_match"):
                col1, col2 = st.columns(2)
                ht      = col1.text_input("Home Team")
                at      = col2.text_input("Away Team")
                ko_date = col1.date_input("Kickoff Date")
                ko_time = col2.time_input("Kickoff Time (UTC)")
                comp    = st.text_input("Competition", value="World Cup")
                fake_id = st.number_input("Fixture ID (unique int)", value=999999, step=1)
                if st.form_submit_button("Add Match"):
                    if get_match_by_api_id(int(fake_id)):
                        st.error("Fixture ID already exists.")
                    else:
                        ko_dt = datetime.datetime.combine(ko_date, ko_time)
                        upsert_match(
                            api_fixture_id=int(fake_id),
                            home_team=ht, away_team=at,
                            home_logo="", away_logo="",
                            kickoff_utc=ko_dt,
                            competition=comp,
                            status="NS",
                        )
                        st.success("Match added.")
                        st.rerun()

        with st.expander("✏️ Edit Existing Match"):
            mdf2 = matches_df()
            if not mdf2.empty:
                match_labels = {
                    row["id"]: f"[{row['id']}] {row.get('home_team','')} vs {row.get('away_team','')} ({row.get('kickoff_utc','')})"
                    for _, row in mdf2.iterrows()
                }
                selected_id = st.selectbox(
                    "Select match to edit",
                    options=list(match_labels.keys()),
                    format_func=lambda x: match_labels[x]
                )
                sel = mdf2[mdf2["id"] == selected_id].iloc[0]

                with st.form("edit_match"):
                    ec1, ec2 = st.columns(2)
                    e_ht     = ec1.text_input("Home Team",  value=sel.get("home_team", ""))
                    e_at     = ec2.text_input("Away Team",  value=sel.get("away_team", ""))
                    e_comp   = st.text_input("Competition", value=sel.get("competition", ""))
                    valid_statuses = ["NS", "1H", "HT", "2H", "FT", "AET", "PEN"]
                    cur_status     = sel.get("status", "NS")
                    e_status = st.selectbox(
                        "Status",
                        valid_statuses,
                        index=valid_statuses.index(cur_status)
                               if cur_status in valid_statuses else 0
                    )
                    ec3, ec4 = st.columns(2)
                    e_hg = ec3.number_input(
                        "Home Goals", min_value=0, max_value=30,
                        value=_safe_int(sel.get("home_goals")) or 0
                    )
                    e_ag = ec4.number_input(
                        "Away Goals", min_value=0, max_value=30,
                        value=_safe_int(sel.get("away_goals")) or 0
                    )
                    try:
                        existing_ko = datetime.datetime.fromisoformat(
                            str(sel.get("kickoff_utc", "")).replace("+00:00", "")
                        )
                    except Exception:
                        existing_ko = datetime.datetime.utcnow()
                    e_date = st.date_input("Kickoff Date", value=existing_ko.date())
                    e_time = st.time_input("Kickoff Time (UTC)", value=existing_ko.time())

                    if st.form_submit_button("💾 Save Changes"):
                        e_ko = datetime.datetime.combine(e_date, e_time)
                        update_match_fields(selected_id, {
                            "home_team":   e_ht,
                            "away_team":   e_at,
                            "competition": e_comp,
                            "status":      e_status,
                            "home_goals":  e_hg,
                            "away_goals":  e_ag,
                            "kickoff_utc": e_ko.isoformat(),
                        })
                        if e_status in FINISHED_STATUSES:
                            m_dict = get_match_by_id(selected_id)
                            if m_dict:
                                _calculate_points_for_match(m_dict)
                                update_match_fields(selected_id, {"scores_updated": "True"})
                        st.success("Match updated.")
                        st.rerun()
            else:
                st.info("No matches yet.")

    # ── View User Predictions ──────────────────────────────────────────────
    with tab_predictions:
        st.markdown("### View User Predictions")
        udf2    = users_df()
        non_adm = udf2[udf2["is_admin"] != "True"]
        if not non_adm.empty:
            selected_user = st.selectbox("Select User", options=non_adm["username"].tolist())
            u = get_user_by_username(selected_user)
            if u:
                pdf2   = predictions_df()
                mdf3   = matches_df()
                merged = pdf2[pdf2["user_id"] == str(u["id"])].merge(
                    mdf3, left_on="match_id", right_on="id", suffixes=("", "_m")
                )
                merged["kickoff_dt"] = pd.to_datetime(merged["kickoff_utc"], errors="coerce")
                merged = merged.sort_values("kickoff_dt", ascending=False)

                if not merged.empty:
                    rows = [{
                        "Match":      f"{r.get('home_team','')} vs {r.get('away_team','')}",
                        "Kickoff":    str(r.get("kickoff_utc", "")),
                        "Prediction": f"{r.get('predicted_home','')}–{r.get('predicted_away','')}",
                        "Result":     (f"{_safe_int(r.get('home_goals'))}–{_safe_int(r.get('away_goals'))}"
                                      if _safe_int(r.get("home_goals")) is not None else "—"),
                        "Points":     r.get("points_earned", "—"),
                    } for _, r in merged.iterrows()]
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    st.info("No predictions yet.")
        else:
            st.info("No regular users found.")


# ─── 16. MAIN ROUTER ─────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="⚽",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_csv_store()
    ensure_admin()

    if "user_id" not in st.session_state:
        page_login()
        return

    with st.sidebar:
        st.markdown(
            "<h2 style='color:#4fc3f7;text-align:center;'>⚽ WC Predictor</h2>",
            unsafe_allow_html=True
        )
        st.markdown(f"**Logged in as:** {st.session_state['username']}")
        st.divider()

        pages = {
            "🏠 Dashboard":     "dashboard",
            "🎯 Predictions":   "predictions",
            "📜 Match History": "history",
            "🏆 Leaderboard":   "leaderboard",
            "📊 Statistics":    "statistics",
        }
        if st.session_state.get("is_admin"):
            pages["🛠️ Admin Panel"] = "admin"

        selection = st.radio("Navigate", list(pages.keys()), label_visibility="collapsed")
        page_key  = pages[selection]

        st.divider()
        if st.button("🚪 Logout", use_container_width=True):
            for k in ["user_id", "username", "is_admin"]:
                st.session_state.pop(k, None)
            st.rerun()

    if page_key == "dashboard":
        page_dashboard()
    elif page_key == "predictions":
        page_predictions()
    elif page_key == "history":
        page_history()
    elif page_key == "leaderboard":
        page_leaderboard()
    elif page_key == "statistics":
        page_statistics()
    elif page_key == "admin":
        page_admin()


if __name__ == "__main__":
    main()
