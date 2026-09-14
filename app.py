import os
import sqlite3
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import requests
import spotipy
from spotipy.cache_handler import FlaskSessionCacheHandler
from spotipy.oauth2 import SpotifyOAuth

# --- LOAD ENVIRONMENT VARIABLES ---
load_dotenv()

CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID", "9134fb3621004f549224f28c0c60a901")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET", "7c528522f7ec4d509bead004491cfee6")
REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI", "https://ticketpulse-4gii.onrender.com/callback")
TM_API_KEY = os.getenv("TM_API_KEY", "eVZk4A8RXeyobjYUhY7x4MeEJ9Ofb1Lo")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "ticketpulse-secret-session-key-v2-secure")

EVENT_CACHE = {}
ZIP_GEO_CACHE = {}
DB_FILE = "alerts.db"


# --- DATABASE INITIALIZATION ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT,
            artist TEXT,
            event_name TEXT,
            event_date TEXT,
            city TEXT,
            state TEXT,
            url TEXT,
            alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_wallet (
            user_id TEXT PRIMARY KEY,
            points INTEGER DEFAULT 250
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS points_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            action TEXT,
            points INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


init_db()


# --- SPOTIFY PER-USER SESSION AUTH ---
def get_auth_manager():
    # Store token strictly inside user's browser session cookie
    cache_handler = FlaskSessionCacheHandler(session)
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="user-top-read",
        cache_handler=cache_handler,
        cache_path=None,   # Explicitly disable reading or writing any .cache file on disk
        show_dialog=True   # Always prompt login so users do not auto-inherit browser sessions
    )


def get_current_user_sp():
    auth_manager = get_auth_manager()
    token = auth_manager.cache_handler.get_access_token()
    if not token or not auth_manager.validate_token(token):
        return None, auth_manager
    return spotipy.Spotify(auth_manager=auth_manager), auth_manager


def get_user_id(sp):
    if "spotify_user_id" in session:
        return session["spotify_user_id"]
    try:
        user_data = sp.current_user()
        uid = user_data.get("id")
        session["spotify_user_id"] = uid
        return uid
    except Exception:
        return "anonymous_user"


def ensure_user_wallet_seeded(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM user_wallet WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO user_wallet (user_id, points) VALUES (?, 250)", (user_id,))
        c.execute("INSERT INTO points_history (user_id, action, points) VALUES (?, 'Welcome Bonus', 250)", (user_id,))
        conn.commit()
    conn.close()


# --- LOCATION & GENRE UTILITIES ---
def get_lat_long_from_zip(zip_code):
    zip_str = str(zip_code).strip()
    if not zip_str:
        return None

    if zip_str in ZIP_GEO_CACHE:
        return ZIP_GEO_CACHE[zip_str]

    try:
        res = requests.get(f"https://api.zippopotam.us/us/{zip_str}", timeout=4)
        if res.status_code == 200:
            places = res.json().get("places", [])
            if places:
                lat = places[0].get("latitude")
                lon = places[0].get("longitude")
                latlong_str = f"{lat},{lon}"
                ZIP_GEO_CACHE[zip_str] = latlong_str
                return latlong_str
    except Exception:
        pass

    return None


def categorize_genres(genre_list):
    text = " ".join(genre_list).lower()
    if any(k in text for k in ["metal", "deathcore", "metalcore", "djent"]):
        return "Metal"
    if any(k in text for k in ["country", "americana", "bluegrass"]):
        return "Country"
    if any(k in text for k in ["rock", "grunge", "punk"]):
        return "Rock"
    if any(k in text for k in ["indie", "alternative"]):
        return "Alternative"
    if any(k in text for k in ["pop", "dance", "synth"]):
        return "Pop"
    return "Rock" if not text else "Other"


# --- APPLICATION ROUTES ---
@app.route("/")
def home():
    sp, auth_manager = get_current_user_sp()
    if not sp:
        return redirect("/login")

    user_id = get_user_id(sp)
    ensure_user_wallet_seeded(user_id)

    try:
        results = sp.current_user_top_artists(limit=30, time_range="medium_term")
    except Exception:
        session.clear()
        return redirect("/login")

    artists = []
    available_categories = set(["All"])

    for item in results.get("items", []):
        name = item.get("name")
        raw_genres = item.get("genres", [])
        primary_category = categorize_genres(raw_genres)
        available_categories.add(primary_category)
        images = item.get("images", [])
        img_url = images[0]["url"] if images else ""

        artists.append({
            "id": item.get("id"),
            "name": name,
            "category": primary_category,
            "subgenres": ", ".join(raw_genres[:2]) if raw_genres else "Alternative",
            "image": img_url
        })

    sorted_categories = ["All"] + sorted([c for c in available_categories if c != "All"])
    return render_template("index.html", artists=artists, categories=sorted_categories)


@app.route("/login")
def login():
    auth_manager = get_auth_manager()
    return redirect(auth_manager.get_authorize_url())


@app.route("/callback")
def callback():
    auth_manager = get_auth_manager()
    code = request.args.get("code")
    if code:
        auth_manager.get_access_token(code)
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/wallet", methods=["GET"])
def get_wallet():
    sp, _ = get_current_user_sp()
    if not sp:
        return jsonify({"points": 0, "history": []})

    user_id = get_user_id(sp)
    ensure_user_wallet_seeded(user_id)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM user_wallet WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    points = row[0] if row else 0

    c.execute("SELECT action, points, created_at FROM points_history WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
    history = [{"action": h[0], "points": h[1], "date": h[2]} for h in c.fetchall()]
    conn.close()
    return jsonify({"points": points, "history": history})


@app.route("/api/wallet/claim", methods=["POST"])
def claim_ticket_points():
    sp, _ = get_current_user_sp()
    if not sp:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    user_id = get_user_id(sp)
    ensure_user_wallet_seeded(user_id)

    data = request.json or {}
    event_name = data.get("event_name", "Concert Ticket")
    award = 50

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE user_wallet SET points = points + ? WHERE user_id = ?", (award, user_id))
    c.execute("INSERT INTO points_history (user_id, action, points) VALUES (?, ?, ?)", (user_id, f"Ticket Click: {event_name[:35]}", award))
    conn.commit()

    c.execute("SELECT points FROM user_wallet WHERE user_id = ?", (user_id,))
    new_balance = c.fetchone()[0]
    conn.close()

    return jsonify({"success": True, "new_balance": new_balance, "awarded": award})


@app.route("/api/wallet/redeem", methods=["POST"])
def redeem_perk():
    sp, _ = get_current_user_sp()
    if not sp:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    user_id = get_user_id(sp)
    ensure_user_wallet_seeded(user_id)

    data = request.json or {}
    cost = int(data.get("cost", 0))
    perk_name = data.get("perk_name", "Reward Perk")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM user_wallet WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    balance = row[0] if row else 0

    if balance < cost:
        conn.close()
        return jsonify({"success": False, "message": "Not enough points!"}), 400

    c.execute("UPDATE user_wallet SET points = points - ? WHERE user_id = ?", (cost, user_id))
    c.execute("INSERT INTO points_history (user_id, action, points) VALUES (?, ?, ?)", (user_id, f"Redeemed: {perk_name}", -cost))
    conn.commit()

    c.execute("SELECT points FROM user_wallet WHERE user_id = ?", (user_id,))
    new_balance = c.fetchone()[0]
    conn.close()

    return jsonify({"success": True, "new_balance": new_balance, "message": f"Unlocked {perk_name}!"})


@app.route("/api/events")
def get_artist_events():
    artist_name = request.args.get("artist", "").strip()
    postal_code = request.args.get("postal_code", "").strip()
    radius = request.args.get("radius", "150").strip()

    if not artist_name:
        return jsonify({"events": []})

    cache_key = f"{artist_name}_{postal_code}_{radius}"
    if cache_key in EVENT_CACHE:
        return jsonify({"events": EVENT_CACHE[cache_key]})

    att_url = "https://app.ticketmaster.com/discovery/v2/attractions.json"
    att_params = {"apikey": TM_API_KEY, "keyword": artist_name, "size": 3}
    attraction_id = None

    try:
        r = requests.get(att_url, params=att_params, timeout=5)
        if r.status_code == 200:
            attractions = r.json().get("_embedded", {}).get("attractions", [])
            for att in attractions:
                if att.get("name", "").strip().lower() == artist_name.strip().lower():
                    attraction_id = att.get("id")
                    break
            if not attraction_id and attractions:
                attraction_id = attractions[0].get("id")
    except Exception:
        pass

    if not attraction_id:
        EVENT_CACHE[cache_key] = []
        return jsonify({"events": []})

    events_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    ev_params = {
        "apikey": TM_API_KEY,
        "attractionId": attraction_id,
        "sort": "date,asc",
        "size": 4
    }

    if postal_code:
        coords = get_lat_long_from_zip(postal_code)
        if coords:
            ev_params["latlong"] = coords
            ev_params["radius"] = radius
            ev_params["unit"] = "miles"
        else:
            ev_params["postalCode"] = postal_code
            ev_params["radius"] = radius
            ev_params["unit"] = "miles"
    else:
        ev_params["countryCode"] = "US"

    events = []
    try:
        ev_res = requests.get(events_url, params=ev_params, timeout=5)
        if ev_res.status_code == 200:
            raw = ev_res.json().get("_embedded", {}).get("events", [])
            for ev in raw:
                venues = ev.get("_embedded", {}).get("venues", [])
                venue_name = venues[0].get("name", "TBD") if venues else "TBD"
                city = venues[0].get("city", {}).get("name", "") if venues else ""
                state = venues[0].get("state", {}).get("stateCode", "") if venues else ""

                events.append({
                    "id": ev.get("id"),
                    "name": ev.get("name"),
                    "date": ev.get("dates", {}).get("start", {}).get("localDate", "TBD"),
                    "venue": venue_name,
                    "city": city,
                    "state": state,
                    "url": ev.get("url", "#")
                })
    except Exception:
        pass

    EVENT_CACHE[cache_key] = events
    return jsonify({"events": events})


@app.route("/api/releases")
def get_releases():
    sp, _ = get_current_user_sp()
    if not sp:
        return jsonify({"releases": []})

    try:
        results = sp.current_user_top_artists(limit=10, time_range="medium_term")
        releases = []

        for item in results.get("items", []):
            artist_id = item.get("id")
            artist_name = item.get("name")
            try:
                album_data = sp.artist_albums(artist_id, album_type="album,single", limit=2)
                for alb in album_data.get("items", []):
                    releases.append({
                        "artist": artist_name,
                        "title": alb.get("name"),
                        "type": alb.get("album_type").upper(),
                        "date": alb.get("release_date"),
                        "image": alb.get("images")[0]["url"] if alb.get("images") else None,
                        "url": alb.get("external_urls", {}).get("spotify", "#")
                    })
            except Exception:
                pass

        releases.sort(key=lambda x: x["date"], reverse=True)
        return jsonify({"releases": releases})
    except Exception:
        return jsonify({"releases": []})


@app.route("/api/scan-alerts", methods=["POST"])
def scan_alerts():
    sp, _ = get_current_user_sp()
    if not sp:
        return jsonify({"message": "Unauthorized", "new_alerts": []}), 401

    user_id = get_user_id(sp)
    data = request.json or {}
    tracked_artists = data.get("artists", [])
    postal_code = data.get("postal_code", "").strip()
    radius = data.get("radius", "150").strip()

    if not tracked_artists:
        return jsonify({"message": "No artists currently tracked.", "new_alerts": []})

    coords = get_lat_long_from_zip(postal_code) if postal_code else None

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    new_alerts = []

    for artist in tracked_artists:
        att_url = "https://app.ticketmaster.com/discovery/v2/attractions.json"
        att_params = {"apikey": TM_API_KEY, "keyword": artist, "size": 1}
        att_res = requests.get(att_url, params=att_params).json()
        attractions = att_res.get("_embedded", {}).get("attractions", [])
        if not attractions:
            continue
        att_id = attractions[0].get("id")

        ev_url = "https://app.ticketmaster.com/discovery/v2/events.json"
        ev_params = {
            "apikey": TM_API_KEY,
            "attractionId": att_id,
            "sort": "date,asc",
            "size": 5
        }

        if coords:
            ev_params["latlong"] = coords
            ev_params["radius"] = radius
            ev_params["unit"] = "miles"
        elif postal_code:
            ev_params["postalCode"] = postal_code
            ev_params["radius"] = radius
            ev_params["unit"] = "miles"
        else:
            ev_params["countryCode"] = "US"

        ev_res = requests.get(ev_url, params=ev_params).json()
        events = ev_res.get("_embedded", {}).get("events", [])

        for ev in events:
            ev_id = f"{user_id}_{ev.get('id')}"
            c.execute("SELECT event_id FROM seen_events WHERE event_id = ?", (ev_id,))
            exists = c.fetchone()

            if not exists:
                venues = ev.get("_embedded", {}).get("venues", [])
                city = venues[0].get("city", {}).get("name", "") if venues else ""
                state = venues[0].get("state", {}).get("stateCode", "") if venues else ""
                venue_name = venues[0].get("name", "TBD") if venues else "TBD"
                event_date = ev.get("dates", {}).get("start", {}).get("localDate", "TBD")
                event_name = ev.get("name")
                url = ev.get("url", "#")

                c.execute("""
                    INSERT INTO seen_events (event_id, user_id, artist, event_name, event_date, city, state, url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (ev_id, user_id, artist, event_name, event_date, city, state, url))
                conn.commit()

                new_alerts.append({
                    "artist": artist,
                    "name": event_name,
                    "date": event_date,
                    "city": city,
                    "state": state,
                    "venue": venue_name,
                    "url": url
                })

    conn.close()
    return jsonify({"message": f"Scan finished. Found {len(new_alerts)} new local alerts!", "new_alerts": new_alerts})


if __name__ == "__main__":
    print("\n🚀 TicketPulse running at http://127.0.0.1:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=True)