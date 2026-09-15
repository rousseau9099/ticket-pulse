import os
import hashlib
import requests
from flask import Flask, request, redirect, render_template, jsonify

app = Flask(__name__)

# Load credentials from Render Environment Variables
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_API_SECRET = os.environ.get("LASTFM_API_SECRET")

def get_lastfm_username(token, api_key, api_secret):
    """Exchanges a Last.fm web token for an authenticated username."""
    sig_string = f"api_key{api_key}methodauth.getSessiontoken{token}{api_secret}"
    api_sig = hashlib.md5(sig_string.encode('utf-8')).hexdigest()
    
    payload = {
        'method': 'auth.getSession',
        'api_key': api_key,
        'token': token,
        'api_sig': api_sig,
        'format': 'json'
    }
    
    try:
        response = requests.get("http://ws.audioscrobbler.com/2.0/", params=payload, timeout=10)
        data = response.json()
        if 'session' in data:
            return data['session']['name']
    except Exception as e:
        print(f"Last.fm Auth Error: {e}")
    return None

# ---------------------------------------------------------
# Web Routes
# ---------------------------------------------------------

@app.route('/')
def index():
    # Renders your homepage and passes the username if returning from login
    lastfm_user = request.args.get('lastfm_user', '')
    return render_template('index.html', lastfm_user=lastfm_user)

# Catches both the old /login button and /login/lastfm so nothing 404s
@app.route('/login')
@app.route('/login/lastfm')
def login_lastfm():
    callback_url = "https://getticketpulse.com/lastfm/callback"
    auth_url = f"http://www.last.fm/api/auth/?api_key={LASTFM_API_KEY}&cb={callback_url}"
    return redirect(auth_url)

@app.route('/lastfm/callback')
def lastfm_callback():
    token = request.args.get('token')
    if not token:
        return redirect('/?error=no_token')
        
    username = get_lastfm_username(token, LASTFM_API_KEY, LASTFM_API_SECRET)
    if not username:
        return redirect('/?error=auth_failed')

    return redirect(f"/?lastfm_user={username}")

# ---------------------------------------------------------
# Support Endpoints (prevents background fetch errors)
# ---------------------------------------------------------

@app.route('/api/import/lastfm')
def api_import_lastfm():
    username = request.args.get('username')
    if not username:
        return jsonify({"success": False, "message": "No username provided"}), 400
        
    payload = {
        'method': 'user.getTopArtists',
        'user': username,
        'api_key': LASTFM_API_KEY,
        'limit': 15,
        'format': 'json'
    }
    
    try:
        response = requests.get("http://ws.audioscrobbler.com/2.0/", params=payload, timeout=10)
        data = response.json()
        
        if 'error' in data:
            return jsonify({"success": False, "message": data.get('message', 'Last.fm API error')})
            
        artists = []
        for artist in data.get('topartists', {}).get('artist', []):
            artists.append({
                "name": artist['name'],
                "category": "Top Artist",
                "subgenres": "Last.fm",
                "image": "" # Last.fm removed free image URLs, so we leave this blank
            })
            
        return jsonify({"success": True, "artists": artists})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/wallet')
def api_wallet():
    return jsonify({"points": 0, "status": "active"})

@app.route('/api/releases')
def api_releases():
    return jsonify([])

# ---------------------------------------------------------
# App Runner
# ---------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))