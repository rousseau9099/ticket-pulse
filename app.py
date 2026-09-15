import os
import re
import sqlite3
import hashlib
import secrets
from datetime import datetime, timezone

import requests
from flask import (
    Flask,
    request,
    redirect,
    render_template,
    jsonify,
    session,
    g,
)

# =========================================================
# APP CONFIG
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is required."
    )

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    SESSION_COOKIE_SAMESITE="Lax",
)

# =========================================================
# ENVIRONMENT
# =========================================================

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_API_SECRET = os.environ.get("LASTFM_API_SECRET")

TICKETMASTER_API_KEY = os.environ.get("TICKETMASTER_API_KEY")

DATABASE_PATH = os.environ.get(
    "TICKETPULSE_DB",
    "ticketpulse.db"
)

LASTFM_CALLBACK_URL = os.environ.get(
    "LASTFM_CALLBACK_URL",
    "https://getticketpulse.com/lastfm/callback"
)

TM_EVENTS_URL = (
    "https://app.ticketmaster.com/discovery/v2/events.json"
)

# =========================================================
# DATABASE
# =========================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            DATABASE_PATH,
            timeout=15,
            check_same_thread=False
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")

    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)

    if db is not None:
        db.close()


def db_execute(sql, params=()):
    db = get_db()
    cursor = db.execute(sql, params)
    db.commit()
    return cursor


def db_fetchone(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def db_fetchall(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():
    db = get_db()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT,
            bio TEXT DEFAULT '',
            avatar_url TEXT DEFAULT '',
            home_zip TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tracked_artists (
            user_id TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, artist_name),
            FOREIGN KEY(user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS follows (
            follower_id TEXT NOT NULL,
            following_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(follower_id, following_id),
            FOREIGN KEY(follower_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY(following_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS shows (
            id TEXT PRIMARY KEY,
            external_id TEXT UNIQUE,
            name TEXT NOT NULL,
            artist TEXT DEFAULT '',
            venue TEXT DEFAULT '',
            city TEXT DEFAULT '',
            state TEXT DEFAULT '',
            event_date TEXT DEFAULT '',
            url TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            genre TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS checkins (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            show_id TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            verified INTEGER DEFAULT 0,
            rating INTEGER,
            review TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(user_id, show_id),
            FOREIGN KEY(user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY(show_id)
                REFERENCES shows(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            achievement_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            icon TEXT NOT NULL,
            category TEXT NOT NULL,
            target INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS user_achievements (
            user_id TEXT NOT NULL,
            achievement_id INTEGER NOT NULL,
            unlocked_at TEXT NOT NULL,
            PRIMARY KEY(user_id, achievement_id),
            FOREIGN KEY(user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY(achievement_id)
                REFERENCES achievements(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            show_id TEXT,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY(show_id)
                REFERENCES shows(id)
                ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS post_likes (
            post_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(post_id, user_id),
            FOREIGN KEY(post_id)
                REFERENCES posts(id)
                ON DELETE CASCADE,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            post_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(post_id)
                REFERENCES posts(id)
                ON DELETE CASCADE,
            FOREIGN KEY(user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        );
        """
    )

    seed_achievements()
    db.commit()


# =========================================================
# ACHIEVEMENTS
# =========================================================

ACHIEVEMENT_DEFINITIONS = [
    ("first_show", "First Encore", "Attend your first concert.", "🏁", "concerts", 1),
    ("concerts_5", "Concert Goer", "Attend 5 concerts.", "🎟️", "concerts", 5),
    ("concerts_10", "Live Music Regular", "Attend 10 concerts.", "🎤", "concerts", 10),
    ("concerts_25", "Tour Veteran", "Attend 25 concerts.", "🔥", "concerts", 25),
    ("concerts_50", "Road Warrior", "Attend 50 concerts.", "🚗", "concerts", 50),
    ("concerts_100", "Live Music Legend", "Attend 100 concerts.", "👑", "concerts", 100),

    ("artists_5", "Fresh Ears", "See 5 different artists live.", "🎸", "artists", 5),
    ("artists_25", "Crate Digger", "See 25 different artists live.", "💿", "artists", 25),
    ("artists_50", "Music Hoarder", "See 50 different artists live.", "🎶", "artists", 50),
    ("artists_100", "Human Festival", "See 100 different artists live.", "🤘", "artists", 100),

    ("venues_5", "Venue Hopper", "Visit 5 different venues.", "🏟️", "venues", 5),
    ("venues_10", "Venue Collector", "Visit 10 different venues.", "🎪", "venues", 10),

    ("cities_3", "Weekend Warrior", "See concerts in 3 different cities.", "🗺️", "travel", 3),
    ("cities_5", "City Hopper", "See concerts in 5 different cities.", "🚙", "travel", 5),
    ("cities_10", "Road Tripper", "See concerts in 10 different cities.", "🛣️", "travel", 10),

    ("states_3", "State Hopper", "Attend concerts in 3 different states.", "🇺🇸", "travel", 3),
    ("states_5", "Touring Act", "Attend concerts in 5 different states.", "🎫", "travel", 5),
    ("states_10", "American Tour", "Attend concerts in 10 different states.", "🗽", "travel", 10),

    ("genres_3", "Genre Explorer", "Experience 3 different music genres live.", "🎧", "genres", 3),
    ("genres_5", "Musical Tourist", "Experience 5 different music genres live.", "🌎", "genres", 5),

    ("same_artist_5", "Die Hard", "See the same artist 5 times.", "❤️", "artists", 5),
    ("same_artist_10", "Roadie For Life", "See the same artist 10 times.", "🤘", "artists", 10),

    ("verified_1", "Ticket Punched", "Have your first verified ticket.", "🎟️", "tickets", 1),
    ("verified_5", "Ticket Collector", "Have 5 verified tickets.", "🎫", "tickets", 5),
    ("verified_25", "Stub Hoarder", "Have 25 verified tickets.", "📚", "tickets", 25),

    ("streak_3", "Monthly Regular", "Attend concerts in 3 consecutive months.", "🔥", "streaks", 3),
    ("streak_6", "Never Miss", "Attend concerts in 6 consecutive months.", "🔥", "streaks", 6),
    ("streak_12", "Always Something Playing", "Attend concerts in 12 consecutive months.", "🔥", "streaks", 12),
]


def seed_achievements():
    db = get_db()
    for achievement in ACHIEVEMENT_DEFINITIONS:
        db.execute(
            """
            INSERT OR IGNORE INTO achievements
            (
                achievement_key,
                name,
                description,
                icon,
                category,
                target
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            achievement,
        )
    db.commit()


# =========================================================
# USER HELPERS
# =========================================================

def current_user_id():
    return session.get("user_id")


def require_login():
    user_id = current_user_id()
    if not user_id:
        return None, (
            jsonify({
                "success": False,
                "message": "Login required."
            }),
            401
        )

    user = db_fetchone(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    )

    if not user:
        session.clear()
        return None, (
            jsonify({
                "success": False,
                "message": "Session expired."
            }),
            401
        )

    return user, None


def get_or_create_user(username):
    username = username.strip()
    existing = db_fetchone(
        "SELECT * FROM users WHERE username = ?",
        (username,)
    )
    if existing:
        return existing

    user_id = secrets.token_urlsafe(18)
    db_execute(
        """
        INSERT INTO users
        (
            id,
            username,
            display_name,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            username,
            utc_now(),
        )
    )
    return db_fetchone(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    )


def get_title(concert_count):
    if concert_count >= 100:
        return "Live Music Legend"
    if concert_count >= 50:
        return "Road Warrior"
    if concert_count >= 25:
        return "Tour Veteran"
    if concert_count >= 10:
        return "Live Music Regular"
    if concert_count >= 5:
        return "Concert Goer"
    if concert_count >= 1:
        return "Concert Newbie"
    return "New Listener"


# =========================================================
# CSRF
# =========================================================

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def csrf_protection():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if not request.path.startswith("/api/"):
        return None

    expected = session.get("csrf_token")
    provided = request.headers.get("X-CSRF-Token")

    if not expected or not provided:
        return jsonify({
            "success": False,
            "message": "Missing CSRF token."
        }), 403

    if not secrets.compare_digest(expected, provided):
        return jsonify({
            "success": False,
            "message": "Invalid CSRF token."
        }), 403

    return None


# =========================================================
# LAST.FM
# =========================================================

def get_lastfm_username(token, api_key, api_secret):
    if not token or not api_key or not api_secret:
        return None

    sig_string = (
        f"api_key{api_key}"
        f"methodauth.getSession"
        f"token{token}"
        f"{api_secret}"
    )

    api_sig = hashlib.md5(
        sig_string.encode("utf-8")
    ).hexdigest()

    payload = {
        "method": "auth.getSession",
        "api_key": api_key,
        "token": token,
        "api_sig": api_sig,
        "format": "json",
    }

    try:
        response = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params=payload,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if "session" in data:
            return data["session"]["name"]
    except Exception as exc:
        app.logger.exception("Last.fm Auth Error: %s", exc)

    return None


# =========================================================
# WEB ROUTES
# =========================================================

@app.route("/")
def index():
    lastfm_user = session.get("username")
    query_user = request.args.get("lastfm_user")
    if query_user:
        lastfm_user = query_user

    return render_template(
        "index.html",
        lastfm_user=lastfm_user or "",
        user_email=lastfm_user or "",
    )


@app.route("/login")
@app.route("/login/lastfm")
def login_lastfm():
    if not LASTFM_API_KEY:
        return "LASTFM_API_KEY is not configured.", 500

    auth_url = (
        "https://www.last.fm/api/auth/"
        f"?api_key={LASTFM_API_KEY}"
        f"&cb={LASTFM_CALLBACK_URL}"
    )
    return redirect(auth_url)


@app.route("/lastfm/callback")
def lastfm_callback():
    token = request.args.get("token")
    if not token:
        return redirect("/?error=no_token")

    username = get_lastfm_username(
        token,
        LASTFM_API_KEY,
        LASTFM_API_SECRET
    )

    if not username:
        return redirect("/?error=auth_failed")

    user = get_or_create_user(username)

    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["csrf_token"] = secrets.token_urlsafe(32)

    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# =========================================================
# API: CURRENT USER
# =========================================================

@app.route("/api/me")
def api_me():
    csrf = get_csrf_token()
    user_id = current_user_id()

    if not user_id:
        return jsonify({
            "success": True,
            "logged_in": False,
            "user": None,
            "csrf_token": csrf,
        })

    user = db_fetchone(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    )

    if not user:
        session.clear()
        return jsonify({
            "success": True,
            "logged_in": False,
            "user": None,
            "csrf_token": get_csrf_token(),
        })

    return jsonify({
        "success": True,
        "logged_in": True,
        "csrf_token": csrf,
        "user": dict(user),
    })


# =========================================================
# API: LAST.FM IMPORT
# =========================================================

@app.route("/api/import/lastfm")
def api_import_lastfm():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({
            "success": False,
            "message": "No username provided."
        }), 400

    if not LASTFM_API_KEY:
        return jsonify({
            "success": False,
            "message": "Last.fm API is not configured."
        }), 500

    payload = {
        "method": "user.getTopArtists",
        "user": username,
        "api_key": LASTFM_API_KEY,
        "limit": 15,
        "format": "json",
    }

    try:
        response = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params=payload,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            return jsonify({
                "success": False,
                "message": data.get("message", "Last.fm API error.")
            }), 400

        artists = []
        for artist in data.get("topartists", {}).get("artist", []):
            artists.append({
                "name": artist.get("name", ""),
                "category": "Top Artist",
                "subgenres": "Last.fm",
                "image": "",
            })

        return jsonify({
            "success": True,
            "artists": artists,
        })

    except Exception as exc:
        app.logger.exception("Last.fm import failed: %s", exc)
        return jsonify({
            "success": False,
            "message": "Unable to import Last.fm artists."
        }), 502


# =========================================================
# API: ARTIST TRACKING
# =========================================================

@app.route("/api/artists/tracked")
def api_tracked_artists():
    user, error = require_login()
    if error:
        return error

    rows = db_fetchall(
        """
        SELECT artist_name
        FROM tracked_artists
        WHERE user_id = ?
        ORDER BY artist_name COLLATE NOCASE
        """,
        (user["id"],)
    )

    return jsonify({
        "success": True,
        "artists": [row["artist_name"] for row in rows]
    })


@app.route("/api/artists/track", methods=["POST"])
def api_track_artist():
    user, error = require_login()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    artist = str(data.get("artist", "")).strip()

    if not artist or len(artist) > 150:
        return jsonify({
            "success": False,
            "message": "Invalid artist."
        }), 400

    db_execute(
        """
        INSERT OR IGNORE INTO tracked_artists
        (
            user_id,
            artist_name,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            user["id"],
            artist,
            utc_now(),
        )
    )

    return jsonify({
        "success": True,
        "tracked": True,
        "artist": artist,
    })


@app.route("/api/artists/track", methods=["DELETE"])
def api_untrack_artist():
    user, error = require_login()
    if error:
        return error

    artist = request.args.get("artist", "").strip()

    db_execute(
        """
        DELETE FROM tracked_artists
        WHERE user_id = ?
        AND artist_name = ?
        """,
        (
            user["id"],
            artist,
        )
    )

    return jsonify({
        "success": True,
        "tracked": False,
        "artist": artist,
    })


# =========================================================
# TICKETMASTER EVENT HELPERS
# =========================================================

def normalize_ticketmaster_event(event):
    embedded = event.get("_embedded", {})
    attractions = embedded.get("attractions", [])
    venues = embedded.get("venues", [])
    venue = venues[0] if venues else {}
    dates = event.get("dates", {})
    start = dates.get("start", {})
    city = venue.get("city", {}).get("name", "")
    state = venue.get("state", {}).get("stateCode", "")

    image_url = ""
    images = event.get("images", [])
    if images:
        images_sorted = sorted(
            images,
            key=lambda image: (
                image.get("width", 0),
                image.get("height", 0),
            ),
            reverse=True
        )
        image_url = images_sorted[0].get("url", "")

    artist_name = ""
    if attractions:
        artist_name = attractions[0].get("name", "")

    return {
        "id": event.get("id", ""),
        "name": event.get("name", ""),
        "artist": artist_name,
        "venue": venue.get("name", ""),
        "city": city,
        "state": state,
        "date": start.get("localDate", ""),
        "time": start.get("localTime", ""),
        "url": event.get("url", ""),
        "image": image_url,
        "genre": (
            event.get("classifications", [{}])[0]
            .get("genre", {})
            .get("name", "")
            if event.get("classifications")
            else ""
        ),
    }


def ticketmaster_events(params):
    if not TICKETMASTER_API_KEY:
        return [], None  # Fail gracefully with empty list rather than 503ing the UI

    params = dict(params)
    params["apikey"] = TICKETMASTER_API_KEY
    params["countryCode"] = "US"
    params["unit"] = "miles"
    # Note: Removed hardcoded classificationName="music" because 
    # some artist keywords match better without strict genre segmenting

    try:
        response = requests.get(
            TM_EVENTS_URL,
            params=params,
            timeout=8,
        )
        
        if response.status_code != 200:
            return [], None

        data = response.json()
        events = data.get("_embedded", {}).get("events", [])

        return [
            normalize_ticketmaster_event(event)
            for event in events
        ], None

    except requests.RequestException:
        return [], None


@app.route("/api/events")
def api_events():
    artist = request.args.get("artist", "").strip()
    radius = request.args.get("radius", "150")
    postal_code = request.args.get("postal_code", "").strip()
    latlong = request.args.get("latlong", "").strip()

    if not artist:
        return jsonify({"success": True, "events": []})

    try:
        radius_number = min(max(int(radius), 1), 500)
    except ValueError:
        radius_number = 150

    params = {
        "keyword": artist,
        "radius": radius_number,
        "size": 10,
        "sort": "date,asc",
    }

    if latlong and re.match(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$", latlong):
        params["latlong"] = latlong
    elif postal_code:
        params["postalCode"] = postal_code

    events, _ = ticketmaster_events(params)
    
    return jsonify({
        "success": True,
        "events": events or [],
    })

# =========================================================
# =========================================================
# API: ARTIST EVENTS
# =========================================================

@app.route("/api/events")
def api_events():
    artist = request.args.get("artist", "").strip()
    radius = request.args.get("radius", "150")
    postal_code = request.args.get("postal_code", "").strip()
    latlong = request.args.get("latlong", "").strip()

    if not artist:
        return jsonify({
            "success": False,
            "message": "Artist is required.",
            "events": [],
        }), 400

    try:
        radius_number = min(max(int(radius), 1), 500)
    except ValueError:
        radius_number = 150

    params = {
        "keyword": artist,
        "radius": radius_number,
        "size": 20,
        "sort": "date,asc",
    }
...
# =========================================================
# API: NEARBY EVENTS
# =========================================================

@app.route("/api/nearby")
def api_nearby():
    radius = request.args.get("radius", "150")
    postal_code = request.args.get("postal_code", "").strip()
    latlong = request.args.get("latlong", "").strip()

    try:
        radius_number = min(max(int(radius), 1), 500)
    except ValueError:
        radius_number = 150

    params = {
        "radius": radius_number,
        "size": 50,
        "sort": "date,asc",
    }

    if latlong:
        if re.match(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$", latlong):
            params["latlong"] = latlong
    elif postal_code:
        params["postalCode"] = postal_code
    else:
        return jsonify({
            "success": False,
            "message": "Location is required.",
            "events": [],
        }), 400

    events, error = ticketmaster_events(params)
    if error:
        return error

    return jsonify({
        "success": True,
        "events": events or [],
    })


# =========================================================
# SHOW DATABASE
# =========================================================

def save_show(event):
    show_id = event.get("id") or secrets.token_urlsafe(12)
    existing = db_fetchone(
        "SELECT * FROM shows WHERE external_id = ?",
        (show_id,)
    )
    if existing:
        return existing["id"]

    db_execute(
        """
        INSERT INTO shows
        (
            id,
            external_id,
            name,
            artist,
            venue,
            city,
            state,
            event_date,
            url,
            image_url,
            genre,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            show_id,
            show_id,
            event.get("name", ""),
            event.get("artist", ""),
            event.get("venue", ""),
            event.get("city", ""),
            event.get("state", ""),
            event.get("date", ""),
            event.get("url", ""),
            event.get("image", ""),
            event.get("genre", ""),
            utc_now(),
        )
    )
    return show_id


# =========================================================
# STATS
# =========================================================

def get_user_stats(user_id):
    concerts = db_fetchone(
        "SELECT COUNT(*) AS count FROM checkins WHERE user_id = ?",
        (user_id,)
    )["count"]

    artists = db_fetchone(
        """
        SELECT COUNT(DISTINCT LOWER(NULLIF(TRIM(s.artist), ''))) AS count
        FROM checkins c
        JOIN shows s ON s.id = c.show_id
        WHERE c.user_id = ?
        """,
        (user_id,)
    )["count"]

    venues = db_fetchone(
        """
        SELECT COUNT(DISTINCT LOWER(NULLIF(TRIM(s.venue), ''))) AS count
        FROM checkins c
        JOIN shows s ON s.id = c.show_id
        WHERE c.user_id = ?
        """,
        (user_id,)
    )["count"]

    cities = db_fetchone(
        """
        SELECT COUNT(DISTINCT LOWER(NULLIF(TRIM(s.city), ''))) AS count
        FROM checkins c
        JOIN shows s ON s.id = c.show_id
        WHERE c.user_id = ?
        """,
        (user_id,)
    )["count"]

    states = db_fetchone(
        """
        SELECT COUNT(DISTINCT LOWER(NULLIF(TRIM(s.state), ''))) AS count
        FROM checkins c
        JOIN shows s ON s.id = c.show_id
        WHERE c.user_id = ?
        """,
        (user_id,)
    )["count"]

    genres = db_fetchone(
        """
        SELECT COUNT(DISTINCT LOWER(NULLIF(TRIM(s.genre), ''))) AS count
        FROM checkins c
        JOIN shows s ON s.id = c.show_id
        WHERE c.user_id = ?
        """,
        (user_id,)
    )["count"]

    verified = db_fetchone(
        """
        SELECT COUNT(*) AS count
        FROM checkins
        WHERE user_id = ? AND verified = 1
        """,
        (user_id,)
    )["count"]

    return {
        "concerts": concerts,
        "artists": artists,
        "venues": venues,
        "cities": cities,
        "states": states,
        "genres": genres,
        "verified_tickets": verified,
        "title": get_title(concerts),
    }


# =========================================================
# STREAK
# =========================================================

def calculate_month_streak(user_id):
    rows = db_fetchall(
        """
        SELECT DISTINCT substr(s.event_date, 1, 7) AS month
        FROM checkins c
        JOIN shows s ON s.id = c.show_id
        WHERE c.user_id = ? AND s.event_date != ''
        ORDER BY month DESC
        """,
        (user_id,)
    )
    if not rows:
        return 0

    months = [row["month"] for row in rows]
    streak = 0
    current = None

    for month in months:
        year, month_number = map(int, month.split("-"))
        month_index = year * 12 + month_number

        if current is None:
            current = month_index
            streak = 1
            continue

        if month_index == current - 1:
            streak += 1
            current = month_index
        else:
            break

    return streak


# =========================================================
# ACHIEVEMENT ENGINE
# =========================================================

def achievement_progress(user_id):
    stats = get_user_stats(user_id)
    same_artist = db_fetchone(
        """
        SELECT MAX(artist_count) AS count
        FROM (
            SELECT LOWER(TRIM(s.artist)) AS artist, COUNT(*) AS artist_count
            FROM checkins c
            JOIN shows s ON s.id = c.show_id
            WHERE c.user_id = ? AND TRIM(s.artist) != ''
            GROUP BY LOWER(TRIM(s.artist))
        )
        """,
        (user_id,)
    )["count"] or 0

    streak = calculate_month_streak(user_id)

    return {
        "concerts": stats["concerts"],
        "artists": stats["artists"],
        "venues": stats["venues"],
        "cities": stats["cities"],
        "states": stats["states"],
        "genres": stats["genres"],
        "verified": stats["verified_tickets"],
        "same_artist": same_artist,
        "streak": streak,
    }


def update_achievements(user_id):
    progress = achievement_progress(user_id)
    unlocked_now = []
    achievements = db_fetchall("SELECT * FROM achievements")

    for achievement in achievements:
        key = achievement["achievement_key"]

        if key.startswith("concerts_") or key == "first_show":
            value = progress["concerts"]
        elif key.startswith("artists_"):
            value = progress["artists"]
        elif key.startswith("venues_"):
            value = progress["venues"]
        elif key.startswith("cities_"):
            value = progress["cities"]
        elif key.startswith("states_"):
            value = progress["states"]
        elif key.startswith("genres_"):
            value = progress["genres"]
        elif key.startswith("same_artist_"):
            value = progress["same_artist"]
        elif key.startswith("verified_"):
            value = progress["verified"]
        elif key.startswith("streak_"):
            value = progress["streak"]
        else:
            continue

        already = db_fetchone(
            """
            SELECT 1 FROM user_achievements
            WHERE user_id = ? AND achievement_id = ?
            """,
            (user_id, achievement["id"])
        )
        if already:
            continue

        if value >= achievement["target"]:
            db_execute(
                """
                INSERT INTO user_achievements
                (user_id, achievement_id, unlocked_at)
                VALUES (?, ?, ?)
                """,
                (user_id, achievement["id"], utc_now())
            )
            unlocked_now.append({
                "key": key,
                "name": achievement["name"],
                "description": achievement["description"],
                "icon": achievement["icon"],
                "category": achievement["category"],
            })

    return unlocked_now


# =========================================================
# API: STATS & ACHIEVEMENTS
# =========================================================

@app.route("/api/stats")
def api_stats():
    user, error = require_login()
    if error:
        return error

    stats = get_user_stats(user["id"])
    progress = achievement_progress(user["id"])

    return jsonify({
        "success": True,
        "stats": stats,
        "progress": progress,
    })


@app.route("/api/achievements")
def api_achievements():
    user, error = require_login()
    if error:
        return error

    progress = achievement_progress(user["id"])
    rows = db_fetchall(
        """
        SELECT a.*, ua.unlocked_at
        FROM achievements a
        LEFT JOIN user_achievements ua
          ON ua.achievement_id = a.id AND ua.user_id = ?
        ORDER BY a.category, a.target
        """,
        (user["id"],)
    )

    results = []
    for row in rows:
        key = row["achievement_key"]
        if key.startswith("concerts_") or key == "first_show":
            value = progress["concerts"]
        elif key.startswith("artists_"):
            value = progress["artists"]
        elif key.startswith("venues_"):
            value = progress["venues"]
        elif key.startswith("cities_"):
            value = progress["cities"]
        elif key.startswith("states_"):
            value = progress["states"]
        elif key.startswith("genres_"):
            value = progress["genres"]
        elif key.startswith("same_artist_"):
            value = progress["same_artist"]
        elif key.startswith("verified_"):
            value = progress["verified"]
        elif key.startswith("streak_"):
            value = progress["streak"]
        else:
            value = 0

        results.append({
            "key": row["achievement_key"],
            "name": row["name"],
            "description": row["description"],
            "icon": row["icon"],
            "category": row["category"],
            "target": row["target"],
            "progress": min(value, row["target"]),
            "unlocked": bool(row["unlocked_at"]),
            "unlocked_at": row["unlocked_at"],
        })

    return jsonify({
        "success": True,
        "achievements": results,
    })


# =========================================================
# API: CHECK IN
# =========================================================

@app.route("/api/checkins", methods=["POST"])
def api_checkin():
    user, error = require_login()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    event = data.get("event") or {}
    name = str(event.get("name", "")).strip()

    if not name:
        return jsonify({
            "success": False,
            "message": "Show name is required."
        }), 400

    show_id = save_show(event)
    existing = db_fetchone(
        "SELECT id FROM checkins WHERE user_id = ? AND show_id = ?",
        (user["id"], show_id)
    )

    if existing:
        return jsonify({
            "success": False,
            "message": "You've already checked in to this show."
        }), 409

    checkin_id = secrets.token_urlsafe(18)
    rating = data.get("rating")
    try:
        if rating is not None:
            rating = max(1, min(int(rating), 5))
    except (TypeError, ValueError):
        rating = None

    review = str(data.get("review", "")).strip()[:1000]

    db_execute(
        """
        INSERT INTO checkins
        (id, user_id, show_id, source, verified, rating, review, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (checkin_id, user["id"], show_id, "manual", 0, rating, review, utc_now())
    )

    unlocked = update_achievements(user["id"])

    if data.get("share"):
        post_id = secrets.token_urlsafe(18)
        db_execute(
            """
            INSERT INTO posts (id, user_id, show_id, body, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, user["id"], show_id, f"🎤 Checked in to {name}", utc_now())
        )

    return jsonify({
        "success": True,
        "checkin_id": checkin_id,
        "verified": False,
        "unlocked": unlocked,
        "stats": get_user_stats(user["id"]),
    })


# =========================================================
# API: CONCERT HISTORY & PROFILE
# =========================================================

@app.route("/api/history")
def api_history():
    user, error = require_login()
    if error:
        return error

    rows = db_fetchall(
        """
        SELECT
            c.id, c.verified, c.rating, c.review, c.created_at AS checked_in_at,
            s.id AS show_id, s.name, s.artist, s.venue, s.city, s.state,
            s.event_date, s.url, s.image_url, s.genre
        FROM checkins c
        JOIN shows s ON s.id = c.show_id
        WHERE c.user_id = ?
        ORDER BY CASE WHEN s.event_date = '' THEN c.created_at ELSE s.event_date END DESC
        LIMIT 500
        """,
        (user["id"],)
    )

    return jsonify({
        "success": True,
        "shows": [dict(row) for row in rows]
    })


@app.route("/api/profile")
def api_profile():
    user, error = require_login()
    if error:
        return error

    stats = get_user_stats(user["id"])
    achievements = db_fetchall(
        """
        SELECT a.achievement_key, a.name, a.icon, a.category, ua.unlocked_at
        FROM user_achievements ua
        JOIN achievements a ON a.id = ua.achievement_id
        WHERE ua.user_id = ?
        ORDER BY ua.unlocked_at DESC
        """,
        (user["id"],)
    )

    return jsonify({
        "success": True,
        "user": dict(user),
        "stats": stats,
        "achievements": [dict(row) for row in achievements],
    })


@app.route("/api/profile/<username>")
def api_public_profile(username):
    user = db_fetchone("SELECT * FROM users WHERE username = ?", (username,))
    if not user:
        return jsonify({
            "success": False,
            "message": "User not found."
        }), 404

    stats = get_user_stats(user["id"])
    achievements = db_fetchall(
        """
        SELECT a.achievement_key, a.name, a.icon, a.category, ua.unlocked_at
        FROM user_achievements ua
        JOIN achievements a ON a.id = ua.achievement_id
        WHERE ua.user_id = ?
        ORDER BY ua.unlocked_at DESC
        """,
        (user["id"],)
    )

    return jsonify({
        "success": True,
        "user": dict(user),
        "stats": stats,
        "achievements": [dict(row) for row in achievements],
    })


# =========================================================
# API: COMMUNITY FEED & SOCIAL
# =========================================================

@app.route("/api/community/feed")
def api_community_feed():
    user, error = require_login()
    if error:
        return error

    rows = db_fetchall(
        """
        SELECT
            p.id, p.body, p.created_at,
            u.username, u.display_name, u.avatar_url,
            s.name AS show_name, s.artist AS show_artist, s.venue AS show_venue,
            s.city AS show_city, s.event_date AS show_date,
            (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id = p.id) AS like_count,
            EXISTS (SELECT 1 FROM post_likes my_like WHERE my_like.post_id = p.id AND my_like.user_id = ?) AS liked,
            (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
        FROM posts p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN shows s ON s.id = p.show_id
        ORDER BY p.created_at DESC
        LIMIT 100
        """,
        (user["id"],)
    )

    return jsonify({
        "success": True,
        "posts": [dict(row) for row in rows]
    })


@app.route("/api/community/posts", methods=["POST"])
def api_create_post():
    user, error = require_login()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    body = str(data.get("body", "")).strip()

    if not body:
        return jsonify({
            "success": False,
            "message": "Post cannot be empty."
        }), 400

    if len(body) > 500:
        return jsonify({
            "success": False,
            "message": "Posts are limited to 500 characters."
        }), 400

    show_id = data.get("show_id")
    post_id = secrets.token_urlsafe(18)

    db_execute(
        """
        INSERT INTO posts (id, user_id, show_id, body, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (post_id, user["id"], show_id, body, utc_now())
    )

    return jsonify({
        "success": True,
        "post_id": post_id,
    })


@app.route("/api/community/posts/<post_id>/like", methods=["POST"])
def api_like_post(post_id):
    user, error = require_login()
    if error:
        return error

    existing = db_fetchone(
        "SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?",
        (post_id, user["id"])
    )

    if existing:
        db_execute(
            "DELETE FROM post_likes WHERE post_id = ? AND user_id = ?",
            (post_id, user["id"])
        )
        liked = False
    else:
        db_execute(
            "INSERT INTO post_likes (post_id, user_id, created_at) VALUES (?, ?, ?)",
            (post_id, user["id"], utc_now())
        )
        liked = True

    count = db_fetchone(
        "SELECT COUNT(*) AS count FROM post_likes WHERE post_id = ?",
        (post_id,)
    )["count"]

    return jsonify({
        "success": True,
        "liked": liked,
        "count": count,
    })


@app.route("/api/community/posts/<post_id>/comments", methods=["GET"])
def api_get_comments(post_id):
    rows = db_fetchall(
        """
        SELECT c.id, c.body, c.created_at, u.username, u.display_name, u.avatar_url
        FROM comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.post_id = ?
        ORDER BY c.created_at ASC
        """,
        (post_id,)
    )

    return jsonify({
        "success": True,
        "comments": [dict(row) for row in rows]
    })


@app.route("/api/community/posts/<post_id>/comments", methods=["POST"])
def api_add_comment(post_id):
    user, error = require_login()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    body = str(data.get("body", "")).strip()

    if not body:
        return jsonify({
            "success": False,
            "message": "Comment cannot be empty."
        }), 400

    if len(body) > 300:
        return jsonify({
            "success": False,
            "message": "Comments are limited to 300 characters."
        }), 400

    comment_id = secrets.token_urlsafe(18)
    db_execute(
        """
        INSERT INTO comments (id, post_id, user_id, body, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (comment_id, post_id, user["id"], body, utc_now())
    )

    return jsonify({
        "success": True,
        "comment_id": comment_id,
    })


@app.route("/api/users/search")
def api_user_search():
    user, error = require_login()
    if error:
        return error

    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify({"success": True, "users": []})

    rows = db_fetchall(
        """
        SELECT id, username, display_name, avatar_url
        FROM users
        WHERE username LIKE ? OR display_name LIKE ?
        ORDER BY username
        LIMIT 20
        """,
        (f"%{query}%", f"%{query}%")
    )

    return jsonify({
        "success": True,
        "users": [dict(row) for row in rows]
    })


@app.route("/api/users/<username>/follow", methods=["POST"])
def api_follow_user(username):
    user, error = require_login()
    if error:
        return error

    target = db_fetchone("SELECT * FROM users WHERE username = ?", (username,))
    if not target:
        return jsonify({
            "success": False,
            "message": "User not found."
        }), 404

    if target["id"] == user["id"]:
        return jsonify({
            "success": False,
            "message": "You cannot follow yourself."
        }), 400

    existing = db_fetchone(
        "SELECT 1 FROM follows WHERE follower_id = ? AND following_id = ?",
        (user["id"], target["id"])
    )

    if existing:
        db_execute(
            "DELETE FROM follows WHERE follower_id = ? AND following_id = ?",
            (user["id"], target["id"])
        )
        following = False
    else:
        db_execute(
            "INSERT INTO follows (follower_id, following_id, created_at) VALUES (?, ?, ?)",
            (user["id"], target["id"], utc_now())
        )
        following = True

    return jsonify({
        "success": True,
        "following": following,
    })


# =========================================================
# LEGACY & MISC ENDPOINTS
# =========================================================

@app.route("/api/wallet")
def api_wallet():
    user_id = current_user_id()
    if not user_id:
        return jsonify({"success": True, "retired": True, "points": 0})

    stats = get_user_stats(user_id)
    return jsonify({
        "success": True,
        "retired": True,
        "points": 0,
        "stats": stats,
    })


@app.route("/api/releases")
def api_releases():
    return jsonify([])


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(404)
def page_not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({
            "success": False,
            "message": "API endpoint not found.",
        }), 404

    return render_template(
        "index.html",
        lastfm_user=session.get("username", ""),
        user_email=session.get("username", ""),
    ), 404


@app.errorhandler(500)
def internal_error(error):
    if request.path.startswith("/api/"):
        return jsonify({
            "success": False,
            "message": "Internal server error.",
        }), 500

    return "Internal server error.", 500


# =========================================================
# STARTUP
# =========================================================

with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )