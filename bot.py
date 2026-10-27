import os
import sys
import json
import requests

# Configuration
TITLE_ID = "5107"
BASE_URL = f"https://{TITLE_ID}.playfabapi.com"
CUSTOM_ID = "VT2_Wiki_Bot_Reader"

FANDOM_API_URL = "https://vermintide2.fandom.com/api.php"
TARGET_PAGE = "Module:EventWidget/Data/Current"

BOT_USERNAME = os.environ.get("FANDOM_BOT_USERNAME")
BOT_PASSWORD = os.environ.get("FANDOM_BOT_PASSWORD")

USER_AGENT = "VT2WikiBot/1.0 (Vermintide 2 Fandom Wiki Bot; https://vermintide2.fandom.com)"

# Lua Table Formatter
def to_lua(obj, indent=0):
    """Recursively converts Python dictionaries and lists to formatted Lua tables."""
    spaces = "    " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = []
        for k, v in obj.items():
            if k == "end":
                k = "end_time"
            key_str = f'["{k}"]' if isinstance(k, str) else str(k)
            items.append(f"{spaces}    {key_str} = {to_lua(v, indent + 1)}")
        return "{\n" + ",\n".join(items) + f"\n{spaces}}}"
    elif isinstance(obj, list):
        if not obj:
            return "{}"
        if all(isinstance(x, (str, int, float)) for x in obj):
            items = [f'"{x}"' if isinstance(x, str) else str(x) for x in obj]
            return "{ " + ", ".join(items) + " }"
        else:
            items = [f"{spaces}    {to_lua(x, indent + 1)}" for x in obj]
            return "{\n" + ",\n".join(items) + f"\n{spaces}}}"
    elif isinstance(obj, str):
        escaped = obj.replace("\\", "\\\\").replace("\"", "\\\"")
        return f'"{escaped}"'
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif obj is None:
        return "nil"
    else:
        return str(obj)

# PlayFab Data Fetcher
def fetch_playfab_data():
    """Authenticates with PlayFab and retrieves live event data."""
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json", 
        "X-PlayFabSDK": "VT2WikiBot/1.0",
        "User-Agent": USER_AGENT
    })
    
    print("Authenticating with PlayFab...")
    login_payload = {"TitleId": TITLE_ID, "CustomId": CUSTOM_ID, "CreateAccount": True}
    login_resp = session.post(f"{BASE_URL}/Client/LoginWithCustomID", json=login_payload).json()
    if login_resp.get("code") != 200:
        print(f"PlayFab authentication failed: {json.dumps(login_resp, indent=2)}")
        raise Exception("PlayFab authentication failed.")
    
    ticket = login_resp["data"]["SessionTicket"]
    session.headers["X-Authorization"] = ticket
    
    print("Fetching live events via cloud script...")
    cloud_payload = {
        "FunctionName": "getLiveEvents",
        "FunctionParameter": {"id": 1, "metadata": {}},
        "GeneratePlayStreamEvent": False
    }
    events_resp = session.post(f"{BASE_URL}/Client/ExecuteCloudScript", json=cloud_payload).json()
    if events_resp.get("code") != 200:
        print(f"Cloud script execution failed: {json.dumps(events_resp, indent=2)}")
        raise Exception("Cloud script execution failed.")
        
    return events_resp["data"].get("FunctionResult", {})

def parse_widget_data(raw_data):
    """Extracts and structures relevant data for the wiki widget."""
    live_events = raw_data.get("live_events", raw_data)
    
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
            "start_time": se.get("start", 0) // 1000,
            "end_time": se.get("end", 0) // 1000
        }
        
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

# Fandom Wiki Updater
def update_fandom_wiki(lua_content):
    """Logs into Fandom and updates the target module page."""
    if not BOT_USERNAME or not BOT_PASSWORD:
        print("Warning: Fandom credentials not provided in environment variables. Skipping wiki update.")
        print("Generated Lua content:")
        print(lua_content)
        return

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    
    print("Requesting Fandom login token...")
    resp = session.get(FANDOM_API_URL, params={
        "action": "query", "meta": "tokens", "type": "login", "format": "json"
    }).json()
    
    if "error" in resp:
        print(f"Fandom token request error: {json.dumps(resp, indent=2)}")
        raise Exception("Failed to retrieve Fandom login token.")
        
    login_token = resp["query"]["tokens"]["logintoken"]
    
    print("Authenticating with Fandom...")
    resp = session.post(FANDOM_API_URL, data={
        "action": "login", "lgname": BOT_USERNAME, "lgpassword": BOT_PASSWORD,
        "lgtoken": login_token, "format": "json"
    }).json()
    
    login_result = resp.get("login", {}).get("result")
    if login_result != "Success":
        print(f"Fandom authentication failed. Result: {login_result}")
        print(f"Response details: {json.dumps(resp, indent=2)}")
        raise Exception(f"Fandom authentication failed: {login_result}")
        
    print("Requesting Fandom CSRF token...")
    resp = session.get(FANDOM_API_URL, params={
        "action": "query", "meta": "tokens", "format": "json"
    }).json()
    
    if "error" in resp:
        print(f"Fandom CSRF token request error: {json.dumps(resp, indent=2)}")
        raise Exception("Failed to retrieve Fandom CSRF token.")
        
    csrf_token = resp["query"]["tokens"]["csrftoken"]
    
    print(f"Updating page: {TARGET_PAGE}...")
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
        print(f"Fandom edit request error: {json.dumps(resp, indent=2)}")
        raise Exception(f"Fandom edit failed: {resp['error'].get('info', 'Unknown error')}")
        
    print(f"Successfully updated {TARGET_PAGE} on Fandom.")

if __name__ == "__main__":
    try:
        print("Initializing VT2 Wiki Bot...")
        raw_data = fetch_playfab_data()
        widget_data = parse_widget_data(raw_data)
        
        lua_table = to_lua(widget_data)
        lua_content = f"-- This page is automatically updated by the VT2 Event Bot.\n-- Do not edit manually.\nreturn {lua_table}"
        
        update_fandom_wiki(lua_content)
        print("Execution completed successfully.")
        
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
