import os
import sys
import requests
import json

# CONFIGURATION
TITLE_ID = "5107"
BASE_URL = f"https://{TITLE_ID}.playfabapi.com"
CUSTOM_ID = "VT2_Wiki_Bot_Reader"

FANDOM_API_URL = "https://vermintide2.fandom.com/api.php"
TARGET_PAGE = "Module:EventWidget/Data/Current"

# Credentials loaded from GitHub Secrets
BOT_USERNAME = os.environ.get("FANDOM_BOT_USERNAME")
BOT_PASSWORD = os.environ.get("FANDOM_BOT_PASSWORD")

# LUA FORMATTER
def to_lua(obj, indent=0):
    """Recursively converts Python dicts/lists to formatted Lua tables."""
    spaces = "    " * indent
    if isinstance(obj, dict):
        if not obj: return "{}"
        items = []
        for k, v in obj.items():
            if k == "end": k = "end_time" # Lua reserved keyword protection
            key_str = f'["{k}"]' if isinstance(k, str) else str(k)
            items.append(f"{spaces}    {key_str} = {to_lua(v, indent + 1)}")
        return "{\n" + ",\n".join(items) + f"\n{spaces}}}"
    elif isinstance(obj, list):
        if not obj: return "{}"
        if all(isinstance(x, (str, int, float)) for x in obj):
            items = [f'"{x}"' if isinstance(x, str) else str(x) for x in obj]
            return "{ " + ", ".join(items) + " }"
        else:
            items = [f"{spaces}    {to_lua(x, indent + 1)}" for x in obj]
            return "{\n" + ",\n".join(items) + f"\n{spaces}}}"
    elif isinstance(obj, str):
        return f'"{obj.replace("\\", "\\\\").replace("\"", "\\\"")}"'
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif obj is None:
        return "nil"
    else:
        return str(obj)

# PLAYFAB FETCHER
def fetch_playfab_data():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json", "X-PlayFabSDK": "VT2WikiBot/1.0"})
    
    # Login
    login_payload = {"TitleId": TITLE_ID, "CustomId": CUSTOM_ID, "CreateAccount": True}
    login_resp = session.post(f"{BASE_URL}/Client/LoginWithCustomID", json=login_payload).json()
    if login_resp.get("code") != 200:
        raise Exception(f"PlayFab login failed: {login_resp}")
    
    ticket = login_resp["data"]["SessionTicket"]
    session.headers["X-Authorization"] = ticket
    
    # Get Live Events
    cloud_payload = {
        "FunctionName": "getLiveEvents",
        "FunctionParameter": {"id": 1, "metadata": {}},
        "GeneratePlayStreamEvent": False
    }
    events_resp = session.post(f"{BASE_URL}/Client/ExecuteCloudScript", json=cloud_payload).json()
    if events_resp.get("code") != 200:
        raise Exception(f"getLiveEvents failed: {events_resp}")
        
    return events_resp["data"].get("FunctionResult", {})

def parse_widget_data(raw_data):
    live_events = raw_data.get("live_events", raw_data)
    
    # Weekly Event
    special_events = live_events.get("special_events", [])
    weekly_event = {}
    if special_events:
        active_names = live_events.get("active_events", [])
        se = special_events[0]
        for s in special_events:
            if s.get("name") in active_names:
                se = s
                break
        
        mutators = se.get("mutators", [])
        key = mutators[0] if mutators else se.get("name", "unknown")
        
        weekly_event = {
            "key": key,
            "start_time": se.get("start", 0) // 1000, # Convert ms to seconds
            "end_time": se.get("end", 0) // 1000
        }
        
    # Chaos Wastes
    chaos_wastes_list = live_events.get("weekly_chaos_wastes", [])
    chaos_wastes = {}
    if chaos_wastes_list:
        cw = chaos_wastes_list[0]
        gmd = cw.get("game_mode_data", {})
        info = cw.get("information", {})
        
        chaos_wastes = {
            "journey": gmd.get("journey_name", "unknown"),
            "boons": gmd.get("boons", []),
            "mutators": gmd.get("mutators", []),
            "start_time": info.get("start_timestamp", 0) // 1000,
            "end_time": info.get("end_timestamp", 0) // 1000
        }
        
    return {
        "weekly_event": weekly_event,
        "chaos_wastes": chaos_wastes
    }

# FANDOM UPDATER
def update_fandom_wiki(lua_content):
    if not BOT_USERNAME or not BOT_PASSWORD:
        print("WARNING: Fandom credentials not provided. Skipping wiki update.")
        print("Generated Lua content:\n", lua_content)
        return

    session = requests.Session()
    
    # Get Login Token
    resp = session.get(FANDOM_API_URL, params={
        "action": "query", "meta": "tokens", "type": "login", "format": "json"
    }).json()
    login_token = resp["query"]["tokens"]["logintoken"]
    
    # Login
    resp = session.post(FANDOM_API_URL, data={
        "action": "login", "lgname": BOT_USERNAME, "lgpassword": BOT_PASSWORD,
        "lgtoken": login_token, "format": "json"
    }).json()
    
    if resp.get("login", {}).get("result") != "Success":
        raise Exception(f"Fandom login failed: {resp}")
        
    # Get CSRF Token
    resp = session.get(FANDOM_API_URL, params={
        "action": "query", "meta": "tokens", "format": "json"
    }).json()
    csrf_token = resp["query"]["tokens"]["csrftoken"]
    
    # Edit Page
    resp = session.post(FANDOM_API_URL, data={
        "action": "edit",
        "title": TARGET_PAGE,
        "text": lua_content,
        "token": csrf_token,
        "format": "json",
        "bot": True,
        "summary": "Bot: Automated update of live event data"
    }).json()
    
    if "error" in resp:
        raise Exception(f"Fandom edit failed: {resp}")
        
    print(f"Successfully updated {TARGET_PAGE} on Fandom!")

if __name__ == "__main__":
    try:
        print("Fetching data from PlayFab...")
        raw_data = fetch_playfab_data()
        
        print("Parsing data for wiki widget...")
        widget_data = parse_widget_data(raw_data)
        
        print("Formatting as Lua table...")
        lua_table = to_lua(widget_data)
        lua_content = f"-- This page is automatically updated by the VT2 Event Bot.\n-- Do not edit manually.\nreturn {lua_table}"
        
        print("Pushing to Fandom...")
        update_fandom_wiki(lua_content)
        
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
