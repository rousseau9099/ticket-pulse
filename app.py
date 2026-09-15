import os
import sqlite3
from urllib.parse import quote, urlencode
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth, SpotifyClientCredentials

load_dotenv()

CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID", "9134fb3621004f549224f28c0c60a901")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET", "7c528522f7ec4d509bead004491cfee6")
REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI", "https://ticketpulse-4gii.onrender.com/callback")
TM_API_KEY = os.getenv("TM_API_KEY", "eVZk4A8RXeyobjYUhY7x4MeEJ9Ofb1Lo")

AFFILIATE_CAMPAIGN_ID = os.getenv("AFFILIATE_CAMPAIGN_ID", "4272")
AFFILIATE_PUB_ID = os.getenv("AFFILIATE_PUB_ID", "ticketpulse")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "ticketpulse-v3-isolated-session-98214")

EVENT_CACHE = {}
ZIP_GEO_CACHE = {}
DB_FILE = "alerts.db"

# Public Spotify client (No user OAuth required - bypasses the 250k MAU limit)
sp_public = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET
    )
)

# Preset Curated Taste Packs (Zero-Typing Onboarding)
TASTE_PACKS = {
    "rock_metal": [
        "Falling in Reverse", "The Funeral Portrait", "Asking Alexandria", 
        "Slipknot", "Avenged Sevenfold", "Motionless In White", "Bring Me The Horizon", 
        "Bad Omens", "I Prevail", "Architects"
    ],
    "alt_indie": [
        "Deftones", "Turnstile", "The Smashing Pumpkins", "Foo Fighters", 
        "Queens of the Stone Age", "Blink-182", "Paramore", "Cage the Elephant"
    ],
    "country_americana": [
        "Zach Bryan", "Tyler Childers", "Morgan Wallen", "Cody Jinks", 
        "Chris Stapleton", "Colter Wall", "Luke Combs", "Turnpike Troubadours"
    ],
    "top_touring": [
        "Metallica", "Post Malone", "Noah Kahan", "Billie Eilish", 
        "Tool", "Iron Maiden", "Shinedown", "Lainey Wilson"
    ]
}


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
            email TEXT,
            points INTEGER DEFAULT 25
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

    c.execute("PRAGMA table_info(user_wallet)")
    wallet_cols = [col[1] for col in c.fetchall()]
    if "user_id" not in wallet_cols:
        try: c.execute("ALTER TABLE user_wallet ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError: pass
    if "email" not in wallet_cols:
        try: c.execute("ALTER TABLE user_wallet ADD COLUMN email TEXT")
        except sqlite3.OperationalError: pass

    c.execute("PRAGMA table_info(points_history)")
    history_cols = [col[1] for col in c.fetchall()]
    if "user_id" not in history_cols:
        try: c.execute("ALTER TABLE points_history ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError: pass

    conn.commit()
    conn.close()

init_db()


def create_spotify_oauth():
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="user-top-read user-read-email",
        cache_path=None,
        show_dialog=True
    )


def get_current_user_sp():
    token_info = session.get("token_info", None)
    if not token_info:
        return None

    sp_oauth = create_spotify_oauth()
    if sp_oauth.is_token_expired(token_info):
        try:
            token_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
            session["token_info"] = token_info
        except Exception:
            session.clear()
            return None

    return spotipy.Spotify(auth=token_info["access_token"])


def get_user_id_and_email(sp):
    if "spotify_user_id" in session and "spotify_email" in session:
        return session["spotify_user_id"], session["spotify_email"]
    try:
        user_data = sp.current_user()
        uid = user_data.get("id", "guest_user")
        email = user_data.get("email", "")
        session["spotify_user_id"] = uid
        session["spotify_email"] = email
        return uid, email
    except Exception:
        return "guest_user", ""


def ensure_user_wallet_seeded(user_id, email=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points, email FROM user_wallet WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT OR REPLACE INTO user_wallet (user_id, email, points) VALUES (?, ?, 25)", (user_id, email))
        c.execute("INSERT INTO points_history (user_id, action, points) VALUES (?, 'Welcome Bonus', 25)", (user_id,))
        conn.commit()
    elif email and not row[1]:
        c.execute("UPDATE user_wallet SET email = ? WHERE user_id = ?", (email, user_id))
        conn.commit()
    conn.close()


def wrap_affiliate_url(target_url, user_id="guest"):
    if not target_url or target_url == "#":
        return "#"
    separator = "&" if "?" in target_url else "?"
    return f"{target_url}{separator}camefrom=CFC_BUYAT_{AFFILIATE_PUB_ID}&subid1={user_id}"


def get_lat_long_from_zip(zip_code):
    zip_str = str(zip_code).strip()
    if not zip_str: return None
    if zip_str in ZIP_GEO_CACHE: return ZIP_GEO_CACHE[zip_str]
    try:
        res = requests.get(f"https://api.zippopotam.us/us/{zip_str}", timeout=4)
        if res.status_code == 200:
            places = res.json().get("places", [])
            if places:
                lat = places[0].get("latitude")
                lon = places[0].get("longitude")
                coords = f"{lat},{lon}"
                ZIP_GEO_CACHE[zip_str] = coords
                return coords
    except Exception: pass
    return None


def categorize_genres(genre_list):
    text = " ".join(genre_list).lower()
    if any(k in text for k in ["metal", "deathcore", "metalcore", "djent"]): return "Metal"
    if any(k in text for k in ["country", "americana", "bluegrass"]): return "Country"
    if any(k in text for k in ["rock", "grunge", "punk"]): return "Rock"
    if any(k in text for k in ["indie", "alternative"]): return "Alternative"
    if any(k in text for k in ["pop", "dance", "synth"]): return "Pop"
    return "Rock" if not text else "Other"


# --- ROUTES ---

@app.route("/")
def home():
    sp = get_current_user_sp()
    artists = []
    available_categories = set(["All"])
    email = ""

    # If logged in via Spotify OAuth (for testers/admins)
    if sp:
        user_id, email = get_user_id_and_email(sp)
        ensure_user_wallet_seeded(user_id, email)
        try:
            results = sp.current_user_top_artists(limit=30, time_range="medium_term")
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
        except Exception:
            session.clear()

    sorted_categories = ["All"] + sorted([c for c in available_categories if c != "All"])
    # Render index for EVERYONE, logged in or not. Allows guest usage.
    return render_template("index.html", artists=artists, categories=sorted_categories, user_email=email)


@app.route("/api/packs/<pack_key>")
def get_taste_pack(pack_key):
    artists = TASTE_PACKS.get(pack_key, TASTE_PACKS["rock_metal"])
    pack_data = []
    for name in artists:
        img_url = ""
        category = "Rock"
        try:
            res = sp_public.search(q=name, type="artist", limit=1)
            items = res.get("artists", {}).get("items", [])
            if items:
                img_url = items[0].get("images", [{}])[0].get("url", "")
                raw_genres = items[0].get("genres", [])
                category = categorize_genres(raw_genres)
        except Exception:
            pass
        pack_data.append({
            "name": name,
            "category": category,
            "image": img_url,
            "subgenres": "Featured"
        })
    return jsonify({"artists": pack_data})


@app.route("/api/import/lastfm", methods=["GET"])
def import_lastfm():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"success": False, "message": "Username is required"}), 400
    
    LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "b25b959554ed76058ac220b7b2e0a026")
    url = f"http://ws.audioscrobbler.com/2.0/?method=user.gettopartists&user={quote(username)}&api_key={LASTFM_API_KEY}&format=json&limit=25&period=6month"
    
    try:
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return jsonify({"success": False, "message": "Could not locate Last.fm profile"}), 404
        
        raw_artists = r.json().get("topartists", {}).get("artist", [])
        artists = []
        for a in raw_artists:
            name = a.get("name")
            img_url = ""
            try:
                res = sp_public.search(q=name, type="artist", limit=1)
                items = res.get("artists", {}).get("items", [])
                if items:
                    img_url = items[0].get("images", [{}])[0].get("url", "")
            except Exception:
                pass
            
            artists.append({
                "name": name,
                "category": "Imported",
                "image": img_url,
                "subgenres": f"{a.get('playcount', 0)} plays"
            })
        return jsonify({"success": True, "artists": artists})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/import/playlist", methods=["POST"])
def import_playlist():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"success": False, "message": "URL required"}), 400

    try:
        if "spotify.com/playlist/" in url:
            playlist_id = url.split("playlist/")[1].split("?")[0]
            results = sp_public.playlist_tracks(playlist_id, limit=40)
            seen = set()
            artists = []
            for item in results.get("items", []):
                track = item.get("track")
                if not track: continue
                for art in track.get("artists", []):
                    name = art.get("name")
                    if name not in seen:
                        seen.add(name)
                        img = ""
                        try:
                            a_info = sp_public.artist(art.get("id"))
                            if a_info.get("images"): img = a_info["images"][0]["url"]
                        except Exception: pass
                        artists.append({
                            "name": name,
                            "category": "Playlist",
                            "image": img,
                            "subgenres": "In Playlist"
                        })
            return jsonify({"success": True, "artists": artists})
        else:
            return jsonify({"success": False, "message": "Only public Spotify playlist links supported currently"}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to parse playlist: {str(e)}"}), 500


@app.route("/login")
def login():
    session.clear()
    sp_oauth = create_spotify_oauth()
    return redirect(sp_oauth.get_authorize_url())


@app.route("/callback")
def callback():
    sp_oauth = create_spotify_oauth()
    session.clear()
    code = request.args.get("code")
    token_info = sp_oauth.get_access_token(code, check_cache=False)
    session["token_info"] = token_info
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/api/wallet", methods=["GET"])
def get_wallet():
    sp = get_current_user_sp()
    if not sp: return jsonify({"points": 0, "email": "", "history": []})

    user_id, email = get_user_id_and_email(sp)
    ensure_user_wallet_seeded(user_id, email)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points, email FROM user_wallet WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    points = row[0] if row else 0
    saved_email = row[1] if row and row[1] else email

    c.execute("SELECT action, points, created_at FROM points_history WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
    history = [{"action": h[0], "points": h[1], "date": h[2]} for h in c.fetchall()]
    conn.close()
    return jsonify({"points": points, "email": saved_email, "history": history})


@app.route("/api/user/email", methods=["POST"])
def save_user_email():
    sp = get_current_user_sp()
    if not sp: return jsonify({"success": False, "message": "Unauthorized"}), 401

    user_id, _ = get_user_id_and_email(sp)
    data = request.json or {}
    email = data.get("email", "").strip()

    if not email or "@" not in email:
        return jsonify({"success": False, "message": "Please enter a valid email."}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE user_wallet SET email = ? WHERE user_id = ?", (email, user_id))
    
    c.execute("SELECT id FROM points_history WHERE user_id = ? AND action LIKE '%Email Linked%'", (user_id,))
    if not c.fetchone():
        c.execute("UPDATE user_wallet SET points = points + 50 WHERE user_id = ?", (user_id,))
        c.execute("INSERT INTO points_history (user_id, action, points) VALUES (?, 'Email Linked Bonus', 50)", (user_id,))
    conn.commit()

    c.execute("SELECT points FROM user_wallet WHERE user_id = ?", (user_id,))
    new_balance = c.fetchone()[0]
    conn.close()

    session["spotify_email"] = email
    return jsonify({"success": True, "new_balance": new_balance, "message": "Email saved! +50 Points added to your wallet."})


@app.route("/api/webhooks/impact", methods=["POST", "GET"])
def impact_conversion_webhook():
    data = request.args if request.method == "GET" else (request.json or {})

    user_id = data.get("subid1")
    try: sale_amount = float(data.get("amount", 0.0))
    except (ValueError, TypeError): sale_amount = 0.0

    if not user_id or user_id in ["guest", "guest_user"]:
        return jsonify({"status": "ignored", "reason": "No valid user_id"}), 200

    points_to_award = max(int(sale_amount), 50)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE user_wallet SET points = points + ? WHERE user_id = ?", (points_to_award, user_id))
    c.execute("""
        INSERT INTO points_history (user_id, action, points)
        VALUES (?, ?, ?)
    """, (user_id, f"Verified Ticket Purchase (${sale_amount:.2f})", points_to_award))
    conn.commit()
    conn.close()

    return jsonify({"status": "success", "awarded": points_to_award, "user_id": user_id}), 200


@app.route("/api/wallet/redeem", methods=["POST"])
def redeem_perk():
    sp = get_current_user_sp()
    if not sp: return jsonify({"success": False, "message": "Unauthorized"}), 401

    user_id, _ = get_user_id_and_email(sp)
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

    return jsonify({"success": True, "new_balance": new_balance, "message": f"Claimed {perk_name}! Instructions sent to your email."})


@app.route("/api/events")
def get_artist_events():
    sp = get_current_user_sp()
    user_id = "guest"
    if sp: user_id, _ = get_user_id_and_email(sp)

    artist_name = request.args.get("artist", "").strip()
    postal_code = request.args.get("postal_code", "").strip()
    latlong = request.args.get("latlong", "").strip()
    radius = request.args.get("radius", "150").strip()

    if not artist_name: return jsonify({"events": []})

    cache_key = f"{artist_name}_{postal_code}_{latlong}_{radius}"
    if cache_key in EVENT_CACHE: return jsonify({"events": EVENT_CACHE[cache_key]})

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
    except Exception: pass

    if not attraction_id:
        EVENT_CACHE[cache_key] = []
        return jsonify({"events": []})

    events_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    ev_params = {"apikey": TM_API_KEY, "attractionId": attraction_id, "sort": "date,asc", "size": 4}

    if latlong:
        ev_params["latlong"] = latlong
        ev_params["radius"] = radius
        ev_params["unit"] = "miles"
    elif postal_code:
        coords = get_lat_long_from_zip(postal_code)
        ev_params["latlong" if coords else "postalCode"] = coords or postal_code
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
                raw_url = ev.get("url", "#")

                events.append({
                    "id": ev.get("id"),
                    "name": ev.get("name"),
                    "date": ev.get("dates", {}).get("start", {}).get("localDate", "TBD"),
                    "venue": venue_name,
                    "city": city,
                    "state": state,
                    "url": wrap_affiliate_url(raw_url, user_id)
                })
    except Exception: pass

    EVENT_CACHE[cache_key] = events
    return jsonify({"events": events})


@app.route("/api/events/nearby")
def get_nearby_events():
    sp = get_current_user_sp()
    user_id = "guest"
    if sp: user_id, _ = get_user_id_and_email(sp)

    latlong = request.args.get("latlong", "").strip()
    postal_code = request.args.get("postal_code", "").strip()
    radius = request.args.get("radius", "50").strip()

    events_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    ev_params = {
        "apikey": TM_API_KEY,
        "classificationName": "Music",
        "sort": "date,asc",
        "size": 15
    }

    if latlong:
        ev_params["latlong"] = latlong
        ev_params["radius"] = radius
        ev_params["unit"] = "miles"
    elif postal_code:
        coords = get_lat_long_from_zip(postal_code)
        ev_params["latlong" if coords else "postalCode"] = coords or postal_code
        ev_params["radius"] = radius
        ev_params["unit"] = "miles"
    else: return jsonify({"events": []})

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
                raw_url = ev.get("url", "#")

                events.append({
                    "id": ev.get("id"),
                    "name": ev.get("name"),
                    "date": ev.get("dates", {}).get("start", {}).get("localDate", "TBD"),
                    "venue": venue_name,
                    "city": city,
                    "state": state,
                    "url": wrap_affiliate_url(raw_url, user_id)
                })
    except Exception: pass

    return jsonify({"events": events})


@app.route("/api/releases")
def get_releases():
    sp = get_current_user_sp()
    if not sp: return jsonify({"releases": []})

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
            except Exception: pass

        releases.sort(key=lambda x: x["date"], reverse=True)
        return jsonify({"releases": releases})
    except Exception: return jsonify({"releases": []})


@app.route("/api/scan-alerts", methods=["POST"])
def scan_alerts():
    sp = get_current_user_sp()
    if not sp: return jsonify({"message": "Unauthorized", "new_alerts": []}), 401

    user_id, _ = get_user_id_and_email(sp)
    data = request.json or {}
    tracked_artists = data.get("artists", [])
    postal_code = data.get("postal_code", "").strip()
    radius = data.get("radius", "150").strip()

    if not tracked_artists: return jsonify({"message": "No artists currently tracked.", "new_alerts": []})
    coords = get_lat_long_from_zip(postal_code) if postal_code else None

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    new_alerts = []

    for artist in tracked_artists:
        att_url = "https://app.ticketmaster.com/discovery/v2/attractions.json"
        att_params = {"apikey": TM_API_KEY, "keyword": artist, "size": 1}
        att_res = requests.get(att_url, params=att_params).json()
        attractions = att_res.get("_embedded", {}).get("attractions", [])
        if not attractions: continue
        att_id = attractions[0].get("id")

        ev_url = "https://app.ticketmaster.com/discovery/v2/events.json"
        ev_params = {"apikey": TM_API_KEY, "attractionId": att_id, "sort": "date,asc", "size": 5}

        if coords:
            ev_params["latlong"] = coords
            ev_params["radius"] = radius
            ev_params["unit"] = "miles"
        elif postal_code:
            ev_params["postalCode"] = postal_code
            ev_params["radius"] = radius
            ev_params["unit"] = "miles"
        else: ev_params["countryCode"] = "US"

        ev_res = requests.get(ev_url, params=ev_params).json()
        events = ev_res.get("_embedded", {}).get("events", [])

        for ev in events:
            ev_id = f"{user_id}_{ev.get('id')}"
            c.execute("SELECT event_id FROM seen_events WHERE event_id = ?", (ev_id,))
            if not c.fetchone():
                venues = ev.get("_embedded", {}).get("venues", [])
                city = venues[0].get("city", {}).get("name", "") if venues else ""
                state = venues[0].get("state", {}).get("stateCode", "") if venues else ""
                venue_name = venues[0].get("name", "TBD") if venues else "TBD"
                event_date = ev.get("dates", {}).get("start", {}).get("localDate", "TBD")
                event_name = ev.get("name")
                raw_url = ev.get("url", "#")
                aff_url = wrap_affiliate_url(raw_url, user_id)

                c.execute("""
                    INSERT INTO seen_events (event_id, user_id, artist, event_name, event_date, city, state, url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (ev_id, user_id, artist, event_name, event_date, city, state, aff_url))
                conn.commit()

                new_alerts.append({
                    "artist": artist,
                    "name": event_name,
                    "date": event_date,
                    "city": city,
                    "state": state,
                    "venue": venue_name,
                    "url": aff_url
                })

    conn.close()
    return jsonify({"message": f"Scan finished. Found {len(new_alerts)} new local alerts!", "new_alerts": new_alerts})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)