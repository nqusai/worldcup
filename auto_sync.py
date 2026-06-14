import os
import json
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timezone

# --- CONFIGURATION ---
COMPETITION_CODE = "WC" # World Cup code

# 1. Initialize Firebase Securely
cert_json = os.environ.get('FIREBASE_CREDENTIALS')
if not cert_json:
    raise ValueError("Missing FIREBASE_CREDENTIALS environment variable")

cred_dict = json.loads(cert_json)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

# 2. API Setup
API_KEY = os.environ.get('FOOTBALL_API_KEY')
if not API_KEY:
    raise ValueError("Missing FOOTBALL_API_KEY environment variable")
    
headers = { "X-Auth-Token": API_KEY }
base_url = f"https://api.football-data.org/v4/competitions/{COMPETITION_CODE}/matches"

print("🤖 Starting Automatic Sync...")

# ==========================================
# STEP 1: SYNC ALL GAMES (Upcoming & Live)
# ==========================================
print("Fetching all games...")
resp = requests.get(base_url, headers=headers).json()

current_time = datetime.now(timezone.utc)

if 'matches' in resp:
    count = 0
    for match in resp['matches']:
        match_date = datetime.strptime(match['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ms_until_kickoff = (match_date - current_time).total_seconds() * 1000
        is_locked = ms_until_kickoff <= 3600000 # 1 hour
        
        match_data = {
            "homeTeam": match['homeTeam'].get('name', 'TBD'),
            "awayTeam": match['awayTeam'].get('name', 'TBD'),
            "utcDate": match['utcDate'],
            "status": match['status'],
            "is_locked": is_locked
        }
        
        # Save actual scores if they exist
        if 'score' in match and match['score'].get('fullTime') and match['score']['fullTime']['home'] is not None:
            match_data['score'] = match['score']['fullTime']
            
        db.collection("matches").document(str(match['id'])).set(match_data, merge=True)
        count += 1
    print(f"✅ Synced {count} matches to Firebase.")
else:
    print("⚠️ No matches found in API response.")

# ==========================================
# STEP 1.5: LOCK PREDICTIONS (1 Hour Before)
# ==========================================
print("Locking predictions for games starting soon...")
preds_ref = db.collection('predictions').stream()
all_preds = {p.id: p.to_dict() for p in preds_ref}

for match in resp.get('matches', []):
    match_date = datetime.strptime(match['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    ms_until_kickoff = (match_date - current_time).total_seconds() * 1000
    
    if ms_until_kickoff <= 3600000: # 1 hour
        match_id = str(match['id'])
        # Find all unlocked predictions for this match
        for p_id, p_data in all_preds.items():
            if str(p_data.get('match_id')) == match_id and not p_data.get('is_locked', False):
                db.collection('predictions').document(p_id).update({"is_locked": True})
                print(f"🔒 Locked prediction {p_id}")

# ==========================================
# STEP 2: CALCULATE POINTS FOR FINISHED GAMES
# ==========================================
print("Calculating points for finished games...")
finished_resp = requests.get(f"{base_url}?status=FINISHED", headers=headers).json()
finished_matches = finished_resp.get('matches', [])

preds_ref = db.collection('predictions').stream()
user_points = {}

for doc in preds_ref:
    pred = doc.to_dict()
    uid = pred.get('user_id')
    if not uid:
        continue
    
    if uid not in user_points:
        user_points[uid] = {"name": pred.get('user_name', 'Unknown Player'), "total": 0}
        
    # Find if this specific prediction's match has finished
    actual_match = next((m for m in finished_matches if str(m['id']) == str(pred.get('match_id'))), None)
    
    if actual_match and 'score' in actual_match and actual_match['score'].get('fullTime') and actual_match['score']['fullTime'].get('home') is not None:
        r_home = actual_match['score']['fullTime']['home']
        r_away = actual_match['score']['fullTime']['away']
        p_home = pred.get('home_pred')
        p_away = pred.get('away_pred')
        
        if p_home is not None and p_away is not None:
            # Convert to integers just in case they are stored as strings in Firebase
            p_home = int(p_home)
            p_away = int(p_away)
            r_home = int(r_home)
            r_away = int(r_away)
            
            pts = 0
            if p_home == r_home and p_away == r_away:
                pts = 3 # Exact
            elif (p_home - p_away) == (r_home - r_away):
                pts = 2 # Margin / Draw
            elif (p_home > p_away and r_home > r_away) or (p_home < p_away and r_home < r_away):
                pts = 1 # Outcome
                
            user_points[uid]['total'] += pts
            
            # Save awarded points to the prediction document
            db.collection('predictions').document(doc.id).update({"points_awarded": pts})

# Update Users Leaderboard Collection
for uid, data in user_points.items():
    db.collection('users').document(uid).set({
        "name": data['name'], 
        "points": data['total']
    }, merge=True)

print("✅ Leaderboard updated successfully!")
print("🏁 Sync complete.")