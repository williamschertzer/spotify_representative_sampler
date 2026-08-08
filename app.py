import csv
import io
import os
import random
import re
import string
from collections import Counter
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
SPOTSCRAPER_API_KEY = os.getenv("SPOTSCRAPER_API_KEY")
SPOTSCRAPER_BASE_URL = "https://api.spotscraper.com/v1"
STREAM_COUNT_CACHE = {}
OBSCURE_CANDIDATE_POOL_SIZE = 80
RECCOBEATS_BASE_URL = "https://api.reccobeats.com/v1"
RECCOBEATS_FEATURE_CACHE = {}
RECCOBEATS_BATCH_SIZE = 40  # The audio-features endpoint accepts at most 40 ids.
MAX_BPM_RETRIES = 5
MAX_LIBRARY_BPM_ANALYSIS = 40
PITCH_CLASSES = (
    "C", "C♯ / D♭", "D", "D♯ / E♭", "E", "F",
    "F♯ / G♭", "G", "G♯ / A♭", "A", "A♯ / B♭", "B",
)
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_TAG_CACHE = {}
MAX_LIBRARY_TAG_ANALYSIS = 40
ARTIST_GENRE_CACHE = {}
ARTIST_BATCH_SIZE = 50  # Spotify's several-artists endpoint accepts up to 50 ids.


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
    return (
        spotipy.Spotify(
            auth=token_info["access_token"],
            requests_timeout=10,
            retries=1,
            status_retries=1,
            backoff_factor=0.3,
        )
        if token_info else None
    )


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
        "popularity": track.get("popularity"),
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
    if track_id in STREAM_COUNT_CACHE:
        return STREAM_COUNT_CACHE[track_id]
    if not SPOTSCRAPER_API_KEY:
        raise RuntimeError(
            "SpotScraper is not configured. Add SPOTSCRAPER_API_KEY in Render's Environment settings."
        )
    response = requests.get(
        f"{SPOTSCRAPER_BASE_URL}/tracks/{track_id}",
        headers={"x-api-key": SPOTSCRAPER_API_KEY, "Accept": "application/json"},
        timeout=12,
    )
    if response.status_code in (401, 403):
        raise RuntimeError("SpotScraper rejected the API key. Check SPOTSCRAPER_API_KEY in Render.")
    if response.status_code == 402:
        raise RuntimeError("SpotScraper requires an active billing balance or subscription.")
    if response.status_code == 429:
        raise RuntimeError("SpotScraper's request limit was reached. Check your usage or try again later.")
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        detail = response.text[:160].strip()
        raise RuntimeError(f"SpotScraper returned an error: {detail or exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("SpotScraper returned an unreadable response. Please try again later.") from exc
    count = find_stream_count(payload)
    if count is None:
        raise RuntimeError("SpotScraper returned track data without a recognizable stream count.")
    STREAM_COUNT_CACHE[track_id] = count
    return count


def get_lastfm_tags(track):
    """Return ranked Last.fm track tags, with artist tags as a fallback."""
    if not LASTFM_API_KEY:
        raise RuntimeError("Last.fm is not configured. Add LASTFM_API_KEY in Render's Environment settings.")
    artist = track["artists"][0] if track.get("artists") else ""
    if not artist:
        return []

    def request_tags(method, **params):
        response = requests.get(
            LASTFM_API_URL,
            params={
                "method": method,
                "api_key": LASTFM_API_KEY,
                "format": "json",
                "autocorrect": 1,
                **params,
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            return []
        tags = payload.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        ranked = []
        for tag in tags:
            name = str(tag.get("name", "")).strip().lower()
            try:
                count = int(tag.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if name:
                ranked.append((name, count))
        ranked.sort(key=lambda item: item[1], reverse=True)
        strong = [name for name, count in ranked if count >= 10]
        return (strong or [name for name, _ in ranked])[:10]

    try:
        tags = request_tags("track.getTopTags", artist=artist, track=track["name"])
        return tags or request_tags("artist.getTopTags", artist=artist)
    except (requests.RequestException, ValueError):
        return []


def add_lastfm_tags(tracks, lookup_limit=MAX_LIBRARY_TAG_ANALYSIS):
    """Attach cached Last.fm tags without making any Spotify artist requests."""
    pending = [
        track for track in tracks
        if track.get("id") and track["id"] not in LASTFM_TAG_CACHE
    ][:lookup_limit]

    def fetch(track):
        return track["id"], get_lastfm_tags(track)

    if pending:
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(fetch, track) for track in pending]
            for future in as_completed(futures):
                try:
                    track_id, tags = future.result()
                    LASTFM_TAG_CACHE[track_id] = tags
                except RuntimeError:
                    raise
                except Exception:
                    continue

    for track in tracks:
        tags = LASTFM_TAG_CACHE.get(track.get("id"), [])
        track["tags"] = tags
        track["genres"] = tags  # Existing filters and templates use this field.
    return tracks


def add_artist_genres(sp, tracks):
    """Merge Spotify's own artist genres into each track's genre list.

    One request covers 50 artists, so a whole library resolves in a handful
    of calls — far broader coverage than the capped per-track tag lookups.
    Small artists often have empty genre arrays, so Last.fm tags stay in the
    list as a complement rather than being replaced.
    """
    artist_ids = [aid for track in tracks for aid in track.get("artist_ids", [])]
    pending = [aid for aid in dict.fromkeys(artist_ids) if aid not in ARTIST_GENRE_CACHE]
    try:
        for start in range(0, len(pending), ARTIST_BATCH_SIZE):
            chunk = pending[start:start + ARTIST_BATCH_SIZE]
            response = sp.artists(chunk) or {}
            for artist in response.get("artists", []):
                if artist and artist.get("id"):
                    ARTIST_GENRE_CACHE[artist["id"]] = [
                        genre.lower() for genre in artist.get("genres", [])
                    ]
    except (SpotifyException, requests.RequestException):
        pass  # Genres stay tag-only when artist lookups are unavailable.

    for track in tracks:
        merged = list(track.get("genres", []))
        for aid in track.get("artist_ids", []):
            for genre in ARTIST_GENRE_CACHE.get(aid, []):
                if genre not in merged:
                    merged.append(genre)
        track["genres"] = merged
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


def fetch_reccobeats_features_batch(spotify_ids):
    """Fetch audio features for many Spotify IDs, keyed back by Spotify ID.

    Each result's href holds the exact Spotify recording, so no title search
    or fuzzy matching is needed. IDs missing from the catalog are simply
    absent from the returned mapping.
    """
    features_by_id = {}
    for start in range(0, len(spotify_ids), RECCOBEATS_BATCH_SIZE):
        chunk = spotify_ids[start:start + RECCOBEATS_BATCH_SIZE]
        response = requests.get(
            f"{RECCOBEATS_BASE_URL}/audio-features",
            params={"ids": ",".join(chunk)},
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        for features in payload.get("content", []):
            spotify_id = spotify_track_id(features.get("href"))
            if spotify_id and isinstance(features, dict):
                features_by_id[spotify_id] = features
    return features_by_id


def get_reccobeats_audio_features(spotify_id):
    """Return ReccoBeats audio features for one exact Spotify recording."""
    try:
        features = fetch_reccobeats_features_batch([spotify_id]).get(spotify_id)
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise RuntimeError("ReccoBeats could not return audio features for this track. Please try again later.") from exc
    if not features:
        raise RuntimeError("This exact Spotify recording was not found in the ReccoBeats catalog.")
    return features


def display_audio_features(features):
    """Turn raw ReccoBeats data into readable rows for the feature analyzer."""
    excluded = {"id", "href", "isrc", "mode"}
    normalized = {
        "acousticness", "danceability", "energy", "instrumentalness",
        "liveness", "speechiness", "valence",
    }
    descriptions = {
        "acousticness": "Confidence that the track is acoustic; higher means more acoustic.",
        "danceability": "How suitable the track is for dancing based on its musical elements.",
        "energy": "Perceived intensity and activity; higher values feel more energetic.",
        "instrumentalness": "Likelihood that the track contains no vocals.",
        "liveness": "Likelihood that the recording contains a live audience.",
        "loudness": "Average perceived loudness measured in decibels (dB).",
        "speechiness": "Amount of spoken-word content in the track.",
        "tempo": "Estimated speed of the track in beats per minute.",
        "valence": "Musical positivity; higher values tend to sound happier or more upbeat.",
    }
    rows = []
    for raw_name, value in features.items():
        name = str(raw_name).lower()
        if name in excluded:
            continue
        label = name.replace("_", " ").title()
        description = descriptions.get(name, "")
        if name == "key":
            try:
                key_number = int(value)
                pitch = PITCH_CLASSES[key_number] if 0 <= key_number < len(PITCH_CLASSES) else "Unknown"
                display_value = f"{pitch} ({key_number} / 11)"
            except (TypeError, ValueError):
                display_value = str(value)
            description = "Pitch class: 0 is C, 1 is C♯/D♭, and the scale continues through 11 (B)."
        elif name == "tempo":
            try:
                display_value = f"{float(value):.2f} BPM"
            except (TypeError, ValueError):
                display_value = f"{value} BPM"
            label = "Tempo (BPM)"
        elif name in normalized:
            try:
                display_value = f"{float(value):.2f} / 1.00"
            except (TypeError, ValueError):
                display_value = str(value)
        elif name == "loudness":
            try:
                display_value = f"{float(value):.2f} dB"
            except (TypeError, ValueError):
                display_value = f"{value} dB"
        elif isinstance(value, float):
            display_value = f"{value:.2f}"
        else:
            display_value = value
        rows.append({"name": label, "value": display_value, "description": description})
    return rows


def add_reccobeats_features(tracks):
    """Attach batch ReccoBeats features to tracks and skip unavailable songs."""
    uncached_ids = [
        track["id"] for track in tracks
        if track.get("id") and track["id"] not in RECCOBEATS_FEATURE_CACHE
    ]
    # A failed chunk only loses its own 40 tracks; the rest still resolve.
    for start in range(0, len(uncached_ids), RECCOBEATS_BATCH_SIZE):
        chunk = uncached_ids[start:start + RECCOBEATS_BATCH_SIZE]
        try:
            RECCOBEATS_FEATURE_CACHE.update(fetch_reccobeats_features_batch(chunk))
        except (requests.RequestException, ValueError, TypeError):
            continue

    enriched = []
    for track in tracks:
        features = RECCOBEATS_FEATURE_CACHE.get(track.get("id"))
        if features:
            track["audio_features"] = features
            track["bpm"] = features.get("tempo")
            enriched.append(track)
    return enriched


def liked_song_statistics(tracks, featured_tracks, features_attempted, keywords, target_bpm, tolerance):
    """Create funnel counts and compact distributions for the results page."""
    matching = [track for track in tracks if matches_keywords(track, keywords)]
    featured_ids = {track["id"] for track in featured_tracks}
    matching_featured = [track for track in matching if track.get("id") in featured_ids]
    bpm_matching = [
        track for track in matching_featured
        if track.get("bpm") is not None and abs(track["bpm"] - target_bpm) <= tolerance
    ]

    genre_counts = Counter(genre for track in tracks for genre in track.get("genres", []))
    top_genres = genre_counts.most_common(12)
    genre_max = top_genres[0][1] if top_genres else 1

    bpm_buckets = [
        ("Under 80", 0, 80), ("80–99", 80, 100), ("100–119", 100, 120),
        ("120–139", 120, 140), ("140–159", 140, 160), ("160+", 160, float("inf")),
    ]
    bpm_distribution = []
    bpm_max = 1
    for label, minimum, maximum in bpm_buckets:
        bucket_count = sum(
            1 for track in featured_tracks
            if track.get("bpm") is not None and minimum <= track["bpm"] < maximum
        )
        bpm_distribution.append({"label": label, "count": bucket_count})
        bpm_max = max(bpm_max, bucket_count)

    return {
        "total_liked": len(tracks),
        "genre_matching": len(matching),
        "genre_featured": len(matching_featured),
        "bpm_matching": len(bpm_matching),
        "features_available": len(featured_tracks),
        "features_attempted": features_attempted,
        "genre_coverage": sum(1 for track in tracks if track.get("genres")),
        "genre_distribution": [
            {"label": genre, "count": count, "percent": round(count / genre_max * 100, 1)}
            for genre, count in top_genres
        ],
        "bpm_distribution": [
            {**bucket, "percent": round(bucket["count"] / bpm_max * 100, 1)}
            for bucket in bpm_distribution
        ],
    }


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


def search_catalog(sp, keywords, desired_count, market=None, search_round=0, include_tags=True):
    """Collect a varied catalog candidate pool using Spotify Search."""
    text_query = " ".join(keywords).strip()
    candidates = {}
    attempts = max(3, min(10, (desired_count + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE + 1))
    for attempt in range(attempts):
        if keywords:
            # The genre field filter matches Spotify's artist genre taxonomy
            # instead of free text. Rotating through the keywords keeps
            # multi-keyword pools varied across attempts and rounds.
            keyword = keywords[(search_round + attempt) % len(keywords)]
            queries = [f'genre:"{keyword.lower()}"', text_query]
        else:
            # A short random prefix gives the random option different results each run.
            queries = [random.choice(string.ascii_lowercase)]
        offset = (
            random.randint(0, 99) * SEARCH_PAGE_SIZE
            if not text_query
            else (search_round * attempts + attempt) * SEARCH_PAGE_SIZE
        )
        items = []
        # Keywords outside Spotify's genre taxonomy return nothing from the
        # genre filter, so free text remains the fallback query.
        for search_query in queries:
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
            if items:
                break
        for item in items:
            track = track_from_spotify(item)
            if track["id"]:
                candidates[track["id"]] = track
        if not items or len(candidates) >= desired_count:
            break
    tracks = list(candidates.values())
    if include_tags and LASTFM_API_KEY:
        add_lastfm_tags(tracks)
    return add_artist_genres(sp, tracks) if include_tags else tracks


def matches_keywords(track, keywords):
    if not keywords:
        return True
    haystack = " ".join([
        track["name"], *track["artists"], track["album"], *track.get("genres", [])
    ]).lower()
    # Boundaries treat hyphens as part of the word, so "pop" no longer
    # matches "k-pop" or "synth-pop" while "pop punk" and "art pop" still do.
    return any(
        re.search(rf"(?<![\w-]){re.escape(keyword.lower().strip())}(?![\w-])", haystack)
        for keyword in keywords
    )


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
        "obscure_audio_features": None,
        "audio_track": None,
        "audio_features": None,
        "playlist_stats": None,
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
    try:
        count = max(1, min(100, int(request.form.get("num_tracks", "20"))))
        target_bpm = float(request.form["target_bpm"]) if request.form.get("target_bpm") else None
        bpm_tolerance = max(0, float(request.form.get("bpm_tolerance", "5")))
    except ValueError:
        return render_home(error="Track count, BPM, and BPM tolerance must be valid numbers.")

    if not keywords:
        return render_home(error="Add at least one genre or keyword.")
    if target_bpm is None:
        return render_home(error="Enter a target BPM.")
    if source == "liked" and not LASTFM_API_KEY:
        return render_home(error="Liked Songs genre analysis needs LASTFM_API_KEY configured in Render.")

    # Liked Songs are loaded once. Catalog searches request fresh pages on each
    # replacement round so failed BPM candidates are replaced with new tracks.
    liked_candidates = []
    all_liked_tracks = []
    library_featured = []
    library_analysis_count = 0
    if source == "liked":
        all_liked_tracks = get_liked_tracks(sp)
        random.shuffle(all_liked_tracks)
        add_lastfm_tags(all_liked_tracks)
        # Artist genres cover the whole library in a few batched requests,
        # so keyword filtering is not limited to the capped tag sample.
        add_artist_genres(sp, all_liked_tracks)
        liked_candidates = [track for track in all_liked_tracks if matches_keywords(track, keywords)]

        # Batch feature lookups cover 40 songs per request; the cap keeps the
        # library sample bounded, and coverage is reported on the results page.
        nonmatching = [track for track in all_liked_tracks if track not in liked_candidates]
        analysis_pool = (liked_candidates + nonmatching)[:MAX_LIBRARY_BPM_ANALYSIS]
        library_analysis_count = len(analysis_pool)
        library_featured = add_reccobeats_features(analysis_pool)

    eligible = {}
    checked_ids = set()
    retries_used = 0
    candidate_batch_size = max(10, min(30, count * 2))
    for attempt in range(MAX_BPM_RETRIES + 1):
        if len(eligible) >= count:
            break
        if source == "liked":
            new_candidates = [
                track for track in liked_candidates if track.get("id") not in checked_ids
            ][:candidate_batch_size]
        else:
            catalog_tracks = search_catalog(
                sp, keywords, candidate_batch_size, search_round=attempt
            )
            new_candidates = [
                track for track in catalog_tracks
                if matches_keywords(track, keywords) and track.get("id") not in checked_ids
            ][:candidate_batch_size]

        if not new_candidates:
            break
        checked_ids.update(track["id"] for track in new_candidates)
        featured_tracks = add_reccobeats_features(new_candidates)
        for track in featured_tracks:
            bpm = track.get("bpm")
            if bpm is None:
                continue
            if target_bpm is None or abs(bpm - target_bpm) <= bpm_tolerance:
                eligible[track["id"]] = track
        if len(eligible) < count and attempt < MAX_BPM_RETRIES:
            retries_used += 1

    filtered = list(eligible.values())
    selected = choose_tracks(filtered, count)
    playlist_stats = None
    if source == "liked":
        featured_by_id = {track["id"]: track for track in library_featured}
        featured_by_id.update({
            track["id"]: track for track in liked_candidates if track.get("audio_features")
        })
        playlist_stats = liked_song_statistics(
            all_liked_tracks,
            list(featured_by_id.values()),
            library_analysis_count,
            keywords,
            target_bpm,
            bpm_tolerance,
        )
    if not selected:
        return render_home(error=(
            "No songs had both matching keywords and available ReccoBeats features in that BPM range. "
            "Try broader keywords or a wider BPM tolerance."
        ), playlist_stats=playlist_stats)

    label = ", ".join(keywords)
    playlist_name = request.form.get("playlist_name", "").strip() or f"Discovery: {label}"
    playlist = create_playlist(sp, selected, playlist_name, f"Created from {source} songs for: {label}")
    session["csv_data"] = tracks_to_csv_bytes(selected).decode("utf-8")
    return render_home(
        message=(
            f"Created “{playlist_name}” with {len(selected)} of {count} requested songs "
            f"after checking {len(checked_ids)} candidates and using {retries_used} replacement round(s)."
        ),
        playlist_url=playlist["external_urls"]["spotify"],
        show_download=True,
        filtered_count=len(filtered),
        selected_count=len(selected),
        tracks=selected,
        playlist_stats=playlist_stats,
    )


@app.route("/find_obscure_song", methods=["POST"])
def find_obscure_song():
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("login"))
    source = request.form.get("source", "liked")
    min_popularity = request.form.get("min_popularity", "").strip()
    max_popularity = request.form.get("max_popularity", "").strip()
    try:
        popularity_floor = int(min_popularity)
        popularity_ceiling = int(max_popularity)
        if not 0 <= popularity_floor <= popularity_ceiling <= 100:
            raise ValueError
    except ValueError:
        return render_home(error=(
            "Popularity values must be whole numbers from 0 to 100, "
            "and the minimum cannot be greater than the maximum."
        ))

    tracks = get_liked_tracks(sp) if source == "liked" else search_catalog(
        sp, [], OBSCURE_CANDIDATE_POOL_SIZE, include_tags=False
    )
    candidates = [
        track for track in tracks
        if track.get("popularity") is not None
        and popularity_floor <= track["popularity"] <= popularity_ceiling
    ]
    if not candidates:
        return render_home(error=(
            f"No songs with popularity from {popularity_floor} to {popularity_ceiling} were found. "
            "Try a wider popularity range."
        ))

    track = random.choice(candidates)
    try:
        track["stream_count"] = get_stream_count(track["id"])
    except (RuntimeError, requests.RequestException) as exc:
        return render_home(error=str(exc))

    feature_rows = None
    feature_error = None
    try:
        feature_rows = display_audio_features(get_reccobeats_audio_features(track["id"]))
    except RuntimeError as exc:
        feature_error = str(exc)
    return render_home(
        message=(
            f"Randomly selected from {len(candidates)} song(s) with popularity "
            f"from {popularity_floor} to {popularity_ceiling}."
        ),
        error=feature_error,
        random_track=track,
        obscure_audio_features=feature_rows,
    )


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
        features = get_reccobeats_audio_features(track_id)
    except SpotifyException as exc:
        return render_home(error=f"Spotify could not load that track: {exc.msg or 'check the link and try again.'}")
    except RuntimeError as exc:
        return render_home(error=str(exc))
    return render_home(
        message=f"Loaded ReccoBeats audio features for “{track['name']}”.",
        audio_track=track,
        audio_features=display_audio_features(features),
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
