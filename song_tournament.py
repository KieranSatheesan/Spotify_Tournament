# song_tournament.py
# A robust, append-only song tournament.
# - Tracks metadata in ~/.song_tournament/song_tournament.db
# - Decisions appended to ./song_tournament_matches.csv (project root)
# - Leaderboard is recomputed from the CSV on every render (wins/losses + Elo)
from __future__ import annotations
import os, csv, json, time, random, sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import List, Tuple, Dict

import pandas as pd
import streamlit as st
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv

# ============ Config ============
load_dotenv()  # read .env if present

APP_DIR = os.path.join(os.path.expanduser("~"), ".song_tournament")
os.makedirs(APP_DIR, exist_ok=True)

DB_PATH = os.path.join(APP_DIR, "song_tournament.db")
TOKEN_CACHE = os.path.join(APP_DIR, ".spotipy_token_cache")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_CSV = os.path.join(ROOT_DIR, "song_tournament_matches.csv")  # append-only log

PAGE_SIZE = 50
ELO_K = 24  # set 0 to disable Elo

# ============ Data model ============
@dataclass
class Track:
    id: str
    name: str
    artist: str
    url: str
    image: str
    preview_url: str | None

# ============ Storage: Tracks in SQLite ============
def db_conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    return con

def init_db():
    with closing(db_conn()) as con, con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                artist TEXT NOT NULL,
                url TEXT NOT NULL,
                image TEXT,
                preview_url TEXT
            )
        """)

def upsert_track(t: Track):
    with closing(db_conn()) as con, con:
        con.execute("""
            INSERT INTO tracks(id, name, artist, url, image, preview_url)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              artist=excluded.artist,
              url=excluded.url,
              image=excluded.image,
              preview_url=excluded.preview_url
        """, (t.id, t.name, t.artist, t.url, t.image, t.preview_url))

def track_ids() -> List[str]:
    with closing(db_conn()) as con:
        return [r[0] for r in con.execute("SELECT id FROM tracks").fetchall()]

def fetch_tracks(ids: List[str]) -> List[Track]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = f"SELECT id, name, artist, url, image, preview_url FROM tracks WHERE id IN ({placeholders})"
    with closing(db_conn()) as con:
        rows = con.execute(sql, ids).fetchall()
    return [Track(*row) for row in rows]

def library_empty() -> bool:
    with closing(db_conn()) as con:
        (n,) = con.execute("SELECT COUNT(*) FROM tracks").fetchone()
    return n < 2

# ============ Decisions: Append-only CSV ============
CSV_FIELDS = ["ts","winner_id","loser_id","winner_elo_before","winner_elo_after","loser_elo_before","loser_elo_after"]

def ensure_log_csv():
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

def read_log_df() -> pd.DataFrame:
    ensure_log_csv()
    try:
        df = pd.read_csv(LOG_CSV)
        # basic sanity
        if not set(CSV_FIELDS).issubset(df.columns):
            return pd.DataFrame(columns=CSV_FIELDS)
        return df
    except Exception:
        # corrupt/empty -> start fresh (keep file)
        return pd.DataFrame(columns=CSV_FIELDS)

def append_decision_row(row: Dict):
    ensure_log_csv()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writerow(row)

# ============ Elo & Leaderboard (computed from log) ============
def compute_elo_and_counts(df_log: pd.DataFrame, K: int = 24) -> tuple[Dict[str,float], Dict[str,int], Dict[str,int]]:
    elo: Dict[str, float] = {}
    wins: Dict[str, int] = {}
    losses: Dict[str, int] = {}

    def get_elo(x: str) -> float:
        if x not in elo:
            elo[x] = 1000.0
        return elo[x]

    for _, row in df_log.sort_values("ts").iterrows():
        w = str(row["winner_id"]); l = str(row["loser_id"])
        Ew = 1.0 / (1.0 + 10 ** ((get_elo(l) - get_elo(w)) / 400.0))
        if K > 0:
            elo[w] = get_elo(w) + K * (1 - Ew)
            elo[l] = get_elo(l) - K * (1 - Ew)

        wins[w] = wins.get(w, 0) + 1
        losses[l] = losses.get(l, 0) + 1

    return elo, wins, losses

def build_leaderboard() -> pd.DataFrame:
    df_log = read_log_df()
    elo, wins, losses = compute_elo_and_counts(df_log, ELO_K)

    # Make a dataframe of stats
    ids = set(list(elo.keys()) + list(wins.keys()) + list(losses.keys()))
    stats = []
    for tid in ids:
        stats.append({
            "id": tid,
            "wins": int(wins.get(tid, 0)),
            "losses": int(losses.get(tid, 0)),
            "elo": int(round(elo.get(tid, 1000.0)))
        })
    df_stats = pd.DataFrame(stats) if stats else pd.DataFrame(columns=["id","wins","losses","elo"])

    # Join with track metadata
    if df_stats.empty:
        return df_stats

    placeholders = ",".join("?" for _ in df_stats["id"].tolist())
    with closing(db_conn()) as con:
        meta = pd.read_sql_query(
            f"SELECT id, name, artist, url FROM tracks WHERE id IN ({placeholders})",
            con, params=df_stats["id"].tolist()
        )
    out = df_stats.merge(meta, on="id", how="left")
    # Put nicer columns first
    out = out[["name","artist","url","wins","losses","elo","id"]].sort_values("elo", ascending=False, kind="mergesort")
    return out.reset_index(drop=True)

# ============ Spotify sync ============
def spotify_client() -> Spotify:
    cid = os.getenv("SPOTIPY_CLIENT_ID")
    sec = os.getenv("SPOTIPY_CLIENT_SECRET")
    redir = os.getenv("SPOTIPY_REDIRECT_URI", "http://localhost:8501/")
    if not cid or not sec:
        st.error("Missing SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET in env/.env")
        st.stop()
    auth = SpotifyOAuth(
        client_id=cid, client_secret=sec, redirect_uri=redir,
        scope="user-library-read", cache_path=TOKEN_CACHE, show_dialog=False, open_browser=True
    )
    return Spotify(auth_manager=auth)

def sync_liked_songs():
    st.write("Syncing Liked Songs from Spotify…")
    sp = spotify_client()
    offset, added = 0, 0
    pb = st.progress(0.0)
    total = None
    while True:
        resp = sp.current_user_saved_tracks(limit=PAGE_SIZE, offset=offset)
        if total is None:
            total = resp.get("total", 0)
        items = resp.get("items", [])
        if not items:
            break
        for it in items:
            tr = (it or {}).get("track") or {}
            if not tr or not tr.get("id"):
                continue
            images = (tr.get("album") or {}).get("images") or []
            img = images[0]["url"] if images else ""
            artists = ", ".join(a.get("name","") for a in (tr.get("artists") or [])) or "Unknown"
            upsert_track(Track(
                id=tr["id"],
                name=tr.get("name","Unknown"),
                artist=artists,
                url=(tr.get("external_urls") or {}).get("spotify",""),
                image=img,
                preview_url=tr.get("preview_url")
            ))
            added += 1
        offset += len(items)
        if total:
            pb.progress(min(1.0, offset/max(total,1)))
        if len(items) == 0: break
    pb.empty()
    st.success(f"Synced/updated {added} tracks.")

# ============ UI helpers ============
def pick_two_random_tracks() -> Tuple[Track, Track]:
    ids = track_ids()
    if len(ids) < 2:
        st.error("Need at least 2 tracks loaded. Sync your Liked Songs.")
        st.stop()
    a, b = random.choice(ids), random.choice(ids)
    while b == a:
        b = random.choice(ids)
    a_t, b_t = fetch_tracks([a, b])
    if random.random() < 0.5:
        a_t, b_t = b_t, a_t
    return a_t, b_t

def track_card(t: Track):
    with st.container(border=True):
        cols = st.columns([1,2])
        with cols[0]:
            if t.image: st.image(t.image, use_container_width=True)
        with cols[1]:
            st.markdown(f"**{t.name}**")
            st.caption(t.artist)
            if t.url: st.link_button("Open in Spotify", t.url, use_container_width=True)
            if t.preview_url: st.audio(t.preview_url)

def record_pick(winner: Track, loser: Track):
    # Compute on-the-fly Elo BEFORE/AFTER using current log
    df_log = read_log_df()
    elo, _, _ = compute_elo_and_counts(df_log, ELO_K)
    w_before = float(elo.get(winner.id, 1000.0))
    l_before = float(elo.get(loser.id, 1000.0))
    if ELO_K > 0:
        Ew = 1.0 / (1.0 + 10 ** ((l_before - w_before)/400.0))
        w_after = w_before + ELO_K * (1 - Ew)
        l_after = l_before - ELO_K * (1 - Ew)
    else:
        w_after, l_after = w_before, l_before

    append_decision_row({
        "ts": int(time.time()),
        "winner_id": winner.id,
        "loser_id": loser.id,
        "winner_elo_before": w_before,
        "winner_elo_after":  w_after,
        "loser_elo_before":  l_before,
        "loser_elo_after":   l_after,
    })

# ============ App ============
def main():
    st.set_page_config(page_title="Song Tournament", page_icon="🎧", layout="wide")
    st.title("🎧 Song Tournament (append-only, reliable)")
    st.caption("Two random songs from your Liked Songs go head-to-head. Every choice is logged to CSV. Leaderboard recomputes from the log, so it can’t go out of sync.")

    init_db()
    ensure_log_csv()

    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Sync Liked Songs from Spotify", use_container_width=True):
            sync_liked_songs()
            st.rerun()

        st.divider()
        st.write(f"DB: `{DB_PATH}`")
        st.write(f"Log: `{LOG_CSV}`")
        if st.download_button("⬇️ Download decisions CSV", data=open(LOG_CSV,"rb").read(),
                              file_name=os.path.basename(LOG_CSV), mime="text/csv",
                              use_container_width=True):
            pass

    tabs = st.tabs(["Matchup", "Leaderboard", "Log"])

    # Matchup
    with tabs[0]:
        if library_empty():
            st.info("No tracks yet. Click **Sync Liked Songs** in the sidebar.")
        else:
            a, b = pick_two_random_tracks()
            cols = st.columns(2)
            with cols[0]:
                track_card(a)
                if st.button("Pick this one ✅", key=f"pick_{a.id}", use_container_width=True):
                    record_pick(a, b)
                    st.success("Recorded! ✅")
                    st.rerun()
            with cols[1]:
                track_card(b)
                if st.button("Pick this one ✅", key=f"pick_{b.id}", use_container_width=True):
                    record_pick(b, a)
                    st.success("Recorded! ✅")
                    st.rerun()

    # Leaderboard
    with tabs[1]:
        df = build_leaderboard()
        if df.empty:
            st.info("No decisions yet. Make a pick on the Matchup tab.")
        else:
            sort_by = st.selectbox("Sort by", ["elo","wins","losses"], index=0)
            ascending = st.checkbox("Ascending", value=False)
            st.dataframe(df.sort_values(sort_by, ascending=ascending).reset_index(drop=True),
                         use_container_width=True)

    # Log
    with tabs[2]:
        st.subheader("Recent decisions")
        log_df = read_log_df()
        if log_df.empty:
            st.info("No decisions recorded yet.")
        else:
            # human-friendly join
            ids = pd.unique(log_df[["winner_id","loser_id"]].values.ravel("K"))
            if len(ids):
                placeholders = ",".join("?" for _ in ids.tolist())
                with closing(db_conn()) as con:
                    meta = pd.read_sql_query(
                        f"SELECT id, name, artist FROM tracks WHERE id IN ({placeholders})",
                        con, params=ids.tolist()
                    )
                name_map = dict(zip(meta["id"], meta["name"]))
                artist_map = dict(zip(meta["id"], meta["artist"]))
            else:
                name_map, artist_map = {}, {}

            def nice(r):
                return {
                    "time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(r["ts"]))),
                    "winner": f'{name_map.get(r["winner_id"], r["winner_id"])} — {artist_map.get(r["winner_id"], "")}',
                    "loser":  f'{name_map.get(r["loser_id"],  r["loser_id"])} — {artist_map.get(r["loser_id"],  "")}',
                    "w_elo_before": int(round(r.get("winner_elo_before", 1000))),
                    "w_elo_after":  int(round(r.get("winner_elo_after",  1000))),
                    "l_elo_before": int(round(r.get("loser_elo_before",  1000))),
                    "l_elo_after":  int(round(r.get("loser_elo_after",   1000))),
                }

            pretty = pd.DataFrame([nice(r) for _, r in log_df.sort_values("ts", ascending=False).head(200).iterrows()])
            st.dataframe(pretty, use_container_width=True)

if __name__ == "__main__":
    main()
