import sqlite3
import time
import requests
from plyer import notification

# --- CONFIGURATION ---
TM_API_KEY = "eVZk4A8RXeyobjYUhY7x4MeEJ9Ofb1Lo"
DB_FILE = "alerts.db"

# Default fallback location if not customized:
DEFAULT_ZIP = "50542"
DEFAULT_RADIUS = "150"  # miles


def get_lat_long_from_zip(zip_code):
  """Resolves US zip code to coordinates."""
  try:
    res = requests.get(
        f"https://api.zippopotam.us/us/{str(zip_code).strip()}", timeout=5
    )
    if res.status_code == 200:
      places = res.json().get("places", [])
      if places:
        return f"{places[0].get('latitude')},{places[0].get('longitude')}"
  except Exception:
    pass
  return None


def get_tracked_artists_from_db():
  """Finds all artists currently marked as tracked."""
  # If you want to customize your tracked list directly in code or pull from Spotify:
  # This can pull from a watchlist table or a simple list:
  return [
      "TOOL",
      "I Prevail",
      "Shinedown",
      "HARDY",
      "Falling In Reverse",
      "Set It Off",
      "Highly Suspect",
      "Silent Theory",
      "Asking Alexandria",
  ]


def send_windows_notification(artist, event_name, venue, city, state, date, url):
  """Triggers native Windows desktop alert."""
  title = f"🎟️ TicketPulse: {artist.upper()}"
  message = f"New Tour Date!\n📅 {date}\n📍 {venue} ({city}, {state})"

  try:
    notification.notify(
        title=title,
        message=message,
        app_name="TicketPulse",
        timeout=10,  # stays on screen for 10 seconds
    )
    print(f"\n[ALERT SENT] {artist} @ {venue} ({city}, {state})")
  except Exception as e:
    print(f"Failed to display desktop alert: {e}")


def run_tour_radar_check(postal_code=DEFAULT_ZIP, radius=DEFAULT_RADIUS):
  print(
      f"\n🔎 [TicketPulse Radar] Scanning for new tour dates within {radius}"
      f" miles of {postal_code}..."
  )

  coords = get_lat_long_from_zip(postal_code)
  if not coords:
    print("❌ Could not resolve GPS coordinates for zip code.")
    return

  conn = sqlite3.connect(DB_FILE)
  c = conn.cursor()

  # Ensure table exists
  c.execute("""
        CREATE TABLE IF NOT EXISTS seen_events (
            event_id TEXT PRIMARY KEY,
            artist TEXT,
            event_name TEXT,
            event_date TEXT,
            city TEXT,
            state TEXT,
            url TEXT,
            alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
  conn.commit()

  tracked_artists = get_tracked_artists_from_db()
  new_shows_found = 0

  for artist in tracked_artists:
    print(f" -> Checking {artist}...")

    # 1. Lookup verified Attraction ID
    att_url = "https://app.ticketmaster.com/discovery/v2/attractions.json"
    att_params = {"apikey": TM_API_KEY, "keyword": artist, "size": 1}

    try:
      att_res = requests.get(att_url, params=att_params, timeout=5).json()
      attractions = att_res.get("_embedded", {}).get("attractions", [])
      if not attractions:
        continue
      att_id = attractions[0].get("id")

      # 2. Check Events via Coordinates + Radius
      ev_url = "https://app.ticketmaster.com/discovery/v2/events.json"
      ev_params = {
          "apikey": TM_API_KEY,
          "attractionId": att_id,
          "latlong": coords,
          "radius": radius,
          "unit": "miles",
          "sort": "date,asc",
          "size": 5,
      }

      ev_res = requests.get(ev_url, params=ev_params, timeout=5).json()
      events = ev_res.get("_embedded", {}).get("events", [])

      for ev in events:
        ev_id = ev.get("id")
        c.execute(
            "SELECT event_id FROM seen_events WHERE event_id = ?", (ev_id,)
        )
        exists = c.fetchone()

        if not exists:
          venues = ev.get("_embedded", {}).get("venues", [])
          venue_name = venues[0].get("name", "TBD") if venues else "TBD"
          city = venues[0].get("city", {}).get("name", "") if venues else ""
          state = (
              venues[0].get("state", {}).get("stateCode", "") if venues else ""
          )
          event_date = (
              ev.get("dates", {}).get("start", {}).get("localDate", "TBD")
          )
          event_name = ev.get("name")
          url = ev.get("url", "#")

          # Save to seen_events table immediately
          c.execute(
              """
                        INSERT INTO seen_events (event_id, artist, event_name, event_date, city, state, url)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
              (ev_id, artist, event_name, event_date, city, state, url),
          )
          conn.commit()

          # Fire Windows Desktop Push Alert!
          send_windows_notification(
              artist, event_name, venue_name, city, state, event_date, url
          )
          new_shows_found += 1
          time.sleep(1)  # Brief pause between notifications

    except Exception as e:
      print(f"Error checking {artist}: {e}")

    time.sleep(0.25)  # Rate limit protection (Ticketmaster 5 req/sec cap)

  conn.close()
  print(
      f"\n Radar run complete. Found and alerted {new_shows_found} new"
      " show(s)!\n"
  )


if __name__ == "__main__":
  # Runs the scan once when executed
  run_tour_radar_check()