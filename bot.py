import email.utils
import json
import os
import re
import sys
import time
import requests

# Configuration
TITLE_ID = "5107"
BASE_URL = f"https://{TITLE_ID}.playfabapi.com"
CUSTOM_ID = os.environ.get("PLAYFAB_CUSTOM_ID", "VT2_Wiki_Bot_Reader")
CLIENT_VERSION = os.environ.get("VT2_CLIENT_VERSION", "6.13.0")

PLAYFAB_METADATA = {
    "client_type": "client",
    "client_version": CLIENT_VERSION,
    "realm": "official",
}

FANDOM_API_URL = "https://vermintide2.fandom.com/api.php"
TARGET_CURRENT_PAGE = "Module:EventWidget/Data/Current"
TARGET_STORE_PAGE = "Module:EventWidget/Data/Store"

BOT_USERNAME = os.environ.get("FANDOM_BOT_USERNAME")
BOT_PASSWORD = os.environ.get("FANDOM_BOT_PASSWORD")

USER_AGENT = "VT2WikiBot/2.0 (Vermintide 2 Fandom Wiki Bot; https://vermintide2.fandom.com)"
REQUEST_TIMEOUT = 25
WOLF_FATHER_CACHE_FILE = os.environ.get(
    "WOLF_FATHER_CACHE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "wolf_father_cache.json"),
)
WOLF_FATHER_POST_EVENT_SECONDS = 3 * 86400

# Constants from scripts/settings/dlcs/morris/deus_journey_settings.lua
AVAILABLE_JOURNEY_ORDER = [
    "journey_ruin",
    "journey_cave",
    "journey_ice",
    "journey_citadel",
]
DEUS_JOURNEY_CYCLE_GODS = [
    "nurgle",
    "tzeentch",
    "khorne",
    "slaanesh",
]

CW_DIFFICULTY_MAP = [
    ("normal", "recruit"),
    ("hard", "veteran"),
    ("harder", "champion"),
    ("hardest", "legend"),
]


# Utility Functions
def deep_parse(obj):
    """Recursively parses JSON strings embedded within dictionaries or lists."""
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped and stripped[0] in "{[":
            try:
                parsed = json.loads(stripped)
                return deep_parse(parsed)
            except (json.JSONDecodeError, ValueError):
                return obj
        return obj
    if isinstance(obj, dict):
        return {k: deep_parse(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_parse(i) for i in obj]
    return obj


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_list(value):
    return value if isinstance(value, list) else []


def ms_to_seconds(value):
    """Converts millisecond timestamps (or numeric strings) to integer Unix seconds."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return 0
    if isinstance(value, (int, float)):
        if value <= 0 or value >= 4102444800000:
            return 0
        return int(value // 1000) if value > 100_000_000_000 else int(value)
    return 0


def parse_http_date_ms(resp):
    """Extracts server UTC timestamp in milliseconds from an HTTP Date header."""
    date_hdr = resp.headers.get("Date")
    if date_hdr:
        try:
            dt = email.utils.parsedate_to_datetime(date_hdr)
            return int(dt.timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    return int(time.time() * 1000)


def load_wolf_father_cache():
    """Loads the last valid calendar so its final claim windows survive API removal."""
    try:
        with open(WOLF_FATHER_CACHE_FILE, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        return cached if isinstance(cached, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_wolf_father_cache(calendar):
    """Atomically persists a normalized Wolf-Father calendar."""
    if not isinstance(calendar, dict) or not calendar.get("days"):
        return
    tmp_path = WOLF_FATHER_CACHE_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(calendar, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, WOLF_FATHER_CACHE_FILE)
    except OSError as exc:
        print(f"Warning: could not save Wolf-Father cache: {exc}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# Lua Table Formatter
LUA_RESERVED_WORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for",
    "function", "goto", "if", "in", "local", "nil", "not", "or",
    "repeat", "return", "then", "true", "until", "while",
}
LUA_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def format_lua_key(key):
    """Returns a bare key for valid Lua identifiers, bracket notation otherwise."""
    if isinstance(key, str):
        if key == "end":
            key = "end_time"
        if LUA_IDENTIFIER_PATTERN.match(key) and key not in LUA_RESERVED_WORDS:
            return key
        escaped = (
            key.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'["{escaped}"]'
    return f"[{key}]"


def escape_lua_str(s):
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def to_lua(obj, indent=0):
    """Recursively converts Python dicts and lists to clean formatted Lua tables."""
    spaces = "    " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = []
        for k, v in obj.items():
            if v is None:
                continue
            key_str = format_lua_key(k)
            items.append(f"{spaces}    {key_str} = {to_lua(v, indent + 1)}")
        if not items:
            return "{}"
        return "{\n" + ",\n".join(items) + f"\n{spaces}}}"
    if isinstance(obj, list):
        if not obj:
            return "{}"
        if all(isinstance(x, (str, int, float, bool)) for x in obj):
            items = []
            for x in obj:
                if isinstance(x, bool):
                    items.append("true" if x else "false")
                elif isinstance(x, str):
                    items.append(f'"{escape_lua_str(x)}"')
                else:
                    items.append(str(x))
            inline = "{ " + ", ".join(items) + " }"
            if len(inline) <= 100:
                return inline
        items = [f"{spaces}    {to_lua(x, indent + 1)}" for x in obj]
        return "{\n" + ",\n".join(items) + f"\n{spaces}}}"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, str):
        return f'"{escape_lua_str(obj)}"'
    if obj is None:
        return "nil"
    return str(obj)


def wrap_lua_module(table_obj):
    return (
        "-- This page is automatically updated by the VT2 Event Bot.\n"
        "-- Do not edit manually.\n"
        f"return {to_lua(table_obj)}\n"
    )


# PlayFab Data Fetcher
def execute_cloud_script(session, function_name, params=None, required=False):
    """Calls a PlayFab CloudScript function, returning (parsed_result, server_ms)."""
    payload = {
        "FunctionName": function_name,
        "FunctionParameter": params if params is not None else {},
        "GeneratePlayStreamEvent": False,
    }
    try:
        resp = session.post(
            f"{BASE_URL}/Client/ExecuteCloudScript",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        server_ms = parse_http_date_ms(resp)
        body = resp.json()
    except Exception as exc:
        if required:
            raise
        print(f"Warning: CloudScript {function_name} request failed: {exc}")
        return {}, int(time.time() * 1000)

    if body.get("code") != 200:
        msg = f"CloudScript {function_name} HTTP/API error: {json.dumps(body)}"
        if required:
            raise RuntimeError(msg)
        print(f"Warning: {msg}")
        return {}, server_ms

    data = as_dict(body.get("data"))
    if data.get("Error"):
        msg = f"CloudScript {function_name} execution error: {json.dumps(data['Error'])}"
        if required:
            raise RuntimeError(msg)
        print(f"Warning: {msg}")
        return {}, server_ms

    return deep_parse(data.get("FunctionResult", {})), server_ms


def fetch_playfab_data():
    """Authenticates with PlayFab and retrieves all wiki-relevant live datasets."""
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "X-PlayFabSDK": "VT2WikiBot/2.0",
        "User-Agent": USER_AGENT,
    })

    print("Authenticating with PlayFab")
    login_payload = {
        "TitleId": TITLE_ID,
        "CustomId": CUSTOM_ID,
        "CreateAccount": True,
        "InfoRequestParameters": {
            "GetTitleData": True,
        },
    }
    login_http = session.post(
        f"{BASE_URL}/Client/LoginWithCustomID",
        json=login_payload,
        timeout=REQUEST_TIMEOUT,
    )
    login_resp = login_http.json()
    if login_resp.get("code") != 200:
        print(f"PlayFab authentication failed: {json.dumps(login_resp, indent=2)}")
        raise RuntimeError("PlayFab authentication failed.")

    ticket = login_resp["data"]["SessionTicket"]
    session.headers["X-Authorization"] = ticket

    info_payload = as_dict(login_resp["data"].get("InfoResultPayload"))
    title_data = deep_parse(as_dict(info_payload.get("TitleData")))

    print("Fetching live events via CloudScript")
    live_events_res, _ = execute_cloud_script(
        session,
        "getLiveEvents",
        {"id": 1, "metadata": PLAYFAB_METADATA},
        required=False,
    )
    if not live_events_res and "live_events_v2" in title_data:
        live_events_res = {"live_events": title_data["live_events_v2"]}

    print("Fetching weekly Chaos Wastes rewards")
    weekly_rewards_res, _ = execute_cloud_script(
        session,
        "getWeeklyEventRewards",
        {"id": 2, "metadata": PLAYFAB_METADATA},
    )

    print("Fetching special event quests")
    quests_res, _ = execute_cloud_script(
        session,
        "getQuests",
        {"metadata": PLAYFAB_METADATA},
    )

    print("Fetching Gifts of the Wolf Father calendar status")
    store_rewards_res, _ = execute_cloud_script(
        session,
        "getStoreRewards",
        {"metadata": PLAYFAB_METADATA},
    )

    print("Fetching Chaos Wastes Be'lakor & Chaos God cycles")
    belakor_res, belakor_ms = execute_cloud_script(session, "deusSetBelakorCurse", {})
    deus_res, deus_ms = execute_cloud_script(session, "deusPlayerSetup", {})
    if as_dict(as_dict(deus_res).get("deus_journey_cycle_data")).get("remaining_time_ms", 0) < 0:
        # First call after an expired week updates stored cycle state on PlayFab;
        # a single immediate re-read returns the fresh positive remaining_time_ms.
        deus_res, deus_ms = execute_cloud_script(session, "deusPlayerSetup", {})
    print("Fetching Lohner's Emporium store items")
    store_resp = {}
    try:
        r_store = session.post(
            f"{BASE_URL}/Client/GetStoreItems",
            json={"StoreId": "Store"},
            timeout=REQUEST_TIMEOUT,
        ).json()
        if r_store.get("code") == 200:
            store_resp = deep_parse(as_dict(r_store.get("data")))
        else:
            print(f"Warning: GetStoreItems failed: {json.dumps(r_store)}")
    except Exception as exc:
        print(f"Warning: GetStoreItems exception: {exc}")

    print("Fetching PlayFab catalog metadata for event membership")
    catalog_resp = {}
    try:
        r_catalog = session.post(
            f"{BASE_URL}/Client/GetCatalogItems",
            json={},
            timeout=REQUEST_TIMEOUT,
        ).json()
        if r_catalog.get("code") == 200:
            catalog_resp = deep_parse(as_dict(r_catalog.get("data")))
        else:
            print(f"Warning: GetCatalogItems failed: {json.dumps(r_catalog)}")
    except Exception as exc:
        print(f"Warning: GetCatalogItems exception: {exc}")

    return {
        "title_data": title_data,
        "live_events_res": live_events_res,
        "weekly_rewards_res": weekly_rewards_res,
        "quests_res": quests_res,
        "store_rewards_res": store_rewards_res,
        "belakor_res": belakor_res,
        "belakor_ms": belakor_ms,
        "deus_res": deus_res,
        "deus_ms": deus_ms,
        "store_resp": store_resp,
        "catalog_resp": catalog_resp,
    }


# Data Parsers
def normalize_cycle(raw_cycle_dict, now_ms):
    """Normalizes PlayFab cycle data (span_ms, remaining_time_ms, cycle_count)."""
    span_ms = raw_cycle_dict.get("span_ms")
    rem_ms = raw_cycle_dict.get("remaining_time_ms")
    cycle_count = raw_cycle_dict.get("cycle_count")
    if not isinstance(span_ms, (int, float)) or span_ms <= 0:
        return None
    if not isinstance(rem_ms, (int, float)) or not isinstance(cycle_count, int):
        return None

    span_ms = int(span_ms)
    rem_ms = int(rem_ms)
    if rem_ms < 0:
        past_ms = -rem_ms
        cycle_delta = (past_ms + span_ms - 1) // span_ms
        cycle_count += cycle_delta
        rem_ms = span_ms - (past_ms % span_ms)

    span_s = span_ms // 1000
    start_s = int(round((now_ms - (span_ms - rem_ms)) / 1000.0))
    end_s = start_s + span_s
    return {
        "cycle_count": cycle_count,
        "span": span_s,
        "start_time": start_s,
        "end_time": end_s,
    }


def clean_reward_entry(entry):
    """Normalizes a single PlayFab reward object into clean Lua-ready keys."""
    entry = as_dict(entry)
    out = {}
    for key in (
        "reward_type",
        "weapon_skin_name",
        "item_name",
        "item_id",
        "keep_decoration_name",
        "currency_code",
        "amount",
    ):
        if key in entry and entry[key] is not None:
            out[key] = entry[key]
    return out


def parse_current_data(bundle):
    """Builds the Lua data dictionary for Module:EventWidget/Data/Current."""
    title_data = as_dict(bundle.get("title_data"))
    live_events_res = as_dict(bundle.get("live_events_res"))
    live_events = as_dict(live_events_res.get("live_events", live_events_res))

    # Weekly Chaos Wastes rotation + tier rewards
    chaos_wastes = {}
    weekly_start = 0
    weekly_end = 0
    chaos_list = as_list(live_events.get("weekly_chaos_wastes"))
    if chaos_list:
        cw = as_dict(chaos_list[0])
        gmd = as_dict(cw.get("game_mode_data"))
        info = as_dict(cw.get("information"))
        weekly_start = ms_to_seconds(info.get("start_timestamp", 0))
        weekly_end = ms_to_seconds(info.get("end_timestamp", 0))
        chaos_wastes = {
            "journey": gmd.get("journey_name", "unknown"),
            "boons": as_list(gmd.get("boons")),
            "mutators": as_list(gmd.get("mutators")),
            "start_time": weekly_start,
            "end_time": weekly_end,
        }

    weekly_rewards_res = as_dict(bundle.get("weekly_rewards_res"))
    deus_rewards_root = as_dict(as_dict(weekly_rewards_res.get("data")).get("deus"))
    deus_tiers = as_dict(deus_rewards_root.get("data"))
    if deus_tiers and chaos_wastes:
        if isinstance(deus_rewards_root.get("index"), int):
            chaos_wastes["reward_index"] = deus_rewards_root["index"]

        parsed_tiers = {}
        weapon_skin = None
        for pf_tier, wiki_tier in CW_DIFFICULTY_MAP:
            tier_obj = as_dict(deus_tiers.get(pf_tier))
            raw_list = as_list(tier_obj.get("rewards"))
            cleaned_list = [clean_reward_entry(r) for r in raw_list if isinstance(r, dict)]
            if cleaned_list:
                parsed_tiers[wiki_tier] = cleaned_list
                for r in cleaned_list:
                    if r.get("weapon_skin_name") and not weapon_skin:
                        weapon_skin = r["weapon_skin_name"]

        if weapon_skin:
            chaos_wastes["weapon_skin"] = weapon_skin
        if parsed_tiers:
            chaos_wastes["rewards"] = parsed_tiers

    # Weekly Event
    weekly_event = {}
    weekly_list = as_list(live_events.get("weekly_events"))
    if weekly_list:
        we_gmd = as_dict(as_dict(weekly_list[0]).get("game_mode_data"))
        we_mutators = as_list(we_gmd.get("mutators"))
        weekly_event = {
            "key": we_mutators[0] if we_mutators else "unknown",
            "mutators": we_mutators,
            "start_time": weekly_start,
            "end_time": weekly_end,
        }

    # Special Events
    special_event = {}
    special_list = as_list(live_events.get("special_events"))
    active_names = as_list(live_events.get("active_events"))
    if special_list:
        se = as_dict(special_list[0])
        for candidate in special_list:
            if isinstance(candidate, dict) and candidate.get("name") in active_names:
                se = candidate
                break
        special_event = {
            "key": se.get("name", "unknown"),
            "mutators": as_list(se.get("mutators")),
            "level_keys": as_list(se.get("level_keys")),
            "start_time": ms_to_seconds(se.get("start", 0)),
            "end_time": ms_to_seconds(se.get("end", 0)),
        }

    special_events = []
    for candidate in special_list:
        if isinstance(candidate, dict):
            special_events.append({
                "key": candidate.get("name", "unknown"),
                "active": candidate.get("name") in active_names,
                "mutators": as_list(candidate.get("mutators")),
                "level_keys": as_list(candidate.get("level_keys")),
                "start_time": ms_to_seconds(candidate.get("start", 0)),
                "end_time": ms_to_seconds(candidate.get("end", 0)),
            })

    # Standalone XP Multiplier (from TitleData["experience_multiplier"])
    raw_xp = title_data.get("experience_multiplier", 1)
    try:
        xp_val = float(raw_xp)
        xp_multiplier = int(xp_val) if xp_val.is_integer() else xp_val
    except (TypeError, ValueError):
        xp_multiplier = 1

    # Live Special Event Quests (global; ignores per-player daily/weekly quests)
    quests_res = as_dict(bundle.get("quests_res"))
    raw_event_quests = as_dict(quests_res.get("current_event_quests"))
    event_quests = []
    for slot_key in sorted(raw_event_quests.keys()):
        q = as_dict(raw_event_quests[slot_key])
        if not q.get("name"):
            continue
        entry = {
            "slot": slot_key,
            "key": q["name"],
        }
        if "type" in q:
            entry["type"] = q["type"]
        reward = clean_reward_entry(q.get("reward"))
        if reward:
            entry["reward"] = reward
        event_quests.append(entry)

    # Gifts of the Wolf Father (Advent Calendar ONLY; ignores daily login loop)
    store_rewards = as_dict(bundle.get("store_rewards_res"))
    wolf_father = {}
    if store_rewards.get("event_type") == "calendar":
        total_rewards = store_rewards.get("total_rewards")
        if not isinstance(total_rewards, int):
            total_rewards = len(as_list(store_rewards.get("rewards")))
        wf_start = ms_to_seconds(store_rewards.get("start_time", 0))
        wf_end = ms_to_seconds(store_rewards.get("end_time", 0))
        if wf_end == 0 and wf_start > 0 and total_rewards > 0:
            wf_end = wf_start + 86400 * total_rewards

        days = []
        for idx, day_rewards in enumerate(as_list(store_rewards.get("rewards")), start=1):
            cleaned_day = [
                clean_reward_entry(item)
                for item in as_list(day_rewards)
                if isinstance(item, dict)
            ]
            day_entry = {
                "day": idx,
                "rewards": cleaned_day,
            }
            if wf_start > 0:
                day_entry["unlock_time"] = wf_start + 86400 * (idx - 1)
            days.append(day_entry)

        wolf_father = {
            "active": True,
            "total_rewards": total_rewards,
            "start_time": wf_start,
            "end_time": wf_end,
            "display_end_time": wf_end + WOLF_FATHER_POST_EVENT_SECONDS if wf_end else 0,
            "updated_at": int(time.time()),
            "days": days,
        }
        save_wolf_father_cache(wolf_father)
    else:
        cached_wolf_father = load_wolf_father_cache()
        cached_end = ms_to_seconds(cached_wolf_father.get("end_time", 0))
        if cached_end and int(time.time()) < cached_end + WOLF_FATHER_POST_EVENT_SECONDS:
            cached_wolf_father["active"] = int(time.time()) < cached_end
            cached_wolf_father["display_end_time"] = cached_end + WOLF_FATHER_POST_EVENT_SECONDS
            wolf_father = cached_wolf_father

    # Chaos Wastes Be'lakor Invasion & Chaos God Curse Cycles
    belakor_active_cfg = as_dict(title_data.get("belakor_active"))
    belakor_enabled = bool(belakor_active_cfg.get("active", True))

    belakor_raw = as_dict(as_dict(bundle.get("belakor_res")).get("deus_belakor_curse_data"))
    belakor_norm = normalize_cycle(belakor_raw, bundle.get("belakor_ms", int(time.time() * 1000)))
    belakor_data = {}
    if belakor_norm:
        b_idx = belakor_norm["cycle_count"] % len(AVAILABLE_JOURNEY_ORDER)
        belakor_data = {
            "active": belakor_enabled,
            "journey": AVAILABLE_JOURNEY_ORDER[b_idx],
            "cycle_count": belakor_norm["cycle_count"],
            "span": belakor_norm["span"],
            "start_time": belakor_norm["start_time"],
            "end_time": belakor_norm["end_time"],
        }

    deus_raw = as_dict(as_dict(bundle.get("deus_res")).get("deus_journey_cycle_data"))
    deus_norm = normalize_cycle(deus_raw, bundle.get("deus_ms", int(time.time() * 1000)))
    god_cycle_data = {}
    if deus_norm:
        base_idx = deus_norm["cycle_count"] % len(DEUS_JOURNEY_CYCLE_GODS)
        journeys_gods = {}
        for idx, j_name in enumerate(AVAILABLE_JOURNEY_ORDER):
            god_idx = (base_idx + idx) % len(DEUS_JOURNEY_CYCLE_GODS)
            journeys_gods[j_name] = DEUS_JOURNEY_CYCLE_GODS[god_idx]
        god_cycle_data = {
            "cycle_count": deus_norm["cycle_count"],
            "span": deus_norm["span"],
            "start_time": deus_norm["start_time"],
            "end_time": deus_norm["end_time"],
            "journeys": journeys_gods,
        }

    chaos_wastes_cycles = {}
    if belakor_data:
        chaos_wastes_cycles["belakor"] = belakor_data
    if god_cycle_data:
        chaos_wastes_cycles["god_cycle"] = god_cycle_data

    return {
        "weekly_event": weekly_event,
        "chaos_wastes": chaos_wastes,
        "chaos_wastes_cycles": chaos_wastes_cycles,
        "xp_multiplier": xp_multiplier,
        "special_event": special_event,
        "special_events": special_events,
        "event_quests": event_quests,
        "wolf_father": wolf_father,
    }


def parse_store_data(bundle):
    """Builds the Lua data dictionary for Module:EventWidget/Data/Store."""
    title_data = as_dict(bundle.get("title_data"))
    store_resp = as_dict(bundle.get("store_resp"))
    raw_store = as_list(store_resp.get("Store"))
    catalog_resp = as_dict(bundle.get("catalog_resp"))
    raw_catalog = as_list(catalog_resp.get("Catalog"))

    marketing = as_dict(as_dict(store_resp.get("MarketingData")).get("Metadata"))
    uploaded = marketing.get("uploaded", "")

    sm_items = {}
    vs_items = {}
    other_currency_counts = {}
    discounts = []
    event_items = []

    sorted_store = sorted(
        (item for item in raw_store if isinstance(item, dict) and item.get("ItemId")),
        key=lambda x: str(x["ItemId"]),
    )

    for item in sorted_store:
        key = str(item["ItemId"])
        vc_prices = as_dict(item.get("VirtualCurrencyPrices"))
        custom = as_dict(item.get("CustomData"))
        reg_prices = as_dict(custom.get("regular_prices"))
        raw_end = custom.get("end_time")
        end_ts = ms_to_seconds(raw_end) if raw_end is not None else 0

        if "SM" in vc_prices and isinstance(vc_prices["SM"], (int, float)):
            sm_items[key] = int(vc_prices["SM"])
        if "VS" in vc_prices and isinstance(vc_prices["VS"], (int, float)):
            vs_items[key] = int(vc_prices["VS"])

        for curr_code, curr_price in sorted(vc_prices.items()):
            if not isinstance(curr_price, (int, float)):
                continue
            price_int = int(curr_price)
            reg_val = reg_prices.get(curr_code)
            reg_int = int(reg_val) if isinstance(reg_val, (int, float)) else None

            if curr_code not in ("SM", "VS"):
                other_currency_counts[curr_code] = other_currency_counts.get(curr_code, 0) + 1

            # Active sales / discounts
            if reg_int and reg_int > price_int:
                pct = int(round((1.0 - (price_int / float(reg_int))) * 100))
                disc_entry = {
                    "key": key,
                    "currency": curr_code,
                    "price": price_int,
                    "regular_price": reg_int,
                    "discount_pct": pct,
                }
                if end_ts > 0:
                    disc_entry["end_time"] = end_ts
                elif isinstance(raw_end, str) and raw_end:
                    disc_entry["end_time_raw"] = raw_end
                discounts.append(disc_entry)

            # Temporary Event Shop stock (time-limited or event-currency items)
            if raw_end is not None or curr_code not in ("SM", "VS"):
                ev_entry = {
                    "key": key,
                    "currency": curr_code,
                    "price": price_int,
                }
                if reg_int is not None:
                    ev_entry["regular_price"] = reg_int
                if end_ts > 0:
                    ev_entry["end_time"] = end_ts
                elif isinstance(raw_end, str) and raw_end:
                    ev_entry["end_time_raw"] = raw_end
                event_items.append(ev_entry)

    # Seasonal Emporium membership comes from Catalog CustomData.events.
    # Availability and price come independently from GetStoreItems. Keeping
    # only IDs here avoids duplicating names, images, links, or descriptions.
    event_membership = {}
    type_buckets = {
        "weapon_skin": "weapons",
        "skin": "skins",
        "hat": "skins",
        "frame": "frames",
    }
    sm_available = set(sm_items)
    for catalog_item in raw_catalog:
        if not isinstance(catalog_item, dict):
            continue
        key = str(catalog_item.get("ItemId") or "")
        if not key or key not in sm_available:
            continue
        custom = deep_parse(catalog_item.get("CustomData", {}))
        custom = as_dict(custom)
        events = as_list(custom.get("events"))
        bucket_name = type_buckets.get(str(custom.get("item_type") or ""))
        if not events or not bucket_name:
            continue
        for event_key in events:
            if not isinstance(event_key, str) or not event_key:
                continue
            event = event_membership.setdefault(event_key, {
                "weapons": [], "skins": [], "frames": []
            })
            event[bucket_name].append(key)

    for event in event_membership.values():
        for bucket_name in ("weapons", "skins", "frames"):
            event[bucket_name] = sorted(set(event[bucket_name]))

    # Featured storefront from TitleData["store_layout_override"]
    layout_override = as_dict(title_data.get("store_layout_override"))
    pages = as_dict(layout_override.get("pages"))
    featured_raw = as_dict(pages.get("featured"))
    dlc_raw = as_dict(pages.get("dlc"))

    featured_grid = []
    for g in as_list(featured_raw.get("grid")):
        g = as_dict(g)
        if g.get("key") or g.get("id"):
            entry = {
                "key": g.get("key") or g.get("id"),
                "type": g.get("type", "item"),
            }
            if isinstance(g.get("steam_itemdefid"), int):
                entry["steam_itemdefid"] = g["steam_itemdefid"]
            featured_grid.append(entry)

    featured_slideshow = []
    for s in as_list(featured_raw.get("slideshow")):
        s = as_dict(s)
        if s.get("product_id"):
            entry = {
                "product_id": s["product_id"],
                "product_type": s.get("product_type", "item"),
            }
            if s.get("header"):
                entry["header"] = s["header"]
            if s.get("texture"):
                entry["texture"] = s["texture"]
            if isinstance(s.get("steam_itemdefid"), int):
                entry["steam_itemdefid"] = s["steam_itemdefid"]
            if isinstance(s.get("prio"), int):
                entry["prio"] = s["prio"]
            featured_slideshow.append(entry)

    featured = {
        "rotation_timestamp": ms_to_seconds(featured_raw.get("rotation_timestamp", 0)),
        "grid": featured_grid,
        "slideshow": featured_slideshow,
        "dlc": [str(x) for x in as_list(dlc_raw.get("content")) if isinstance(x, str)],
    }

    counts = {
        "total": len(sorted_store),
        "sm": len(sm_items),
        "vs": len(vs_items),
        "discounts": len(discounts),
        "event_items": len(event_items),
    }
    for curr_code, cnt in sorted(other_currency_counts.items()):
        counts[curr_code.lower()] = cnt

    return {
        "uploaded": uploaded,
        "counts": counts,
        "discounts": discounts,
        "event_items": event_items,
        "event_membership": event_membership,
        "featured": featured,
        "items": {
            "sm": sm_items,
            "vs": vs_items,
        },
    }


def stabilize_cycle_timestamps(current_data, existing_lua_text):
    """Prevents 1-2s HTTP latency jitter from triggering unnecessary wiki edits
    when cycle_count has not changed since the previous bot run."""
    if not existing_lua_text:
        return
    cycles = as_dict(current_data.get("chaos_wastes_cycles"))
    for block_name in ("belakor", "god_cycle"):
        block = as_dict(cycles.get(block_name))
        if not block:
            continue
        m = re.search(
            rf"{block_name}\s*=\s*\{{[^}}]*?cycle_count\s*=\s*(\d+)[^}}]*?"
            rf"start_time\s*=\s*(\d+)[^}}]*?end_time\s*=\s*(\d+)",
            existing_lua_text,
            re.DOTALL,
        )
        if not m:
            continue
        old_cycle = int(m.group(1))
        old_start = int(m.group(2))
        old_end = int(m.group(3))
        if (
            old_cycle == block.get("cycle_count")
            and abs(block.get("start_time", 0) - old_start) <= 10
        ):
            block["start_time"] = old_start
            block["end_time"] = old_end


# Fandom Wiki Updater
def get_fandom_page_text(session, title):
    """Fetches current raw text of a Fandom page (or None if missing)."""
    resp = session.get(
        FANDOM_API_URL,
        params={
            "action": "query",
            "prop": "revisions",
            "titles": title,
            "rvprop": "content",
            "rvslots": "main",
            "formatversion": "2",
            "format": "json",
        },
        timeout=REQUEST_TIMEOUT,
    ).json()
    pages = resp.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    revs = pages[0].get("revisions", [])
    if not revs:
        return None
    return revs[0].get("slots", {}).get("main", {}).get("content")


def create_authenticated_fandom_session():
    """Authenticates with Fandom once and returns (session, csrf_token)."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print("Requesting Fandom login token")
    resp = session.get(
        FANDOM_API_URL,
        params={"action": "query", "meta": "tokens", "type": "login", "format": "json"},
        timeout=REQUEST_TIMEOUT,
    ).json()
    if "error" in resp:
        raise RuntimeError(f"Failed to retrieve Fandom login token: {json.dumps(resp)}")

    login_token = resp["query"]["tokens"]["logintoken"]

    print("Authenticating with Fandom")
    resp = session.post(
        FANDOM_API_URL,
        data={
            "action": "login",
            "lgname": BOT_USERNAME,
            "lgpassword": BOT_PASSWORD,
            "lgtoken": login_token,
            "format": "json",
        },
        timeout=REQUEST_TIMEOUT,
    ).json()
    login_result = resp.get("login", {}).get("result")
    if login_result != "Success":
        raise RuntimeError(f"Fandom authentication failed ({login_result}): {json.dumps(resp)}")

    print("Requesting Fandom CSRF token")
    resp = session.get(
        FANDOM_API_URL,
        params={"action": "query", "meta": "tokens", "format": "json"},
        timeout=REQUEST_TIMEOUT,
    ).json()
    if "error" in resp:
        raise RuntimeError(f"Failed to retrieve Fandom CSRF token: {json.dumps(resp)}")

    return session, resp["query"]["tokens"]["csrftoken"]


def edit_fandom_page_if_changed(session, csrf_token, title, new_text, summary, existing_text=None):
    """Updates a Fandom module page only if its content actually changed."""
    if existing_text is None:
        existing_text = get_fandom_page_text(session, title)

    if existing_text is not None and existing_text.strip() == new_text.strip():
        print(f"No changes detected for {title}; skipping edit.")
        return False

    print(f"Updating page: {title}")
    resp = session.post(
        FANDOM_API_URL,
        data={
            "action": "edit",
            "title": title,
            "text": new_text,
            "token": csrf_token,
            "format": "json",
            "bot": True,
            "summary": summary,
        },
        timeout=REQUEST_TIMEOUT,
    ).json()
    if "error" in resp:
        raise RuntimeError(
            f"Fandom edit failed for {title}: {resp['error'].get('info', 'Unknown error')}"
        )
    print(f"Successfully updated {title} on Fandom.")
    return True


def publish_to_wiki(current_data, store_data):
    """Publishes Module:EventWidget/Data/Current and Module:EventWidget/Data/Store."""
    has_store = store_data.get("counts", {}).get("total", 0) > 0

    if not BOT_USERNAME or not BOT_PASSWORD:
        current_lua = wrap_lua_module(current_data)
        store_lua = wrap_lua_module(store_data) if has_store else None
        print("Warning: Fandom credentials not provided in environment variables. Skipping wiki update.")
        print(f"\n--- {TARGET_CURRENT_PAGE} ({len(current_lua)} bytes) ---")
        print(current_lua)
        if store_lua:
            print(f"\n--- {TARGET_STORE_PAGE} ({len(store_lua)} bytes, preview) ---")
            print(store_lua[:1500] + ("\n... [truncated]" if len(store_lua) > 1500 else ""))
        return

    session, csrf_token = create_authenticated_fandom_session()

    existing_current = get_fandom_page_text(session, TARGET_CURRENT_PAGE)
    stabilize_cycle_timestamps(current_data, existing_current)
    current_lua = wrap_lua_module(current_data)

    edit_fandom_page_if_changed(
        session,
        csrf_token,
        TARGET_CURRENT_PAGE,
        current_lua,
        "Bot: Automated update of live event and rotation data",
        existing_text=existing_current,
    )

    if has_store:
        store_lua = wrap_lua_module(store_data)
        edit_fandom_page_if_changed(
            session,
            csrf_token,
            TARGET_STORE_PAGE,
            store_lua,
            "Bot: Automated update of Lohner's Emporium store data",
        )
    else:
        print(f"Skipping {TARGET_STORE_PAGE} update because PlayFab returned 0 store items.")


if __name__ == "__main__":
    try:
        print("Initializing VT2 Wiki Bot")
        playfab_bundle = fetch_playfab_data()
        current_widget_data = parse_current_data(playfab_bundle)
        store_widget_data = parse_store_data(playfab_bundle)

        publish_to_wiki(current_widget_data, store_widget_data)
        print("Execution completed successfully.")

    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
