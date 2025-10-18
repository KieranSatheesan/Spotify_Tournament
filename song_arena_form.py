# song_arena_form.py
# "Song Arena" with radio+submit form (rock-solid click detection)
# - Decisions append to CSV in repo root
# - Leaderboard recomputes every render
# - Liked songs cached to JSON in ~/.song_arena

import os, json, time, random, csv, uuid
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ---------- Paths / config ----------
load_dotenv()  # reads .env if present

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.expanduser("~"), ".song_arena")
os.makedirs(DATA_DIR, exist_ok=True)

LIKED_JSON = os.path.join(DATA_DIR, "liked_tracks.json")
LOG_CSV    = os.path.join(ROOT, "song_arena_decisions.csv")

ELO_K = 24
PAGE_SIZE = 50

# ---------- CSV log helpers ----------
CSV_FIELDS = ["ts","winner_id","loser_id","match_id"]

def ensure_log_file():
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

def read_log_df():
    ensure_log_file()
    try:
        df = pd.read_csv(LOG_CSV)
        if not set(CSV_FIELDS).issubset(df.columns):
            return pd.DataFrame(columns=CSV_FIELDS)
        return df
    except Exception:
        return pd.DataFrame(columns=CSV_FIELDS)

def append_decision_row(winner_id, loser_id, match_id):
    ensure_log_file()
    row = {"ts": int(time.time()), "winner_id": winner_id, "loser_id": loser_id, "match_id": match_id}
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)

# ---------- Elo + leaderboard ----------
def compute_elo_and_counts(df, K=24):
    elo, wins, losses = {}, {}, {}

    def get_elo(x):
        if x not in elo:
            elo[x] = 1000.0
        return elo[x]

    for _, r in df.sort_values("ts").iterrows():
        w, l = str(r["winner_id"]), str(r["loser_id"])
        Ew = 1.0 / (1.0 + 10 ** ((get_elo(l) - get_elo(w)) / 400.0))
        if K > 0:
            elo[w] = get_elo(w) + K * (1 - Ew)
            elo[l] = get_elo(l) - K * (1 - Ew)
        wins[w]   = wins.get(w, 0) + 1
        losses[l] = losses.get(l, 0) + 1
    return elo, wins, losses

# ---------- Spotify + cache ----------
def sp_client():
    cid = os.getenv("SPOTIPY_CLIENT_ID")
    sec = os.getenv("SPOTIPY_CLIENT_SECRET")
    redir = os.getenv("SPOTIPY_REDIRECT_URI") or "http://127.0.0.1:8888/callback"
    if not cid or not sec:
        st.error("Missing SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET in your .env.")
        st.stop()
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=cid, client_secret=sec, redirect_uri=redir,
        scope="user-library-read", cache_path=os.path.join(DATA_DIR, ".spotipy_token_cache"),
        show_dialog=False, open_browser=True
    ))

def sync_liked_to_json():
    st.info("Fetching your Liked Songs from Spotify…")
    sp = sp_client()
    out = []
    offset = 0
    total = None
    prog = st.progress(0.0)
    while True:
        resp = sp.current_user_saved_tracks(limit=PAGE_SIZE, offset=offset)
        if total is None:
            total = resp.get("total", 0)
        items = resp.get("items") or []
        if not items:
            break
        for it in items:
            tr = (it or {}).get("track") or {}
            if not tr or not tr.get("id"):
                continue
            artists = ", ".join(a.get("name","") for a in (tr.get("artists") or [])) or "Unknown"
            images  = (tr.get("album") or {}).get("images") or []
            out.append({
                "id": tr.get("id",""),
                "name": tr.get("name","Unknown"),
                "artist": artists,
                "url": (tr.get("external_urls") or {}).get("spotify",""),
                "image": images[0]["url"] if images else "",
                "preview_url": tr.get("preview_url"),
            })
        offset += len(items)
        if total:
            prog.progress(min(1.0, offset/max(total,1)))
        if len(items) == 0:
            break
    prog.empty()
    tmp = LIKED_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, LIKED_JSON)
    st.success(f"Cached {len(out)} tracks → {LIKED_JSON}")
    return out

def load_cached_tracks():
    if not os.path.exists(LIKED_JSON):
        return []
    try:
        with open(LIKED_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

# ---------- UI helpers ----------
def pick_two(tracks):
    if len(tracks) < 2:
        st.error("Need at least 2 tracks. Sync first.")
        st.stop()
    i = random.randrange(len(tracks))
    j = random.randrange(len(tracks))
    while j == i:
        j = random.randrange(len(tracks))
    a, b = tracks[i], tracks[j]
    if random.random() < 0.5:
        a, b = b, a
    return a, b

def track_card(t):
    with st.container(border=True):
        c1, c2 = st.columns([1,2])
        with c1:
            if t.get("image"):
                st.image(t["image"], use_container_width=True)
        with c2:
            st.markdown(f"**{t.get('name','Unknown')}**")
            st.caption(t.get("artist",""))
            if t.get("url"):
                st.link_button("Open in Spotify", t["url"], use_container_width=True)
            if t.get("preview_url"):
                st.audio(t["preview_url"])

# ---------- App ----------
def main():
    st.set_page_config(page_title="Song Arena (Form)", page_icon="🎧", layout="wide")
    st.title("🎧 Song Arena — form-based (reliable on Windows)")
    st.caption("Choose a winner via radio + Submit. Every submit appends a row to CSV and updates the leaderboard.")

    # Session state
    if "current_pair" not in st.session_state:
        st.session_state.current_pair = None
    if "match_id" not in st.session_state:
        st.session_state.match_id = None

    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Sync / Refresh Liked Songs", use_container_width=True):
            sync_liked_to_json()
            st.session_state.current_pair = None  # force new draw
            st.rerun()

        ensure_log_file()
        st.write(f"Log file: `{LOG_CSV}`")
        # Tail of file to prove writes
        try:
            df_log = read_log_df()
            st.caption(f"Rows in log: {len(df_log)}")
        except Exception:
            st.caption("Rows in log: (error reading)")

    tracks = load_cached_tracks()
    if len(tracks) < 2:
        st.warning("No cached liked songs yet. Click **Sync / Refresh Liked Songs**.")
        st.stop()

    tab1, tab2, tab3 = st.tabs(["Matchup", "Leaderboard", "Log"])

    # --- Matchup (form + radio) ---
    with tab1:
        # draw a new pair if none
        if st.session_state.current_pair is None:
            a, b = pick_two(tracks)
            st.session_state.current_pair = (a, b)
            st.session_state.match_id = str(uuid.uuid4())

        a, b = st.session_state.current_pair

        cols = st.columns(2)
        with cols[0]:
            track_card(a)
        with cols[1]:
            track_card(b)

        with st.form("vote_form", clear_on_submit=True):
            choice = st.radio(
                "Pick the winner",
                options=[a["id"], b["id"]],
                format_func=lambda tid: a["name"] if tid == a["id"] else b["name"],
                horizontal=True
            )
            submitted = st.form_submit_button("✅ Submit choice")
            if submitted:
                before = len(read_log_df())
                winner_id = choice
                loser_id = b["id"] if choice == a["id"] else a["id"]
                append_decision_row(winner_id, loser_id, st.session_state.match_id)
                after = len(read_log_df())
                st.success(f"Recorded: {winner_id[:6]} beat {loser_id[:6]} (rows {before} → {after}, +{after-before})")
                # draw a fresh pair & match id
                st.session_state.current_pair = None
                st.session_state.match_id = None
                st.rerun()

        if st.button("🎲 New Pair", type="secondary"):
            st.session_state.current_pair = None
            st.session_state.match_id = None
            st.rerun()

    # --- Leaderboard ---
    with tab2:
        log_df = read_log_df()
        st.caption(f"Decisions recorded: **{len(log_df)}**")
        if log_df.empty:
            st.info("No decisions yet.")
        else:
            elo, wins, losses = compute_elo_and_counts(log_df, ELO_K)
            meta = {t["id"]: t for t in tracks}
            ids = sorted(set(list(wins.keys()) + list(losses.keys()) + list(elo.keys())))
            rows = []
            for tid in ids:
                t = meta.get(tid, {})
                rows.append({
                    "name": t.get("name", tid),
                    "artist": t.get("artist", ""),
                    "wins": wins.get(tid, 0),
                    "losses": losses.get(tid, 0),
                    "elo": int(round(elo.get(tid, 1000))),
                    "url": t.get("url","")
                })
            df = pd.DataFrame(rows)
            sort_by = st.selectbox("Sort by", ["elo","wins","losses"], index=0)
            asc = st.checkbox("Ascending", value=False)
            st.dataframe(df.sort_values(sort_by, ascending=asc).reset_index(drop=True), use_container_width=True)

    # --- Log ---
    with tab3:
        df = read_log_df()
        if df.empty:
            st.info("No decisions yet.")
        else:
            meta = {t["id"]: (t["name"], t["artist"]) for t in tracks}
            def pretty(r):
                w = str(r["winner_id"]); l = str(r["loser_id"])
                wname, wart = meta.get(w, (w,""))
                lname, lart = meta.get(l, (l,""))
                return {
                    "time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(r["ts"]))),
                    "winner": f"{wname} — {wart}",
                    "loser":  f"{lname} — {lart}",
                    "match_id": r.get("match_id","")
                }
            show = pd.DataFrame([pretty(r) for _, r in df.sort_values("ts", ascending=False).head(200).iterrows()])
            st.dataframe(show, use_container_width=True)

if __name__ == "__main__":
    main()
