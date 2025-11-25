import os
import random
import csv
import io

from flask import Flask, redirect, request, session, url_for, render_template, send_file
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ---- Flask setup ----
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# ---- Spotify config (from env vars) ----
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5000/callback")
SCOPE = "user-library-read playlist-modify-private playlist-modify-public"


def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        show_dialog=True,
        open_browser=False
    )


def get_token():
    """Get token_info from session; refresh if needed."""
    token_info = session.get("token_info", None)
    if not token_info:
        return None

    sp_oauth = get_spotify_oauth()
    if sp_oauth.is_token_expired(token_info):
        token_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
        session["token_info"] = token_info
    return token_info


def get_spotify_client():
    token_info = get_token()
    if not token_info:
        return None
    access_token = token_info["access_token"]
    return spotipy.Spotify(auth=access_token)


# ---- Core logic ----

def get_liked_tracks(sp, limit=50):
    offset = 0
    all_items = []
    while True:
        response = sp.current_user_saved_tracks(limit=limit, offset=offset)
        items = response["items"]
        if not items:
            break
        all_items.extend(items)
        offset += limit

    liked_tracks = []
    all_artist_ids = set()  # To collect all unique artist IDs

    for item in all_items:
        track = item["track"]
        track_name = track["name"]
        track_uri = track["uri"]
        track_url = track["external_urls"]["spotify"]  # NEW: clickable URL

        artists = [artist["name"] for artist in track["artists"] if artist["name"]]
        artist_ids = [artist["id"] for artist in track["artists"] if artist["id"]]
        all_artist_ids.update(artist_ids)

        album_name = track["album"]["name"]
        release_date = track["album"].get("release_date", "")
        release_year = release_date.split("-")[0] if release_date else ""

        liked_tracks.append(
            {
                "name": track_name,
                "artists": artists,
                "artist_ids": artist_ids,
                "album": album_name,
                "release_date": release_date,
                "release_year": release_year,
                "uri": track_uri,
                "url": track_url,   # NEW
            }
        )

    # Fetch artist genres in batches of 50
    artist_genre_map = {}

    def chunker(seq, size=50):
        for pos in range(0, len(seq), size):
            yield seq[pos: pos + size]

    for batch in chunker(list(all_artist_ids), 50):
        response = sp.artists(batch)
        for artist in response["artists"]:
            artist_genre_map[artist["id"]] = artist.get("genres", [])

    # Add genres to each track
    for track in liked_tracks:
        genres_for_track = set()
        for artist_id in track["artist_ids"]:
            genres_for_track.update(artist_genre_map.get(artist_id, []))
        track["genres"] = list(genres_for_track)  # NEW: store genres on each track

    return liked_tracks


def filter_tracks_by_keywords(tracks, keywords):
    keywords_lower = [kw.lower().strip() for kw in keywords if kw.strip()]
    filtered = []
    for track in tracks:
        genres_text = " ".join(track.get("genres", []))
        text_to_search = (
            track["name"]
            + " "
            + " ".join(track["artists"])
            + " "
            + track["album"]
            + " "
            + genres_text
        ).lower()
        if any(kw in text_to_search for kw in keywords_lower):
            filtered.append(track)
    return filtered


def select_representative_subset(tracks, n):
    """Return up to n tracks, uniformly sampled if needed."""
    if n <= 0:
        return []
    if len(tracks) <= n:
        return tracks
    return random.sample(tracks, n)


def create_playlist_for_tracks(sp, tracks, playlist_name="Filtered Tracks"):
    user_id = sp.me()["id"]
    playlist = sp.user_playlist_create(
        user=user_id,
        name=playlist_name,
        public=False,
        description="Filtered tracks from Liked Songs",
    )
    uris = [t["uri"] for t in tracks]
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist["id"], uris[i: i + 100])
    return playlist


def tracks_to_csv_bytes(tracks):
    """Create a CSV in-memory and return bytes."""
    output = io.StringIO()
    fieldnames = [
        "name",
        "artists",
        "album",
        "release_date",
        "release_year",
        "genres",   # NEW
        "url",      # NEW
        "uri",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for t in tracks:
        row = {
            "name": t["name"],
            "artists": ", ".join(t["artists"]),
            "album": t["album"],
            "release_date": t["release_date"],
            "release_year": t["release_year"],
            "genres": ", ".join(t.get("genres", [])),
            "url": t["url"],
            "uri": t["uri"],
        }
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


# ---- Routes ----

@app.route("/")
def index():
    token_info = session.get("token_info")
    logged_in = token_info is not None
    # tracks / counts only set after a search; default to None/0 here
    return render_template(
        "index.html",
        logged_in=logged_in,
        message=None,
        playlist_url=None,
        show_download=False,
        filtered_count=None,
        selected_count=None,
        tracks=None,
    )


@app.route("/login")
def login():
    sp_oauth = get_spotify_oauth()
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)


@app.route("/callback")
def callback():
    sp_oauth = get_spotify_oauth()
    code = request.args.get("code")
    token_info = sp_oauth.get_access_token(code, check_cache=False)
    session["token_info"] = token_info
    return redirect(url_for("index"))


@app.route("/create_playlist", methods=["POST"])
def create_playlist_route():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("login"))

    raw_keywords = request.form.get("keywords", "")
    n_str = request.form.get("num_tracks", "0")
    playlist_name_input = request.form.get("playlist_name", "").strip()  # NEW

    keywords = [kw.strip() for kw in raw_keywords.split(",") if kw.strip()]
    try:
        n = int(n_str)
    except ValueError:
        n = 0

    liked_tracks = get_liked_tracks(sp)
    filtered_tracks = filter_tracks_by_keywords(liked_tracks, keywords)
    selected_tracks = select_representative_subset(filtered_tracks, n)

    if not selected_tracks:
        return render_template(
            "index.html",
            logged_in=True,
            message="No tracks matched your keywords. Try different keywords.",
            playlist_url=None,
            show_download=False,
            filtered_count=len(filtered_tracks),
            selected_count=0,
            tracks=[],
        )

    # If playlist name not provided, fall back to default
    if playlist_name_input:
        playlist_name = playlist_name_input
    else:
        playlist_name = f"Rep sample ({len(selected_tracks)}) - {', '.join(keywords)}"

    playlist = create_playlist_for_tracks(sp, selected_tracks, playlist_name)

    # Prepare CSV for download
    csv_bytes = tracks_to_csv_bytes(selected_tracks)
    session["csv_data"] = csv_bytes.decode("utf-8")  # store as string for simplicity

    message = (
        f"Found {len(filtered_tracks)} matching tracks. "
        f"Created playlist '{playlist_name}' with {len(selected_tracks)} tracks."
    )

    return render_template(
        "index.html",
        logged_in=True,
        message=message,
        playlist_url=playlist["external_urls"]["spotify"],
        show_download=True,
        filtered_count=len(filtered_tracks),
        selected_count=len(selected_tracks),
        tracks=selected_tracks,  # NEW: pass tracks to show table
    )


@app.route("/download_csv")
def download_csv():
    csv_text = session.get("csv_data")
    if not csv_text:
        return redirect(url_for("index"))
    csv_bytes = csv_text.encode("utf-8")
    return send_file(
        io.BytesIO(csv_bytes),
        mimetype="text/csv",
        as_attachment=True,
        download_name="filtered_tracks.csv",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)