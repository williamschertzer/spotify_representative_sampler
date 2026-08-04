import csv
import io
import os
import random
import re
import string
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, redirect, render_template, request, send_file, session, url_for
import requests
import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5000/callback")
SCOPE = "user-library-read playlist-modify-private playlist-modify-public"
SEARCH_PAGE_SIZE = 10  # Spotify reduced the Search endpoint maximum to 10 in 2026.
MAX_ARTIST_LOOKUPS = 80  # Keep one web request below Render/Gunicorn's timeout.
ARTIST_GENRE_CACHE = {}
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = "spotify-stream-count.p.rapidapi.com"
MAX_STREAM_CHECKS = 8  # The provider's free plan has a small monthly request allowance.
RECCOBEATS_BASE_URL = "https://api.reccobeats.com/v1"


def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        show_dialog=True,
        open_browser=False,
    )


def get_token():
    token_info = session.get("token_info")
    if not token_info:
        return None
    oauth = get_spotify_oauth()
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
        session["token_info"] = token_info
    return token_info


def get_spotify_client():
    token_info = get_token()
    return spotipy.Spotify(auth=token_info["access_token"], requests_timeout=10) if token_info else None


def track_from_spotify(track):
    """Turn Spotify's nested object into the small shape used by our template."""
    album = track.get("album") or {}
    release_date = album.get("release_date", "")
    return {
        "id": track.get("id"),
        "name": track.get("name", "Unknown track"),
        "artists": [artist.get("name", "Unknown artist") for artist in track.get("artists", [])],
        "artist_ids": [artist["id"] for artist in track.get("artists", []) if artist.get("id")],
        "album": album.get("name", ""),
        "release_date": release_date,
        "release_year": release_date.split("-")[0] if release_date else "",
        "uri": track.get("uri"),
        "url": (track.get("external_urls") or {}).get("spotify", ""),
        "genres": [],
        "bpm": None,
    }


def spotify_track_id(value):
    """Extract a Spotify track ID from a link, URI, or bare ID."""
    value = (value or "").strip()
    match = re.search(r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]{22})", value)
    if match:
        return match.group(1)
    return value if re.fullmatch(r"[A-Za-z0-9]{22}", value) else None


def find_stream_count(payload):
    """Read the count while tolerating small response-shape changes by the provider."""
    if isinstance(payload, bool):
        return None
    if isinstance(payload, (int, float)):
        return int(payload)
    if isinstance(payload, str):
        cleaned = payload.replace(",", "").strip()
        return int(cleaned) if cleaned.isdigit() else None
    if isinstance(payload, dict):
        preferred_keys = ("streamCount", "stream_count", "streams", "playcount", "count", "value")
        for key in preferred_keys:
            if key in payload:
                count = find_stream_count(payload[key])
                if count is not None:
                    return count
        for value in payload.values():
            count = find_stream_count(value)
            if count is not None:
                return count
    if isinstance(payload, list):
        for value in reversed(payload):
            count = find_stream_count(value)
            if count is not None:
                return count
    return None


def get_stream_count(track_id):
    if not RAPIDAPI_KEY:
        raise RuntimeError("RapidAPI is not configured. Add RAPIDAPI_KEY in Render's Environment settings.")
    response = requests.get(
        f"https://{RAPIDAPI_HOST}/v1/spotify/tracks/{track_id}/streams/current",
        headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST},
        timeout=12,
    )
    if response.status_code == 429:
        raise RuntimeError("The RapidAPI request allowance has been reached. Check your plan or try again later.")
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        detail = response.text[:160].strip()
        raise RuntimeError(f"The stream-count service returned an error: {detail or exc}") from exc
    count = find_stream_count(response.json())
    if count is None:
        raise RuntimeError("The stream-count service returned an unfamiliar response without a stream count.")
    return count


def add_artist_genres(sp, tracks):
    """Fetch a bounded set of artists concurrently and cache their genres.

    Spotify removed the bulk-artists endpoint, so fetching artists one at a time
    for a large library can exceed a web server's request timeout. A small worker
    pool makes independent lookups overlap, while the cap keeps the request fast.
    """
    artist_ids = list(dict.fromkeys(
        artist_id for track in tracks for artist_id in track["artist_ids"]
    ))
    uncached_ids = [artist_id for artist_id in artist_ids if artist_id not in ARTIST_GENRE_CACHE]
    uncached_ids = uncached_ids[:MAX_ARTIST_LOOKUPS]

    def fetch_genres(artist_id):
        try:
            return artist_id, sp.artist(artist_id).get("genres", [])
        except Exception:
            # A missing artist or temporary API failure should not break the playlist.
            return artist_id, []

    if uncached_ids:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(fetch_genres, artist_id) for artist_id in uncached_ids]
            for future in as_completed(futures):
                artist_id, genres = future.result()
                ARTIST_GENRE_CACHE[artist_id] = genres

    for track in tracks:
        track["genres"] = sorted({
            genre
            for artist_id in track["artist_ids"]
            for genre in ARTIST_GENRE_CACHE.get(artist_id, [])
        })
    return tracks


def add_audio_features(sp, tracks):
    """Add BPM where Spotify still grants audio-features access.

    Spotify removed this endpoint for Development Mode apps in 2026. Returning
    False lets the route show an honest, useful error instead of crashing.
    """
    ids = [track["id"] for track in tracks if track.get("id")]
    if not ids:
        return False
    try:
        features = []
        for start in range(0, len(ids), 100):
            features.extend(sp.audio_features(ids[start:start + 100]) or [])
    except (SpotifyException, AttributeError):
        return False

    bpm_by_id = {feature["id"]: feature.get("tempo") for feature in features if feature}
    for track in tracks:
        track["bpm"] = bpm_by_id.get(track.get("id"))
    return bool(bpm_by_id)


def get_reccobeats_audio_features(track, spotify_id):
    """Resolve an exact Spotify recording and return ReccoBeats audio features."""
    # Titles can have covers and remasters. Comparing the Spotify ID in href
    # guarantees that we choose the exact recording supplied by the user.
    match = None
    page = 0
    total_pages = 1
    try:
        while page < min(total_pages, 8) and not match:
            search_response = requests.get(
                f"{RECCOBEATS_BASE_URL}/track/search",
                params={"searchText": track["name"], "page": page},
                timeout=12,
            )
            search_response.raise_for_status()
            payload = search_response.json()
            results = payload.get("content", [])
            match = next(
                (result for result in results if spotify_track_id(result.get("href")) == spotify_id),
                None,
            )
            total_pages = max(1, int(payload.get("totalPages", 1)))
            page += 1
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise RuntimeError("ReccoBeats could not search for this track. Please try again later.") from exc
    if not match:
        raise RuntimeError("This exact Spotify recording was not found in the ReccoBeats catalog.")

    try:
        feature_response = requests.get(
            f"{RECCOBEATS_BASE_URL}/track/{match['id']}/audio-features",
            timeout=12,
        )
        feature_response.raise_for_status()
        features = feature_response.json()
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise RuntimeError("ReccoBeats could not return audio features for this track.") from exc
    if not isinstance(features, dict) or not features:
        raise RuntimeError("ReccoBeats returned an empty audio-feature result for this track.")
    return features


def get_liked_tracks(sp):
    tracks = []
    offset = 0
    while True:
        response = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = response.get("items", [])
        tracks.extend(track_from_spotify(item["track"]) for item in items if item.get("track"))
        if len(items) < 50:
            break
        offset += 50
    return tracks


def search_catalog(sp, keywords, desired_count, market=None):
    """Collect a varied catalog candidate pool using Spotify Search."""
    query = " ".join(keywords).strip()
    candidates = {}
    attempts = max(3, min(20, desired_count * 2))
    for attempt in range(attempts):
        # A short random prefix gives the random option different results each run.
        search_query = query or random.choice(string.ascii_lowercase)
        offset = random.randint(0, 99) * SEARCH_PAGE_SIZE if not query else attempt * SEARCH_PAGE_SIZE
        try:
            response = sp.search(
                q=search_query,
                type="track",
                limit=SEARCH_PAGE_SIZE,
                offset=offset,
                market=market,
            )
        except SpotifyException:
            # Some catalogs have fewer than the chosen random offset; retry page 1.
            response = sp.search(q=search_query, type="track", limit=SEARCH_PAGE_SIZE, offset=0, market=market)
        items = response.get("tracks", {}).get("items", [])
        for item in items:
            track = track_from_spotify(item)
            if track["id"]:
                candidates[track["id"]] = track
        if not items or len(candidates) >= desired_count * 3:
            break
    return add_artist_genres(sp, list(candidates.values()))


def matches_keywords(track, keywords):
    if not keywords:
        return True
    haystack = " ".join([
        track["name"], *track["artists"], track["album"], *track.get("genres", [])
    ]).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def choose_tracks(tracks, count):
    if len(tracks) <= count:
        random.shuffle(tracks)
        return tracks
    return random.sample(tracks, count)


def create_playlist(sp, tracks, name, description):
    """Use the current /me and /items Spotify endpoints."""
    playlist = sp._post(
        "me/playlists",
        payload={"name": name, "public": False, "description": description[:300]},
    )
    uris = [track["uri"] for track in tracks if track.get("uri")]
    for start in range(0, len(uris), 100):
        sp._post(f"playlists/{playlist['id']}/items", payload={"uris": uris[start:start + 100]})
    return playlist


def tracks_to_csv_bytes(tracks):
    output = io.StringIO()
    fields = ["name", "artists", "album", "release_date", "genres", "bpm", "url", "uri"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for track in tracks:
        writer.writerow({
            "name": track["name"],
            "artists": ", ".join(track["artists"]),
            "album": track["album"],
            "release_date": track["release_date"],
            "genres": ", ".join(track["genres"]),
            "bpm": round(track["bpm"], 1) if track.get("bpm") else "",
            "url": track["url"],
            "uri": track["uri"],
        })
    return output.getvalue().encode("utf-8")


def render_home(**context):
    defaults = {
        "logged_in": session.get("token_info") is not None,
        "message": None,
        "error": None,
        "playlist_url": None,
        "show_download": False,
        "filtered_count": None,
        "selected_count": None,
        "tracks": None,
        "random_track": None,
        "stream_checks": None,
        "audio_track": None,
        "audio_features": None,
    }
    defaults.update(context)
    return render_template("index.html", **defaults)


@app.route("/")
def index():
    return render_home()


@app.route("/login")
def login():
    return redirect(get_spotify_oauth().get_authorize_url())


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return render_home(error="Spotify did not return an authorization code.")
    session["token_info"] = get_spotify_oauth().get_access_token(code, check_cache=False)
    return redirect(url_for("index"))


@app.route("/create_playlist", methods=["POST"])
def create_playlist_route():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("login"))

    source = request.form.get("source", "liked")
    keywords = [word.strip() for word in request.form.get("keywords", "").split(",") if word.strip()]
    random_mode = request.form.get("discovery_mode") == "random"
    try:
        count = max(1, min(100, int(request.form.get("num_tracks", "20"))))
        target_bpm = float(request.form["target_bpm"]) if request.form.get("target_bpm") else None
        bpm_tolerance = max(0, float(request.form.get("bpm_tolerance", "5")))
    except ValueError:
        return render_home(error="Track count, BPM, and BPM tolerance must be valid numbers.")

    if not random_mode and not keywords:
        return render_home(error="Add at least one genre or keyword, or choose Random discovery.")

    tracks = get_liked_tracks(sp) if source == "liked" else search_catalog(sp, keywords if not random_mode else [], count)
    if source == "liked" and not random_mode and keywords:
        # Shuffle first so repeated searches do not always inspect the same first artists.
        random.shuffle(tracks)
        add_artist_genres(sp, tracks)
    filtered = [track for track in tracks if random_mode or matches_keywords(track, keywords)]

    if target_bpm is not None:
        if not add_audio_features(sp, filtered):
            return render_home(error=(
                "Spotify did not provide BPM data for this app. Spotify removed Audio Features "
                "from Development Mode in 2026; leave BPM blank or use an app with legacy access."
            ))
        filtered = [
            track for track in filtered
            if track.get("bpm") is not None and abs(track["bpm"] - target_bpm) <= bpm_tolerance
        ]

    selected = choose_tracks(filtered, count)
    if not selected:
        return render_home(error="No songs matched those settings. Try broader keywords or a wider BPM range.")

    label = "random" if random_mode else ", ".join(keywords)
    playlist_name = request.form.get("playlist_name", "").strip() or f"Discovery: {label}"
    playlist = create_playlist(sp, selected, playlist_name, f"Created from {source} songs for: {label}")
    session["csv_data"] = tracks_to_csv_bytes(selected).decode("utf-8")
    return render_home(
        message=f"Created “{playlist_name}” with {len(selected)} songs from {source} songs.",
        playlist_url=playlist["external_urls"]["spotify"],
        show_download=True,
        filtered_count=len(filtered),
        selected_count=len(selected),
        tracks=selected,
    )


@app.route("/find_obscure_song", methods=["POST"])
def find_obscure_song():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("login"))
    source = request.form.get("source", "liked")
    keywords = [word.strip() for word in request.form.get("obscure_keywords", "").split(",") if word.strip()]
    random_mode = request.form.get("obscure_mode") == "random"
    max_streams = request.form.get("max_streams", "").strip()
    try:
        stream_ceiling = int(max_streams)
        if stream_ceiling < 0:
            raise ValueError
    except ValueError:
        return render_home(error="Maximum streams must be zero or greater.")
    if not random_mode and not keywords:
        return render_home(error="Add a genre or keyword, or choose Surprise me.")

    tracks = get_liked_tracks(sp) if source == "liked" else search_catalog(
        sp, keywords if not random_mode else [], MAX_STREAM_CHECKS
    )
    if source == "liked" and not random_mode:
        random.shuffle(tracks)
        add_artist_genres(sp, tracks)
    candidates = [track for track in tracks if random_mode or matches_keywords(track, keywords)]
    random.shuffle(candidates)
    candidates = candidates[:MAX_STREAM_CHECKS]
    if not candidates:
        return render_home(error="No songs matched those keywords. Try a broader search.")

    checked = 0
    try:
        for track in candidates:
            checked += 1
            count = get_stream_count(track["id"])
            if count <= stream_ceiling:
                track["stream_count"] = count
                return render_home(
                    message=f"Found a song with {count:,} streams after checking {checked} candidate(s).",
                    random_track=track,
                    stream_checks=checked,
                )
    except (RuntimeError, requests.RequestException) as exc:
        return render_home(error=str(exc))
    return render_home(error=(
        f"None of the {checked} checked candidates had {stream_ceiling:,} streams or fewer. "
        "Try a higher ceiling or different keywords."
    ))


@app.route("/track_audio_features", methods=["POST"])
def track_audio_features():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("login"))
    track_id = spotify_track_id(request.form.get("track_link"))
    if not track_id:
        return render_home(error="Enter a valid Spotify track link, URI, or 22-character track ID.")
    try:
        track = track_from_spotify(sp.track(track_id))
        features = get_reccobeats_audio_features(track, track_id)
    except SpotifyException as exc:
        return render_home(error=f"Spotify could not load that track: {exc.msg or 'check the link and try again.'}")
    except RuntimeError as exc:
        return render_home(error=str(exc))
    return render_home(
        message=f"Loaded ReccoBeats audio features for “{track['name']}”.",
        audio_track=track,
        audio_features=features,
    )


@app.route("/download_csv")
def download_csv():
    csv_text = session.get("csv_data")
    if not csv_text:
        return redirect(url_for("index"))
    return send_file(
        io.BytesIO(csv_text.encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="selected_tracks.csv",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
