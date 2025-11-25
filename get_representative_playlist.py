import os
import random
import csv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Load secrets from environment variables
CLIENT_ID = "ba9358b688184d45b28d003d9d45a13d"
CLIENT_SECRET = "d632eb577681449bacb5bbad87efd629"
REDIRECT_URI = "http://127.0.0.1:5000/callback"

scope = "user-library-read playlist-modify-private playlist-modify-public"

sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=scope,
        open_browser=True,
    )
)

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
    all_artist_ids = set()
    for item in all_items:
        track = item["track"]
        track_name = track["name"]
        track_uri = track["uri"]
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
            }
        )

    # Fetch artist genres in batches of 50
    artist_genre_map = {}

    def chunker(seq, size=50):
        for pos in range(0, len(seq), size):
            yield seq[pos : pos + size]

    for batch in chunker(list(all_artist_ids), 50):
        response = sp.artists(batch)
        for artist in response["artists"]:
            artist_genre_map[artist["id"]] = artist.get("genres", [])

    # Add genres to each track
    for track in liked_tracks:
        genres_for_track = set()
        for artist_id in track["artist_ids"]:
            genres_for_track.update(artist_genre_map.get(artist_id, []))
        track["genres"] = list(genres_for_track)

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
        sp.playlist_add_items(playlist["id"], uris[i : i + 100])
    return playlist


def export_to_csv(tracks, filename="filtered_tracks.csv"):
    fieldnames = ["name", "artists", "album", "release_date", "release_year", "uri"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in tracks:
            row = {
                "name": t["name"],
                "artists": ", ".join(t["artists"]),
                "album": t["album"],
                "release_date": t["release_date"],
                "release_year": t["release_year"],
                "uri": t["uri"],
            }
            writer.writerow(row)


if __name__ == "__main__":
    # 1. Fetch liked tracks
    liked_tracks = get_liked_tracks(sp)
    print(f"Total liked tracks: {len(liked_tracks)}")

    # 2. Get user input
    raw_keywords = input("Enter keywords (comma-separated): ")
    keywords = [kw.strip() for kw in raw_keywords.split(",") if kw.strip()]

    desired_n = int(input("How many tracks do you want in the representative set? "))

    # 3. Filter by keywords
    filtered_tracks = filter_tracks_by_keywords(liked_tracks, keywords)
    print(f"Filtered down to {len(filtered_tracks)} tracks matching your keywords.")

    # 4. Choose representative subset
    selected_tracks = select_representative_subset(filtered_tracks, desired_n)
    print(f"Using {len(selected_tracks)} tracks for the playlist.")

    if selected_tracks:
        playlist_name = f"Rep sample ({len(selected_tracks)}) - {', '.join(keywords)}"
        playlist = create_playlist_for_tracks(sp, selected_tracks, playlist_name)
        print("Playlist created at:", playlist["external_urls"]["spotify"])

        # 5. Export metadata to CSV
        csv_name = "my_filtered_liked_tracks.csv"
        export_to_csv(selected_tracks, csv_name)
        print(f"Exported filtered tracks to {csv_name}.")
    else:
        print("No tracks matched your keywords; no playlist was created.")