import os
import hashlib
import requests
from flask import Flask, request, redirect, jsonify

app = Flask(__name__)

# Load credentials from Render
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_API_SECRET = os.environ.get("LASTFM_API_SECRET")

def get_lastfm_username(token, api_key, api_secret):
    # Sort parameters alphabetically for the Last.fm signature
    sig_string = f"api_key{api_key}methodauth.getSessiontoken{token}{api_secret}"
    api_sig = hashlib.md5(sig_string.encode('utf-8')).hexdigest()
    
    payload = {
        'method': 'auth.getSession',
        'api_key': api_key,
        'token': token,
        'api_sig': api_sig,
        'format': 'json'
    }
    
    response = requests.get("http://ws.audioscrobbler.com/2.0/", params=payload)
    data = response.json()
    
    if 'session' in data:
        return data['session']['name']
    return None

@app.route('/login/lastfm')
def login_lastfm():
    # Bounces the user to Last.fm to approve the connection
    callback_url = "https://getticketpulse.com/lastfm/callback"
    auth_url = f"http://www.last.fm/api/auth/?api_key={LASTFM_API_KEY}&cb={callback_url}"
    return redirect(auth_url)

@app.route('/lastfm/callback')
def lastfm_callback():
    # Last.fm sends them back here with a token
    token = request.args.get('token')
    
    if not token:
        return jsonify({"error": "No token provided by Last.fm"}), 400
        
    username = get_lastfm_username(token, LASTFM_API_KEY, LASTFM_API_SECRET)
    
    if not username:
        return jsonify({"error": "Failed to authenticate Last.fm session."}), 401

    # Pass the authenticated username back to your main page
    # where your existing scanner function can pick it up
    return redirect(f"/?lastfm_user={username}")

# ---------------------------------------------------------
# Keep your existing Ticketmaster API routes below here
# ---------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))