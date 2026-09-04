"""
gpbinary.py — Photon GpBinaryV16 LE minimal parser.

Ported from app/lib/gpbinary.js. Parses F3 (GpBinary) packets
into human-readable structures for server-side handling.

Only parses essential packets (is_useful) — noise is ignored.
"""

from typing import Any

# ---------------------------------------------------------------------------
# Constants (mirrors JS)
# ---------------------------------------------------------------------------

OPERATIONS = {
    255: "Join",
    254: "Leave",
    253: "RaiseEvent",
    252: "SetProperties",
    251: "GetProperties",
    250: "ExchangeKeysForEncryption",
    249: "Ping",
    248: "ChangeGroups",
    231: "AuthenticateOnce",
    230: "Authenticate",
    229: "JoinLobby",
    228: "LeaveLobby",
    227: "CreateRoom",
    226: "JoinRoom",
    225: "JoinRandomRoom",
    224: "CancelJoinRandomRoom",
    220: "GetRegions",
}

EVENTS = {
    255: "Join",
    254: "Leave",
    253: "PropertiesChanged",
    226: "AppStats",
    200: "RPC",
    201: "SendSerialize",
    202: "Instantiation",
    203: "CloseConnection",
    204: "Destroy",
    205: "RemoveCachedRPCs",
    206: "SendSerializeReliable",
    207: "DestroyPlayer",
    208: "AssignMaster",
}

PARAMS = {
    255: "RoomName",
    254: "ActorNr",
    253: "TargetActorNr",
    252: "ActorList",
    251: "Properties",
    250: "Broadcast",
    249: "PlayerProperties",
    248: "GameProperties",
    247: "Cache",
    246: "ReceiverGroup",
    245: "Data",
    244: "Code",
    241: "CleanupCacheOnLeave",
    240: "Group",
    239: "Remove/PublishUserId",
    238: "Add",
    237: "SuppressRoomEvents",
    236: "EmptyRoomTTL",
    235: "PlayerTTL",
    234: "EventForward",
    233: "IsComingBack",
    232: "CheckUserOnJoin",
    231: "ExpectedValues",
    230: "Address",
    229: "PeerCount",
    228: "GameCount",
    227: "MasterPeerCount",
    225: "UserId",
    224: "ApplicationId",
    223: "MatchMakingType",
    222: "GameList",
    221: "Token",
    220: "AppVersion",
    218: "Info",
    217: "ClientAuthenticationType",
    216: "ClientAuthenticationParams",
    215: "JoinMode",
    214: "ClientAuthenticationData",
    213: "LobbyName",
    212: "LobbyType",
    210: "Region",
    196: "Cluster",
}

# Essential filters (synced with userscript)
ESSENTIAL_OPS = {225, 226, 227, 230, 231}
ESSENTIAL_EVS = {101, 253, 254, 255}
USEFUL_PROPS = {
    "skid", "BPid", "pid", "Party", "ready", "stG",
    "bz_phase_start", "bz_zone", "bz_phase", "bz_enabled", "bz_pattern",
    "alv", "curScn", "gm", "gv",
    "_pt", "pt",
}
CURSCN_WHITELIST = ["lobby", "game", "zonewars", "zw_lg", "wild", "wwzw", "boxfights", "ww"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hex_to_bytes(hex_str: str) -> bytes:
    """Convert hex string to bytes."""

    return bytes.fromhex(hex_str)


def _utf8_decode(data: bytes) -> str:
    """Decode bytes as UTF-8, fallback to latin1."""

    try:
        return data.decode("utf-8")
    except Exception:
        return data.decode("latin1", errors="ignore")


def is_realistic_curscn(value: str) -> bool:
    """Check if curScn looks realistic (in whitelist)."""

    low = str(value).lower()
    return any(keyword in low for keyword in CURSCN_WHITELIST)


def is_useful_prop(key: str) -> bool:
    """Check if a property key is useful for Rich Presence."""

    return str(key) in USEFUL_PROPS


# ---------------------------------------------------------------------------
# Reader (LE minimal)
# ---------------------------------------------------------------------------

class Reader:
    """Little-endian minimal reader for GpBinary."""

    def __init__(self, data: bytes, little_endian: bool = True, minimal: bool = True):
        self.data = data
        self.pos = 0
        self.little = little_endian
        self.minimal = minimal

    @property
    def left(self) -> int:
        return len(self.data) - self.pos

    def byte(self) -> int:
        val = self.data[self.pos]
        self.pos += 1
        return val

    def bytes(self, count: int) -> bytes:
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def read_count(self) -> int:
        value = self.byte()
        if value >= 0x80:
            value |= self.byte() << 8
        return value

    def count(self) -> int:
        return self.read_count() if self.minimal else self.uint16()

    def str_len(self) -> int:
        return self.read_count() if self.minimal else self.uint16()

    def int_len(self) -> int:
        return self.read_count() if self.minimal else self.uint32()

    def uint16(self) -> int:
        if self.little:
            value = self.data[self.pos] | (self.data[self.pos + 1] << 8)
        else:
            value = (self.data[self.pos] << 8) | self.data[self.pos + 1]
        self.pos += 2
        return value

    def int16(self) -> int:
        value = self.uint16()
        return value - 0x10000 if value > 0x7FFF else value

    def uint32(self) -> int:
        b = self.data
        if self.little:
            value = b[self.pos] | (b[self.pos + 1] << 8) | (b[self.pos + 2] << 16) | (b[self.pos + 3] << 24)
        else:
            value = (b[self.pos] << 24) | (b[self.pos + 1] << 16) | (b[self.pos + 2] << 8) | b[self.pos + 3]
        self.pos += 4
        return value & 0xFFFFFFFF

    def comp_int(self) -> int:
        result = 0
        shift = 0
        while True:
            byte_val = self.byte()
            result |= (byte_val & 0x7F) << shift
            shift += 7
            if not (byte_val & 0x80):
                break
        return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _is_arr_type(type_id: int) -> bool:
    return (type_id & 0x40) != 0


def _read_value(reader: Reader, type_id: int, depth: int, warnings: list) -> dict:
    """Read a single Gp value."""

    if depth > 12:
        warnings.append("max depth")
        return {"type": "deep"}

    # Simple types
    if type_id == 2:
        return {"type": "Boolean", "value": reader.byte() != 0}
    if type_id == 3:
        return {"type": "Byte", "value": reader.byte()}
    if type_id == 4:
        return {"type": "Short", "value": reader.int16()}
    if type_id == 5:
        import struct
        chunk = reader.bytes(4)
        value = struct.unpack("<f", chunk)[0]
        return {"type": "Float", "value": value}
    if type_id == 6:
        import struct
        chunk = reader.bytes(8)
        value = struct.unpack("<d", chunk)[0]
        return {"type": "Double", "value": value}
    if type_id == 7:
        length = reader.str_len()
        return {"type": "String", "value": _utf8_decode(reader.bytes(length))}
    if type_id == 8:
        return {"type": "Null", "value": None}
    if type_id == 9:
        return {"type": "Int", "value": reader.comp_int()}
    if type_id == 10:
        return {"type": "Long", "value": reader.comp_int()}
    if type_id == 11:
        return {"type": "Int", "value": reader.byte()}
    if type_id == 12:
        return {"type": "Int", "value": -reader.byte()}
    if type_id == 13:
        return {"type": "Int", "value": reader.uint16()}
    if type_id == 14:
        return {"type": "Int", "value": -reader.uint16()}
    if type_id == 15:
        return {"type": "Long", "value": reader.byte()}
    if type_id == 16:
        return {"type": "Long", "value": -reader.byte()}
    if type_id == 17:
        return {"type": "Long", "value": reader.uint16()}
    if type_id == 18:
        return {"type": "Long", "value": -reader.uint16()}
    if type_id == 19:
        code = reader.byte()
        remaining = min(reader.left, 96)
        return {"type": f"Custom#{code}", "value": {"hex": reader.bytes(remaining).hex(), "rest": remaining}}
    if type_id == 20:
        return _read_dictionary(reader, depth, warnings)
    if type_id == 21:
        return _read_hashtable(reader, depth, warnings)
    if type_id == 23:
        return _read_object_array(reader, depth, warnings)
    if type_id == 27:
        return {"type": "Boolean", "value": False}
    if type_id == 28:
        return {"type": "Boolean", "value": True}
    if type_id == 29:
        return {"type": "Short", "value": 0}
    if type_id == 30:
        return {"type": "Int", "value": 0}
    if type_id == 31:
        return {"type": "Long", "value": 0}
    if type_id == 32:
        return {"type": "Float", "value": 0}
    if type_id == 33:
        return {"type": "Double", "value": 0}
    if type_id == 34:
        return {"type": "Byte", "value": 0}

    # Array types
    if _is_arr_type(type_id):
        return _read_array(reader, type_id, depth, warnings)

    warnings.append(f"unknown GpType {type_id} @{reader.pos}")
    return {"type": f"Unknown({type_id})", "value": reader.bytes(min(32, reader.left)).hex()}


def _read_array(reader: Reader, type_id: int, depth: int, warnings: list) -> dict:
    """Read an array value."""

    elem = type_id & 0x3F

    # Byte array
    if elem == 3:
        length = reader.int_len()
        if length < 0 or length > 4_000_000:
            warnings.append(f"byte[] len {length} invalid")
            return {"type": "ByteArray", "value": {"hex": "?", "len": -1}}
        if length > reader.left:
            warnings.append(f"byte[] len {length} > remaining {reader.left}")
            return {"type": "ByteArray", "value": {"hex": reader.bytes(reader.left).hex(), "len": reader.left}}
        chunk = reader.bytes(length)
        # Try to decode as ASCII
        ascii = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk[:64])
        return {"type": "ByteArray", "value": {"hex": chunk.hex(), "len": length, "ascii": ascii}}

    # Other arrays
    count = reader.count()
    eff_elem = elem

    if type_id == 0x40:
        count = reader.count()
        eff_elem = reader.byte()

    if count > 65535:
        warnings.append(f"array len {count} invalid")
        return {"type": "Array", "value": reader.bytes(min(64, reader.left)).hex()}

    items = []
    for _ in range(count):
        items.append(_read_value(reader, eff_elem, depth + 1, warnings))

    return {"type": f"Array[{eff_elem}]", "value": items}


def _read_hashtable(reader: Reader, depth: int, warnings: list) -> dict:
    """Read a hashtable."""

    count = reader.count()
    if count > 65535:
        warnings.append("hashtable len invalid")
        return {"type": "Hashtable", "value": reader.bytes(min(64, reader.left)).hex()}

    result = {}
    for _ in range(count):
        key_type = reader.byte()
        key = _read_value(reader, key_type, depth + 1, warnings)
        val_type = reader.byte()
        value = _read_value(reader, val_type, depth + 1, warnings)
        key_str = str(key["value"] if isinstance(key["value"], str) else key["value"])
        result[key_str] = value

    return {"type": "Hashtable", "value": result}


def _read_dictionary(reader: Reader, depth: int, warnings: list) -> dict:
    """Read a dictionary."""

    key_type = reader.byte()
    val_type = reader.byte()
    count = reader.count()

    if count > 65535:
        warnings.append("dict len invalid")
        return {"type": "Dictionary", "value": reader.bytes(min(64, reader.left)).hex()}

    result = {}
    for _ in range(count):
        eff_key = reader.byte() if key_type in (0, 42) else key_type
        key = _read_value(reader, eff_key, depth + 1, warnings)
        eff_val = reader.byte() if val_type in (0, 42) else val_type
        value = _read_value(reader, eff_val, depth + 1, warnings)
        key_str = str(key["value"] if isinstance(key["value"], str) else key["value"])
        result[key_str] = value

    return {"type": "Dictionary", "value": result}


def _read_object_array(reader: Reader, depth: int, warnings: list) -> dict:
    """Read an object array."""

    count = reader.count()
    if count > 65535:
        warnings.append("objarray len invalid")
        return {"type": "ObjectArray", "value": reader.bytes(min(64, reader.left)).hex()}

    items = []
    for _ in range(count):
        type_id = reader.byte()
        items.append(_read_value(reader, type_id, depth + 1, warnings))

    return {"type": "ObjectArray", "value": items}


def _read_params(reader: Reader, count: int, depth: int, warnings: list) -> list:
    """Read operation/event params."""

    params = []
    for i in range(count):
        if reader.left <= 0:
            warnings.append(f"param {i} past end")
            break
        key = reader.byte()
        type_id = reader.byte()
        value = _read_value(reader, type_id, depth + 1, warnings)
        params.append({
            "key": key,
            "keyName": PARAMS.get(key, f"p{key}"),
            "value": value,
        })
    return params


def _read_operation_request(reader: Reader, depth: int, warnings: list) -> dict:
    op_code = reader.byte()
    param_count = reader.count()
    params = _read_params(reader, param_count, depth, warnings)
    return {
        "kind": "OperationRequest",
        "opCode": op_code,
        "opName": OPERATIONS.get(op_code, f"op#{op_code}"),
        "params": params,
    }


def _read_operation_response(reader: Reader, depth: int, warnings: list) -> dict:
    op_code = reader.byte()
    return_code = reader.int16()
    debug_type = reader.byte()
    debug_msg = None

    if debug_type == 7:
        length = reader.count()
        debug_msg = _utf8_decode(reader.bytes(length))
    elif debug_type not in (0, 8):
        warnings.append(f"debug field type {debug_type}")

    param_count = reader.count()
    params = _read_params(reader, param_count, depth, warnings)

    return {
        "kind": "OperationResponse",
        "opCode": op_code,
        "opName": OPERATIONS.get(op_code, f"op#{op_code}"),
        "returnCode": return_code,
        "debugMsg": debug_msg,
        "params": params,
    }


def _read_event_data(reader: Reader, depth: int, warnings: list) -> dict:
    ev_code = reader.byte()
    param_count = reader.count()
    params = _read_params(reader, param_count, depth, warnings)
    return {
        "kind": "EventData",
        "evCode": ev_code,
        "evName": EVENTS.get(ev_code, f"event#{ev_code}"),
        "params": params,
    }


def parse_message(data: bytes, warnings: list) -> dict | None:
    """Parse a single Photon message."""

    if len(data) < 2:
        warnings.append("msg <2B")
        return None

    sign = data[0]
    msg_type = data[1]

    reader = Reader(data, little_endian=True, minimal=True)
    reader.pos = 2

    is_encrypted = (sign & 0x0F) in (0x04, 0x05)

    type_names = {
        1: "Session", 2: "OperationRequest", 3: "OperationResponse",
        4: "EventData", 5: "InitMessage", 6: "InitRequest", 7: "InitResponse",
    }

    result = {
        "sign": f"{sign:02X}",
        "type": msg_type,
        "typeName": type_names.get(msg_type, f"type#{msg_type}"),
        "encrypted": is_encrypted,
    }

    if msg_type == 2:
        result["req"] = _read_operation_request(reader, 0, warnings)
    elif msg_type == 3:
        result["resp"] = _read_operation_response(reader, 0, warnings)
    elif msg_type == 4:
        result["ev"] = _read_event_data(reader, 0, warnings)
    else:
        result["rawTail"] = data[2:2+96].hex()

    # Find next message boundary (F3/F4)
    next_pos = -1
    for i in range(reader.pos, len(data)):
        if data[i] in (0xF3, 0xF4):
            next_pos = i
            break

    result["consumed"] = next_pos if next_pos >= 0 else len(data)
    result["warnings"] = warnings

    return result


# ---------------------------------------------------------------------------
# High-level helpers (unwrap, is_useful, human_sentence)
# ---------------------------------------------------------------------------

def _unwrap(value: Any) -> Any:
    """Unwrap a Gp value to plain Python."""

    if not isinstance(value, dict) or "type" not in value:
        return value

    type_name = value["type"]
    inner = value["value"]

    if type_name in ("String", "Int", "Long", "Byte", "Short", "Float", "Double", "Boolean"):
        return inner

    if type_name == "Hashtable" or type_name.startswith("Dictionary"):
        return {k: _unwrap(v) for k, v in (inner or {}).items()}

    if type_name == "ByteArray":
        # Try UTF-8
        try:
            text = bytes.fromhex(inner["hex"]).decode("utf-8")
            if text.strip() and all(32 <= ord(c) < 127 for c in text.strip()) and len(text) >= 2:
                return text
        except Exception:
            pass
        return inner["hex"][:64]

    if type_name.startswith("Array") or type_name == "ObjectArray":
        return [_unwrap(v) for v in (inner or [])]

    return inner


def parse_gpbinary(hex_str: str) -> dict | None:
    """
    Parse a hex-encoded GpBinary packet.

    Returns a clean dict with opName/evName/gm/curScn etc., or None on failure.
    """

    try:
        data = _hex_to_bytes(hex_str)
        warnings: list = []
        msg = parse_message(data, warnings)

        if not msg:
            return None

        result: dict[str, Any] = {
            "kind": msg["typeName"],
            "raw": msg,
        }

        # Operation request
        if "req" in msg:
            req = msg["req"]
            result["op"] = req["opCode"]
            result["opName"] = req["opName"]
            result["params"] = {}
            for param in req["params"]:
                result["params"][param["keyName"]] = param["value"]

            # Extract useful fields
            if "GameProperties" in result["params"]:
                props = _unwrap(result["params"]["GameProperties"])
                if isinstance(props, dict):
                    if "gm" in props:
                        result["gm"] = props["gm"]
                    if "Party" in props:
                        result["Party"] = props["Party"]

            if "PlayerProperties" in result["params"]:
                result["playerProps"] = _unwrap(result["params"]["PlayerProperties"])

        # Event data
        if "ev" in msg:
            ev = msg["ev"]
            result["evCode"] = ev["evCode"]
            result["evName"] = ev["evName"]
            result["params"] = {}
            for param in ev["params"]:
                result["params"][param["keyName"]] = param["value"]

            # Properties
            props_param = None
            for param in ev["params"]:
                if param["keyName"] in ("Properties", "PlayerProperties"):
                    props_param = param
                    break

            if props_param and props_param["value"] and props_param["value"].get("value"):
                unwrapped = _unwrap(props_param["value"])
                if isinstance(unwrapped, dict):
                    result["properties"] = unwrapped
                    if "gm" in unwrapped:
                        result["gm"] = unwrapped["gm"]
                    if "curScn" in unwrapped:
                        result["curScn"] = unwrapped["curScn"]
                    if "Party" in unwrapped:
                        result["Party"] = unwrapped["Party"]

                    # For Join 255, also expose as playerProps
                    if ev["evCode"] == 255:
                        result["playerProps"] = unwrapped

            # Killfeed
            data_param = next((p for p in ev["params"] if p["keyName"] == "Data"), None)
            if data_param and data_param["value"]["type"] == "String":
                result["killfeed"] = data_param["value"]["value"]

        return result

    except Exception:
        return None


# Actor name cache (for server-side human sentences)
_actor_cache: dict[int, str] = {}


def cache_actor_name(actor_nr: int, name: str) -> None:
    if actor_nr is not None and name:
        _actor_cache[int(actorNr)] = str(name)


def get_actor_name(actor_nr: int) -> str | None:
    return _actor_cache.get(int(actor_nr))


# ---------------------------------------------------------------------------
# Useful / human sentence helpers
# ---------------------------------------------------------------------------

def is_useful(gpb: dict | None) -> bool:
    """Check if a parsed packet is useful for Rich Presence."""

    if not gpb:
        return False

    # Essential ops
    if gpb.get("opName") and gpb.get("op") in ESSENTIAL_OPS:
        return True

    # Essential events
    ev_name = gpb.get("evName")
    ev_code = gpb.get("evCode")

    if ev_name:
        if ev_code == 101:
            return bool(gpb.get("killfeed"))
        if ev_code in (254, 255):
            return True
        if ev_code == 253:
            props = gpb.get("properties") or {}
            return any(is_useful_prop(k) for k in props.keys())

    return False


def _strip_color_tags(text: str) -> str:
    """Remove <color> tags from killfeed text — handles truncated </col and <#hex>.

    Usernames may legitimately contain '<' (e.g. 'jesus loves u <3'), so the
    fallback only removes known formatting tags and truncated tail fragments —
    never a generic <...>, which would eat username content like '<3</b>'.
    """
    import re
    # Exact tags
    text = re.sub(r"<color[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</color>", "", text, flags=re.IGNORECASE)
    # Truncated </col, </color without >, and hex colors like <color=#FC2> or <#000520>
    text = re.sub(r"</?color[^>]*>?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<#?[0-9a-fA-F]{3,6}>", "", text)
    # Other known formatting tags (<b>, <i>, <size=..>) — NOT a generic <...> fallback
    text = re.sub(r"</?(?:b|i|u|s|size|align)[^>]*>?", "", text, flags=re.IGNORECASE)
    # Truncated tail fragment at end of string (packet cut mid-tag, e.g. 'Bob</col')
    text = re.sub(r"</?[a-zA-Z#][^<>]*$", "", text)
    text = re.sub(r"<$", "", text)
    return text.strip()


def _has_tag_fragment(s: str) -> bool:
    """True if s still contains something tag-like (truncated packet), False for
    innocent content like the '<3' in 'jesus loves u <3' ('<' + digit, no tag shape)."""
    import re
    return bool(re.search(r"<\/?[a-zA-Z#]", s) or s.endswith("<"))


def parse_killfeed_text(raw: str) -> dict:
    """Parse killfeed text into killer/victim/type — drops empty victims and truncated tags."""
    clean = _strip_color_tags(raw).strip()
    result = {"raw": raw, "text": clean, "type": None, "killer": None, "victim": None}

    if " killed " in clean:
        killer, victim = clean.split(" killed ", 1)
        killer, victim = killer.strip(), victim.strip()
        # Drop truncated-tag leftovers (e.g. "jew6led killed <empty>"), but keep
        # innocent '<' in usernames (e.g. 'jesus loves u <3')
        if not killer or not victim or _has_tag_fragment(killer) or _has_tag_fragment(victim):
            return {"raw": raw, "text": clean, "type": "other", "killer": None, "victim": None}
        result["type"] = "kill"
        result["killer"] = killer
        result["victim"] = victim
    elif " eliminated " in clean:
        # DF elimination variant ("X eliminated Y") — same as a kill
        killer, victim = clean.split(" eliminated ", 1)
        killer, victim = killer.strip(), victim.strip()
        if not killer or not victim or _has_tag_fragment(killer) or _has_tag_fragment(victim):
            return {"raw": raw, "text": clean, "type": "other", "killer": None, "victim": None}
        result["type"] = "kill"
        result["killer"] = killer
        result["victim"] = victim
    elif " knocked " in clean:
        killer, victim = clean.split(" knocked ", 1)
        killer, victim = killer.strip(), victim.strip()
        if not killer or not victim or _has_tag_fragment(killer) or _has_tag_fragment(victim):
            return {"raw": raw, "text": clean, "type": "other", "killer": None, "victim": None}
        result["type"] = "knock"
        result["killer"] = killer
        result["victim"] = victim
    elif " Left Match" in clean:
        killer = clean.replace(" Left Match", "").strip()
        if not killer or _has_tag_fragment(killer):
            return {"raw": raw, "text": clean, "type": "other", "killer": None, "victim": None}
        result["type"] = "leave"
        result["killer"] = killer
    elif " joined" in clean:
        killer = clean.replace(" joined", "").strip()
        if not killer or _has_tag_fragment(killer):
            return {"raw": raw, "text": clean, "type": "other", "killer": None, "victim": None}
        result["type"] = "join"
        result["killer"] = killer
    else:
        result["type"] = "other"

    return result


def human_sentence(gpb: dict | None) -> str | None:
    """Generate a human-readable sentence for a packet."""

    if not gpb or not is_useful(gpb):
        return None

    # Authenticate
    if gpb.get("opName") in ("Authenticate", "AuthenticateOnce"):
        return "Authenticate: login packet one-time use"

    # CreateRoom
    if gpb.get("opName") == "CreateRoom":
        room = gpb.get("params", {}).get("RoomName")
        if room:
            room = _unwrap(room)
        party = "party yes" if gpb.get("Party") else "party no"
        gm = f" in {gpb['gm']}" if gpb.get("gm") else ""
        return f"Room created: code {room} — {party}{gm}" if room else f"Room created — {party}{gm}"

    # Join
    if gpb.get("opName") in ("JoinRoom", "JoinRandomRoom"):
        gm = f" for {gpb['gm']}" if gpb.get("gm") else ""
        props = gpb.get("playerProps") or {}
        name = props.get("255") or props.get("pfid") or "player"
        skin = f" wearing {props['skid']}" if props.get("skid") else ""
        return f"Joining room{gm} as {name}{skin}"

    # Join event (player joined)
    if gpb.get("evName") == "Join":
        props = gpb.get("properties") or gpb.get("playerProps") or {}
        name = props.get("255") or props.get("pfid") or "A player"
        skin = props.get("skid")
        skin_part = f" wearing {skin}" if skin else ""
        return f"Joined: {name}{skin_part} joined the room"

    # Leave
    if gpb.get("evName") == "Leave":
        return "Left: A player left the room"

    # PropertiesChanged
    if gpb.get("evName") == "PropertiesChanged":
        props = gpb.get("properties") or {}
        parts = []

        if "skid" in props:
            parts.append(f"skin changed to {props['skid']}")
        elif "pid" in props:
            parts.append(f"pickaxe changed to {props['pid']}")
        elif "BPid" in props:
            parts.append(f"backpack changed to {props['BPid']}")

        if "Party" in props:
            parts.append("now in party" if props["Party"] else "left party")

        if "ready" in props:
            parts.append("is ready" if props["ready"] else "not ready")

        if "stG" in props and 0 <= int(props["stG"]) <= 600:
            parts.append(f"zone starts in {props['stG']} seconds")

        if "bz_phase_start" in props and 0 <= int(props["bz_phase_start"]) <= 600:
            parts.append(f"zone starts in {props['bz_phase_start']} seconds")

        if "bz_zone" in props:
            parts.append(f"zone {props['bz_zone']}")

        if "bz_phase" in props and "bz_zone" not in props:
            parts.append(f"phase {props['bz_phase']}")

        if "alv" in props:
            parts.append(f"{'is alive' if props['alv'] else 'is eliminated'}")

        if "curScn" in props and is_realistic_curscn(props["curScn"]):
            parts.append(f"scene is now {props['curScn']}")
        elif "curScn" in props:
            return None

        if "gm" in props and "curScn" not in props:
            parts.append(f"mode is now {props['gm']}")

        if not parts:
            return None

        return f"Props changed: {', '.join(parts)}"

    # Killfeed
    if gpb.get("killfeed"):
        parsed = parse_killfeed_text(gpb["killfeed"])
        if parsed["type"] == "kill":
            return f"Killfeed: {parsed['killer']} killed {parsed['victim']}"
        if parsed["type"] == "knock":
            return f"Killfeed: {parsed['killer']} knocked {parsed['victim']}"
        return f"Killfeed: {parsed['text']}"

    return None
