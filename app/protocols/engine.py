# -*- coding: utf-8 -*-
# protocols/engine.py — FF squad engine (Garena protocol stack), web-driven
# Ported from original terminal main.py. All protocol logic below is untouched;
# only the terminal menu / stdin flow and the dead local dashboard stub were cut.
import sys, asyncio, httpx, random, json, socket, struct, time, os, uuid
import itertools, secrets, traceback, base64
import requests, hmac, string, hashlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any, Callable

import warnings
warnings.filterwarnings("ignore")
try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from message_ids import MESSAGE_ID_TO_NAME
import FYTTeam_pb2
import FYTFF_pb2
import thunderFF_pb2

# FILL: real BR matchmaking selectors for client_version 1.132.6 / code 2019121229.
# Not present anywhere in this dump — pull from a packet capture of a real client
# starting solo/duo/squad BR, or from the APK's protocol config (il2cpp/smali dump).
BR_MODE_ID = 1
BR_MAP_ID = 1

# =========================== CONFIG ===========================
# Dropped entirely during the terminal->web port — every name below is read
# throughout functional_battle_royale/run_account_worker/_team_coordinator
# and was undefined (NameError, swallowed by broad except Exception blocks
# -> silent no-op) until this block was restored.
ACCOUNTS_FILE = "accounts.json"
TOKEN_CACHE_FILE = "token_cache.json"
DEVICES_FILE = "devices.json"
TOKEN_CACHE_TTL = 4500

START_MATCH_INTERVAL    = 1.0
NEW_MATCH_DELAY         = 1.0
MAX_CONCURRENT_MATCHES  = 5       # parallel matches per account (solo-speed engine)
MAX_MATCH_DURATION      = 70000
MATCH_IDLE_TIMEOUT      = 12.0
EXP_REFRESH_INTERVAL    = 30.0
EXP_REFRESH_AFTER_MATCH_DELAY = 5.0
MAX_CONSECUTIVE_PARSE_FAILURES = 5
NON_MATCH_RECONNECT_DELAY      = 0.5

ANTI_AFK_MIN_INTERVAL  = 1.8
ANTI_AFK_MAX_INTERVAL  = 3.5

MATCH_SEARCH_TIMEOUT   = 45.0
MATCH_WATCHDOG_CHECK   = 15.0
REGION_ROTATION_LIST   = ["ID", "SG", "TH", "MY", "VN", "PH"]
REGION_ROTATE_EVERY_N  = 2
MAX_SEARCH_ATTEMPTS    = 12

SAFE_PLAY_MODE        = True
BYPASS_TIMING_JITTER  = True
BYPASS_PACKET_JITTER  = True
BYPASS_HELLO_DELAY    = True
BYPASS_KA_JITTER      = True

ENABLE_HTTP_PING = True
PING_INTERVAL    = 20.0
PING_TIMEOUT     = 8.0
PING_URL         = "https://loginbp.ppmainecoonghj.com/Ping"
PING_REAL_IP     = "103.109.214.211"

FALLBACK_UID = ""
FALLBACK_PASSWORD = ""

ENABLE_SPEED_MOVEMENT       = False   # fake movement band - anti-AFK fire chalu hai
MOVEMENT_SPEED_MIN          = 1.0
MOVEMENT_SPEED_MAX          = 1.4
MOVEMENT_PATTERN            = "circle"
MOVEMENT_TICK_MS            = 240
MOVEMENT_RADIUS             = 180
MOVEMENT_UPDATE_SPEED_EVERY = 4.0
MOVEMENT_STATE_IDLE_CHANCE   = 0.10
MOVEMENT_STATE_WALK_CHANCE   = 0.28
MOVEMENT_STATE_RUN_CHANCE    = 0.32
MOVEMENT_STATE_SPRINT_CHANCE = 0.10
MOVEMENT_STATE_JUMP_CHANCE   = 0.08
MOVEMENT_STATE_CROUCH_CHANCE = 0.06
MOVEMENT_STATE_PRONE_CHANCE  = 0.06

# =========================== SQUAD CONFIG ===========================
# TEAM_SIZE is the fixed squad capacity (BR squad = 4 seats); TEAM_BOTS /
# USER_GAME_UID / CLAN_ID / AUTO_JOIN_CLAN are the per-client knobs — kept
# as module globals for solo/legacy callers, but every live read in the hot
# path below resolves them from account_data first (see _cfg() below) so
# N concurrent clients never race on these.
TEAM_MODE               = True
TEAM_SIZE               = 4          # total squad (bots + captain)
TEAM_BOTS               = 4          # default normal-mode bot count
USER_GAME_UID           = ""         # default captain uid (with_me mode)
CLAN_ID                 = ""
CLAN_STATE_FILE         = "clan_state.json"
AUTO_JOIN_CLAN          = False
TEAM_PEER_WAIT_TIMEOUT  = 15.0
TEAM_CREATE_WAIT        = 1.0
TEAM_INVITE_CYCLE_WAIT  = 1.0
TEAM_INVITE_MAX_TIME    = 45.0
TEAM_HEARTBEAT_INTERVAL = 2.0
TEAM_IDLE_ITERS_TEAM    = 120
TEAM_MEMBER_START_DELAY = 3.0
TEAM_FINAL_WAIT         = 1.0
TEAM_READY_DELAY        = 1.0
_accept_friend_request  = True
DEBUG_TEAM_PACKETS      = False

def _cfg(account_data: Optional[Dict[str, Any]], key: str, fallback):
    """Per-client override lookup. start_client() stamps team_bots/
    captain_uid/clan_id/auto_join_clan onto each account's `ad` dict at
    spawn time (see start_client) so concurrent clients never read each
    other's squad config off the shared globals above."""
    if account_data and key in account_data and account_data[key] not in (None, ""):
        return account_data[key]
    return fallback

try:
    import reqClan_pb2
    _CLAN_PB_AVAILABLE = True
except Exception:
    _CLAN_PB_AVAILABLE = False

# =========================== LIVE STATE ===========================
# Replaces the old _StubState. Real state, pushed to the web layer via
# an asyncio broadcast queue instead of printed to a terminal.

class BotState:
    """Shared live state for every running account across every client/escort.
    account entries keyed by in-game account_id (str)."""

    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        self.refresh_callbacks: Dict[str, Callable] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 500
        self._subscribers: List[asyncio.Queue] = []
        self._paused: set = set()

    # ---- pub/sub for the web layer (WS) ----
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _publish(self, event: Dict[str, Any]):
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(event)
                except Exception:
                    pass

    def log(self, message: str, level: str = "info", uid: Optional[str] = None, client_id: Optional[str] = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "ts": time.time(),
            "level": level,
            "message": message,
            "uid": str(uid) if uid else None,
            "client_id": client_id,
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)
        self._publish({"type": "log", "data": entry})

    def register_account(self, uid, nickname="", region="ID", level=1, exp=0, likes=0,
                          client_id: Optional[str] = None, slot: Optional[int] = None,
                          role: str = "bot", **_):
        uid_s = str(uid)
        existing = self.accounts.get(uid_s, {})
        self.accounts[uid_s] = {
            **existing,
            "uid": uid_s,
            "nickname": nickname or existing.get("nickname") or f"Player_{uid_s[:6]}",
            "region": region or existing.get("region", "ID"),
            "level": level,
            "exp": exp,
            "likes": likes,
            "client_id": client_id if client_id is not None else existing.get("client_id"),
            "slot": slot if slot is not None else existing.get("slot"),
            "role": role,
            "status": existing.get("status", "CONNECTING"),
            "match_state": existing.get("match_state", "IDLE"),
            "matches_played": existing.get("matches_played", 0),
            "last_updated": time.strftime("%H:%M:%S"),
        }
        self._publish({"type": "account", "data": self.accounts[uid_s]})

    def update_status(self, uid, status: str, active_matches: int = 0):
        """Body calls this with either a coarse lifecycle tag (CONNECTING/
        ONLINE/ERROR/OFFLINE/IN_MATCH) or a free-text team-forming tag like
        'IN_TEAM (3/4) [LEADER]' / 'JOINING (2/4) [MEMBER]'. We normalize
        both into {status, match_state} for the per-slot filling/standing/
        in-match UI."""
        uid_s = str(uid)
        if uid_s not in self.accounts:
            return
        acc = self.accounts[uid_s]
        acc["last_updated"] = time.strftime("%H:%M:%S")
        acc["status_raw"] = status
        if status in ("CONNECTING", "ERROR", "OFFLINE"):
            acc["status"] = status
            acc["match_state"] = "IDLE"
        elif status == "IN_MATCH" or active_matches:
            acc["status"] = "ONLINE"
            acc["match_state"] = "IN_MATCH"
        elif status == "ONLINE":
            acc["status"] = "ONLINE"
            acc["match_state"] = "IDLE"
        elif status.startswith("JOINING"):
            acc["status"] = "ONLINE"
            acc["match_state"] = "SEARCHING"
        elif status.startswith("IN_TEAM"):
            acc["status"] = "ONLINE"
            acc["match_state"] = "SEARCHING"
        else:
            acc["status"] = "ONLINE"
            acc["match_state"] = "IDLE"
        self._publish({"type": "account", "data": acc})

    def update_match_state(self, uid, match_state: str):
        uid_s = str(uid)
        if uid_s in self.accounts:
            self.accounts[uid_s]["match_state"] = match_state
            self._publish({"type": "account", "data": self.accounts[uid_s]})

    def update_exp(self, uid, exp: int, level: int):
        uid_s = str(uid)
        if uid_s in self.accounts:
            self.accounts[uid_s]["exp"] = exp
            self.accounts[uid_s]["level"] = level
            self._publish({"type": "account", "data": self.accounts[uid_s]})

    def increment_match(self, uid):
        uid_s = str(uid)
        if uid_s in self.accounts:
            self.accounts[uid_s]["matches_played"] = self.accounts[uid_s].get("matches_played", 0) + 1
            self._publish({"type": "account", "data": self.accounts[uid_s]})

    def is_paused(self, uid):
        return str(uid) in self._paused

    def toggle_pause(self, uid):
        uid_s = str(uid)
        if uid_s in self._paused:
            self._paused.discard(uid_s)
            return False
        self._paused.add(uid_s)
        return True

    def accounts_for_client(self, client_id: str) -> List[Dict[str, Any]]:
        return [a for a in self.accounts.values() if a.get("client_id") == client_id]

    def remove_account(self, uid):
        self.accounts.pop(str(uid), None)
        self._publish({"type": "account_removed", "data": {"uid": str(uid)}})


bot_state = BotState()

async def start_web_dashboard(*a, **k):
    # no-op: the FastAPI app in app/ is the dashboard now.
    return None

def bypass_jitter(base, spread=0.35):
    if not (SAFE_PLAY_MODE and BYPASS_TIMING_JITTER): return base
    return max(0.05, base + random.uniform(-spread, spread))

def bypass_packet_jitter(base, spread_ratio=0.35):
    if not (SAFE_PLAY_MODE and BYPASS_PACKET_JITTER): return base
    return max(0.1, base + random.uniform(-base * spread_ratio, base * spread_ratio))

def bypass_hello_delay():
    if not (SAFE_PLAY_MODE and BYPASS_HELLO_DELAY): return 0.0
    return random.uniform(0.01, 0.10)

def bypass_ka_jitter(base=3.0):
    if not (SAFE_PLAY_MODE and BYPASS_KA_JITTER): return base
    return max(2.0, base + random.uniform(-0.5, 0.8))

def bypass_human_reconnect_delay(attempt):
    if not (SAFE_PLAY_MODE and BYPASS_TIMING_JITTER): return min(attempt * 0.5, 2.0)
    return max(0.05, min(attempt * 0.5, 2.0) + random.uniform(-0.4, 0.7))

# =========================== GUEST GEN (FYT_) ===========================
GEN_uVer = "2018.4.12f1"
GEN_GameVer = "2.132.4"
GEN_ReleaseVer = "OB55"
GEN_UaUnity = f"UnityPlayer/{GEN_uVer} (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"
GEN_Hkey = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"
GEN_Akey = GEN_Hkey.encode()

GEN_SharedCookie = (
    "datadome=nmP601L~4DvFBNemgIhE2d3Y~tR68_GHUaV8WhUdKLhjg74S_YsRWpOCnYL8cNtz"
    "58Olr_SEm1GSttQImpJO1j15k2AGGY~bFDp0hfrk6p0Dv_JZSGXJUGhhY_QsQ~5H"
)

GEN_UserAgents = [
    "GarenaMSDK/4.0.42(RMX3710 ;Android 15;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(RMX3085 ;Android 13;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(SM-A546E ;Android 14;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(SM-A235F ;Android 13;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(CPH2449 ;Android 14;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(V2307 ;Android 14;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(Pixel 6 ;Android 13;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(2201117TG  ;Android 14;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(NTH-NX9 ;Android 13;hi;IND;app 2.132.1 2019118525;)",
    "GarenaMSDK/4.0.42(X6836 ;Android 13;hi;IND;app 2.132.1 2019118525;)",
]

GEN_GgreBlob = bytes.fromhex(
    "47475245010101006d020000832363e79711efb391707fad6381be1acaef57ae198e95d7005ec74d16dd406b7d5356f25b38699671436eb29e6c5ffc6107073ea5fbe9795714af5661bed317bb90d6962420a1edb389ad19115a1b6eeb5205173fd7804b7d23418697ed1c288f66acf0701640c7a613ae69464d9404e38b24d595be1c97c4eac6772952fccbc6714a9472629d0609aeb5de0afb1c078390195a8b6cf96f30b27d7c5120f43af278a150830d3e91f9fa89098d0be519e3802b826e67640293352402ad27479581fcf6e5997f4fc7709bdcb129dc803d1c254b8d5bca4cff1a3d021f169b4e133aca832f1c227baaa2f78ff0ce59b9934af15724766aba47d99de62dc3232f04920cc9dabc292bee3ad4c342f3c7aa94021f7b468417a21eb57bb45b23c27de9e3c5c972012cccfc4e4794d0a6d5368bfcbec8a8ba327815d20f9df47c7eb3c99a18e6747cc3ceb1823a8fcf57afcd5aaf3ca536b76a77c3c218c8dc1860a22fcb87a5a9f8c017745e523a1a4ab8a836993515b6040a33936c39f07b2641885cc90e218555c9539ba541896fdca071175a8cbd8157615b788efeca65bb5b81aeb5cdb5f0712d6e1a50016656d6409e1888546c40fd2f01aab95a05da798b48c1bb9fa01b28d949f903bf3428be1506bb724b13b8e04469bda807ca56d5cdd7d78bd804e9041ebef8e992ff41d441196430efb5ce5ebc4c09f08abd966019132e24cfb54d89fb48d84042963501640b6eec1658d8b1455887cbeae2884cd441ffe0ee1d25b353e83668bc870c9ce8e91991a737e962f8d411d08537352b6fb50c1f65596c9faf1262d9a38e9fb509ecc036b3b0e410b7ef100006c692ca3ba025cda9a13dc649d883bee4c68b051161067d432d080e9e6907e11d895aae0d9e2aa9ebe945e3dee4392bfc353fe98f99895b3bbc69f4"
)

GEN_LoginDataApi = "http://148.113.25.200:2849/getlogindata"


def _gen_veraddr():
    r = httpx.get(
        "https://version.ggwhitehawk.com/live/ver.php",
        params={
            "version": "1.132.3",
            "lang": "hi",
            "device": "android",
            "channel": "android",
            "appstore": "googleplay",
            "region": "IND",
            "whitelist_version": "1.3.0",
            "whitelist_sp_version": "1.0.0",
        },
        timeout=15, verify=False,
    )
    d = r.json()
    Ob = d["remote_version"]
    Ver = d["latest_release_version"]
    LoginBp = d["server_url"].replace("https://", "").replace("http://", "").rstrip("/")
    return Ver, Ob, LoginBp


GEN_NAME_PREFIX = "FYT "        #  naye accounts is prefix se banenge


def _gen_name():
    chars = string.ascii_lowercase + string.digits
    return GEN_NAME_PREFIX + ''.join(random.choices(chars, k=4))


def _gen_pwd():
    return ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=64))


def _gen_sig(payload):
    return hmac.new(GEN_Akey, payload.encode(), hashlib.sha256).hexdigest()


def _gen_reg(app_ver):
    Pwd = _gen_pwd()
    UaStr = random.choice(GEN_UserAgents)
    Url = "https://100067.connect.garena.com/api/v2/oauth/guest:register"
    Data = {"app_id": 100067, "client_type": 2, "password": Pwd, "source": 2}
    Text = json.dumps(Data, separators=(',', ':'))
    Sig = _gen_sig(Text)
    Hdr = {
        "User-Agent": UaStr,
        "Authorization": f"Signature {Sig}",
        "Cookie": GEN_SharedCookie,
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "Host": "100067.connect.garena.com",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
    }
    r = requests.post(Url, headers=Hdr, data=Text, verify=False, timeout=25)
    j = r.json()
    if "data" in j and "uid" in j["data"]:
        return j["data"]["uid"], Pwd, Sig
    raise Exception(f"Reg failed: {j}")


def _gen_accs(uid, pwd, app_ver):
    UaStr = random.choice(GEN_UserAgents)
    Url = "https://100067.connect.garena.com/api/v2/oauth/guest/token:grant"
    Hdr = {
        "User-Agent": UaStr,
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "Host": "100067.connect.garena.com",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
    }
    Prm = {
        "client_id": 100067,
        "client_secret": GEN_Hkey,
        "client_type": 2,
        "device_id": "",
        "password": pwd,
        "response_type": "token",
        "uid": uid
    }
    payload = json.dumps(Prm, separators=(',', ':'))
    r = requests.post(Url, headers=Hdr, data=payload, verify=False, timeout=25)
    j = r.json()
    if "data" in j and "access_token" in j["data"]:
        return j["data"]["access_token"], j["data"]["open_id"]
    raise Exception(f"Token grant failed: {j}")


def _gen_crvr(n):
    if n < 0: return b''
    H = []
    while True:
        S = n & 0x7F
        n >>= 7
        if n: S |= 0x80
        H.append(S)
        if not n: break
    return bytes(H)


def _gen_f_vint(f, v):
    return _gen_crvr((f << 3) | 0) + _gen_crvr(v)


def _gen_f_str(f, s):
    enc = s.encode() if isinstance(s, str) else s
    return _gen_crvr((f << 3) | 2) + _gen_crvr(len(enc)) + enc


def _gen_f_bytes(f, b):
    return _gen_crvr((f << 3) | 2) + _gen_crvr(len(b)) + b


def _gen_enaes(hex_data):
    key = bytes([89,103,38,116,99,37,68,69,117,104,54,37,90,99,94,56])
    iv = bytes([54,111,121,90,68,114,50,50,69,51,121,99,104,106,77,37])
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad(bytes.fromhex(hex_data), AES.block_size)).hex()


def _gen_encode_open_id(open_id):
    ks = [0x30,0x30,0x30,0x32,0x30,0x31,0x37,0x30,0x30,0x30,0x30,0x30,0x32,0x30,0x31,0x37,
          0x30,0x30,0x30,0x30,0x30,0x32,0x30,0x31,0x37,0x30,0x30,0x30,0x30,0x30,0x32,0x30]
    return ''.join(chr(ord(c) ^ ks[i % len(ks)]) for i, c in enumerate(open_id)).encode('latin1')


def _gen_majreg(nick, access_tok, open_id, pwd, host, ob):
    Url = f"https://{host}/MajorRegister"
    fields = {
        1:  nick,
        2:  access_tok,
        3:  open_id,
        5:  102000007,
        6:  4,
        7:  1,
        13: 1,
        14: _gen_encode_open_id(open_id),
        15: "hi",
        16: 2,
        20: GEN_GameVer,
        21: 1,
        22: GEN_GgreBlob,
    }
    body = b''
    for k, v in fields.items():
        if isinstance(v, int):
            body += _gen_f_vint(k, v)
        elif isinstance(v, bytes):
            body += _gen_f_bytes(k, v)
        else:
            body += _gen_f_str(k, v)
    Payload = bytes.fromhex(_gen_enaes(body.hex()))
    Hdr = {
        "User-Agent": GEN_UaUnity,
        "Accept": "*/*",
        "Accept-Encoding": "deflate, gzip",
        "Authorization": "Bearer",
        "X-GA": "v1 1",
        "X-GA-SV": str(int(time.time())),
        "ReleaseVersion": GEN_ReleaseVer,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": GEN_uVer,
        "Host": host,
        "Connection": "Keep-Alive",
    }
    return requests.post(Url, headers=Hdr, data=Payload, verify=False, timeout=25)


def _gen_check_active(uid, pwd):
    try:
        r = requests.get(
            GEN_LoginDataApi,
            params={"uid": uid, "pass": pwd},
            timeout=20, verify=False,
        )
        return r.json()
    except Exception:
        return {}


def _gen_save_account(uid, pwd):
    filename = ACCOUNTS_FILE
    accounts = []
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    accounts = data
        except Exception:
            accounts = []
    for acc in accounts:
        if str(acc.get("uid")) == str(uid):
            return
    accounts.append({"uid": str(uid), "password": str(pwd)})
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
    except Exception as e:
        print(f"  Save error: {e}")


def _gen_one_account(ver, ob, host):
    """Sync: garena reg + token grant + MajorRegister. Returns (uid, pwd, nick, reg_ok).
    BR_ACCOUNT_DIRTY_NAME aane pe naam ke alag formats try karta hai."""
    Uid, Pwd, Sig = _gen_reg(ver)
    AccessTok, OpenId = _gen_accs(Uid, Pwd, ver)

    sfx = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    tag = GEN_NAME_PREFIX.strip().rstrip("_")   # "FYT"
    candidates = [
        f"{tag}_{sfx}",      # FYT_1e47   direct ye (server accept karta hai)
        f"{tag}{sfx}",       # FYT1e47   (fallback)
        f"vai_{sfx}",        # fallback (proven)
    ]

    reg_ok = False
    Nick = candidates[-1]
    for cand in candidates:
        Resp = _gen_majreg(cand, AccessTok, OpenId, Pwd, host, ob)
        if Resp.status_code == 200:
            Nick = cand
            reg_ok = True
            break
        try:
            err_txt = Resp.text[:100]
        except Exception:
            err_txt = "<no body>"
        print(f"  MajReg    => {Resp.status_code} | {err_txt} | rejected: '{cand}'")
        # naam ke alawa koi aur error (version/config) -> aage retry bekaar hai
        if "DIRTY_NAME" not in err_txt.upper():
            break
    return Uid, Pwd, Nick, reg_ok


async def auto_generate_accounts(count=4):
    print_info(f"GEN: {count} accounts ({GEN_NAME_PREFIX}xxxx)")
    loop = asyncio.get_running_loop()
    try:
        Ver, Ob, Host = await loop.run_in_executor(None, _gen_veraddr)
    except Exception as e:
        print_error(f"version config fail: {e}")
        return 0
    saved = 0
    for i in range(count):
        try:
            Uid, Pwd, Nick, reg_ok = await loop.run_in_executor(None, _gen_one_account, Ver, Ob, Host)
        except Exception as e:
            print_error(f"[{i+1}/{count}] err: {e}")
            continue
        print_success(f"UID => {Uid}")
        print_success(f"PASS => {Pwd}")
        acc = await process_account_uid_pass(str(Uid), str(Pwd))
        if acc:
            saved += 1
            _gen_save_account(str(Uid), str(Pwd))
        else:
            print_error(f"[{i+1}/{count}] login fail")
    print_success(f"GEN done: {saved}/{count} saved")
    return saved


# =========================== GLORY COUNTER (persisted) ===========================
GLORY_FILE = "glory_count.json"
ACCOUNTS_FILE = "accounts.json"

def _load_glory() -> int:
    try:
        with open(GLORY_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("glory", 0))
    except Exception:
        return 0

def _save_glory(n: int) -> None:
    try:
        with open(GLORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"glory": n}, f)
    except Exception:
        pass

GLORY_COUNT = _load_glory()
_glory_lock = asyncio.Lock()

async def add_glory(n: int = 1) -> int:
    global GLORY_COUNT
    async with _glory_lock:
        GLORY_COUNT += n
        _save_glory(GLORY_COUNT)
        total = GLORY_COUNT
    print_success(f"GLORY +{n} => TOTAL: {total}")
    return total

# ============================== DEVICE ==============================
def get_device_for_account(account_identifier):
    devices = {}
    if os.path.exists(DEVICES_FILE):
        try:
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                devices = json.load(f)
        except Exception: pass
    key = str(account_identifier)
    if key in devices: return devices[key]
    dev_list = [
        ("Samsung", "SM-G998B", "Adreno (TM) 660", "Android OS 12 / API-31"),
        ("Xiaomi", "2201122G", "Adreno (TM) 730", "Android OS 13 / API-33"),
        ("Realme", "RMX3700", "Mali-G710", "Android OS 14 / API-34"),
        ("OnePlus", "CPH2451", "Adreno (TM) 740", "Android OS 13 / API-33"),
        ("OPPO", "CPH2611", "Adreno (TM) 720", "Android OS 14 / API-34"),
    ]
    brand, model, gpu, os_ver = random.choice(dev_list)
    dev = {
        "unique_device_id": f"Google|{uuid.uuid4()}",
        "brand": brand, "model": model, "gpu_renderer": gpu, "system_software": os_ver,
        "screen_width": random.choice([1080, 1440, 720]),
        "screen_height": random.choice([2400, 3200, 1600]),
        "screen_dpi": str(random.randint(300, 420)),
        "memory": random.randint(2800, 6500),
        "processor_details": f"ARM64 FP ASIMD AES | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
        "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}",
    }
    devices[key] = dev
    try:
        with open(DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(devices, f, indent=2)
    except Exception: pass
    return dev

# =========================== DNS ===========================
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_DNS_CACHE_TTL = 300.0

async def resolve_host_cloudflare(hostname):
    if not hostname: return hostname
    parts = hostname.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts): return hostname
    now = time.time()
    if hostname in _DNS_CACHE:
        ip, exp = _DNS_CACHE[hostname]
        if now < exp: return ip
    try:
        loop = asyncio.get_running_loop()
        info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
        if info:
            ip = info[0][4][0]
            _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
            return ip
    except Exception: pass
    return hostname

def optimize_tcp_socket(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    except Exception: pass

def optimize_udp_socket(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
    except Exception: pass

# =========================== HTTP CLIENT ===========================
client = httpx.AsyncClient(verify=False, timeout=10.0,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=50))
headers = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive', 'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'X-Unity-Version': '2018.4.12f1', 'X-GA-SV': '1789535859',
    'X-GA': 'v1 1', 'ReleaseVersion': 'OB55'
}

AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV  = b'6oyZDr22E3ychjM%'

CRC7_TABLE = bytes([
    0,9,18,27,36,45,54,63,72,65,90,83,108,101,126,119,25,16,11,2,61,52,47,38,
    81,88,67,74,117,124,103,110,50,59,32,41,22,31,4,13,122,115,104,97,94,87,76,69,
    43,34,57,48,15,6,29,20,99,106,113,120,71,78,85,92,100,109,118,127,64,73,82,91,
    44,37,62,55,8,1,26,19,125,116,111,102,89,80,75,66,53,60,39,46,17,24,3,10,
    86,95,68,77,114,123,96,105,30,23,12,5,58,51,40,33,79,70,93,84,107,98,121,112,
    7,14,21,28,35,42,49,56,65,72,83,90,101,108,119,126,9,0,27,18,45,36,63,54,
    88,81,74,67,124,117,110,103,16,25,2,11,52,61,38,47,115,122,97,104,87,94,69,76,
    59,50,41,32,31,22,13,4,106,99,120,113,78,71,92,85,34,43,48,57,6,15,20,29,
    37,44,55,62,1,8,19,26,109,100,127,118,73,64,91,82,60,53,46,39,24,17,10,3,
    116,125,102,111,80,89,66,75,23,30,5,12,51,58,33,40,95,86,77,68,123,114,105,96,
    14,7,28,21,42,35,56,49,70,79,84,93,98,107,112,121,
])
_DELTA = 0x9E3779B9
_ROUNDS = 16
_FIELD_SIZES = {0:1, 1:2, 2:2, 3:1, 4:2}
_FIELD_NAMES = {0:"sendOption", 1:"cmd", 2:"orderId", 3:"flags", 4:"length"}

# =========================== LOG ===========================
# every print_* call now also streams to bot_state -> WS -> dashboard log panel
class Colors:
    GREEN=FAIL=WARNING=CYAN=MAGENTA=WHITE=HEADER=ENDC=''

def _pb(tag, t, level="info"):
    try: print(f"\033[1m》》 {tag} {t}\033[0m")
    except Exception: pass
    try: bot_state.log(str(t), level)
    except Exception: pass

def print_colored(t, c=None):
    _pb("", t, "info")

def print_success(t):
    _pb("[+]", t, "success")

def print_error(t):
    _pb("[-]", t, "error")

def print_warning(t):
    _pb("[!]", t, "warning")

def print_info(t):
    _pb("[i]", t, "info")

def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict): return default
    if key in d:
        v = d[key].get('data'); return v if v is not None else default
    if str(key) in d:
        v = d[str(key)].get('data'); return v if v is not None else default
    return default

# =========================== MATCH COUNTER ===========================
_match_counters: Dict[str, int] = {}
_match_counter_lock = asyncio.Lock()

async def _inc_match(uid):
    async with _match_counter_lock:
        _match_counters[uid] = _match_counters.get(uid, 0) + 1
        return _match_counters[uid]

async def _dec_match(uid):
    async with _match_counter_lock:
        if uid in _match_counters and _match_counters[uid] > 0:
            _match_counters[uid] -= 1
        return _match_counters.get(uid, 0)

async def _get_match_count(uid):
    async with _match_counter_lock:
        return _match_counters.get(uid, 0)

async def _get_total_match_count():
    async with _match_counter_lock:
        return sum(_match_counters.values())

# =========================== SQUAD STATE ===========================
# Registry keyed by team_key (= client_id) so N clients can run concurrent
# squads without one client's peer count / leader election / match-code
# bleeding into another's. Original code had exactly one global dict here
# because the terminal version only ever ran one squad per process.
_team_state_registry: Dict[str, Dict[str, Any]] = {}

def _get_team(team_key: str) -> Dict[str, Any]:
    if team_key not in _team_state_registry:
        _team_state_registry[team_key] = {
            "peers": {},
            "leader_uid": None,
            "team_code": None,
            "pk_code": None,
            "members": set(),
            "members_lock": asyncio.Lock(),
            "lock": asyncio.Lock(),
        }
    return _team_state_registry[team_key]

def _drop_team(team_key: str):
    _team_state_registry.pop(team_key, None)

async def _team_member_join(uid, team_key: str):
    uid = str(uid)
    ts = _get_team(team_key)
    async with ts["members_lock"]:
        before = len(ts["members"])
        ts["members"].add(uid)
        after = len(ts["members"])
    if after != before:
        print_success(f"[TEAM]  In team ({after}/{TEAM_SIZE}) | joined: {uid}")
        try:
            for p_uid in list(ts["peers"].keys()):
                role = "LEADER" if ts["leader_uid"] == p_uid else "MEMBER"
                joined = p_uid in ts["members"]
                tag = f"IN_TEAM ({after}/{TEAM_SIZE}) [{role}]" if joined else f"JOINING ({after}/{TEAM_SIZE}) [{role}]"
                bot_state.update_status(p_uid, tag, 0)
        except Exception: pass
    return after

async def _team_member_leave(uid, team_key: str):
    ts = _get_team(team_key)
    async with ts["members_lock"]:
        ts["members"].discard(str(uid))

async def _team_member_count(team_key: str):
    ts = _get_team(team_key)
    async with ts["members_lock"]:
        return len(ts["members"])

async def _team_member_reset(team_key: str, keep_leader=True):
    ts = _get_team(team_key)
    async with ts["members_lock"]:
        ts["members"].clear()
        if keep_leader and ts["leader_uid"]:
            ts["members"].add(ts["leader_uid"])

# =========================== TOKEN CACHE ===========================
_token_cache_memo = {}
_token_cache_memo_time = 0.0
_TOKEN_CACHE_MEMO_TTL = 5.0

def _json_serializer(obj):
    if isinstance(obj, (bytes, bytearray)): return {"__bytes_hex__": bytes(obj).hex()}
    raise TypeError(f"not serializable: {type(obj)}")

def _json_deserializer(obj):
    if isinstance(obj, dict):
        if "__bytes_hex__" in obj and len(obj) == 1:
            try: return bytes.fromhex(obj["__bytes_hex__"])
            except Exception: return b""
        return {k: _json_deserializer(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_json_deserializer(x) for x in obj]
    return obj

def _load_token_cache():
    global _token_cache_memo, _token_cache_memo_time
    now = time.time()
    if _token_cache_memo and (now - _token_cache_memo_time) < _TOKEN_CACHE_MEMO_TTL:
        return _token_cache_memo
    if not os.path.exists(TOKEN_CACHE_FILE): return {}
    try:
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content: return {}
        data = json.loads(content)
        if not isinstance(data, dict): raise ValueError("root must be dict")
        parsed = _json_deserializer(data)
        _token_cache_memo = parsed; _token_cache_memo_time = now
        return parsed
    except Exception as e:
        print_error(f"Token cache corrupt -> delete: {e}")
        try: os.remove(TOKEN_CACHE_FILE)
        except Exception: pass
        return {}

def _save_token_cache(cache):
    global _token_cache_memo, _token_cache_memo_time
    try:
        tmp = TOKEN_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, default=_json_serializer)
        os.replace(tmp, TOKEN_CACHE_FILE)
        _token_cache_memo = cache; _token_cache_memo_time = time.time()
    except Exception as e: print_error(f"Cache save: {e}")

def cache_get(uid):
    cache = _load_token_cache()
    e = cache.get(str(uid))
    if not e: return None
    if time.time() - e.get("cached_at", 0) > TOKEN_CACHE_TTL:
        cache_invalidate(uid); return None
    if str(e.get("account_id", "")).isdigit(): e["account_id"] = int(e["account_id"])
    if not isinstance(e.get("login_payload_data"), (bytes, bytearray)):
        cache_invalidate(uid); return None
    return e

def cache_set(uid, data):
    cache = _load_token_cache()
    e = dict(data); e["cached_at"] = time.time()
    cache[str(uid)] = e; _save_token_cache(cache)
    print_success(f"[CACHE] Saved UID {uid}")

def cache_invalidate(uid):
    cache = _load_token_cache()
    if str(uid) in cache:
        del cache[str(uid)]; _save_token_cache(cache)

def _update_cache_profile(data, level, exp):
    try:
        keys = []
        if data.get('auth_uid'): keys.append(str(data['auth_uid']))
        if data.get('auth_token'): keys.append(f"tok_{data['auth_token'][:20]}")
        if not keys: return
        cache = _load_token_cache(); ch = False
        for k in keys:
            if k in cache and isinstance(cache[k], dict):
                cache[k]['level'] = level; cache[k]['exp'] = exp; ch = True
        if ch: _save_token_cache(cache)
    except Exception: pass

# =========================== ENCRYPTION ===========================
async def aes_encrypt(payload, key, iv):
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(payload, AES.block_size))

async def get_playstore_version():
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id'))
    return res.get("version")

async def version_config():
    app_version = await get_playstore_version()
    url = ("https://version.ggwhitehawk.com/live/ver.php"
           f"?version={app_version}&lang=hi&device=android&channel=android"
           "&appstore=googleplay&region=ID&whitelist_version=1.3.0&whitelist_sp_version=1.0.0")
    try:
        r = await client.get(url); r.raise_for_status()
        d = r.json()
        su = d.get("server_url"); rv = d.get("remote_version"); lr = d.get("latest_release_version")
        if not su or not rv or not lr: return None
        return lr, rv, su
    except Exception: return None

async def get_access_token(uid, password):
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {
        "Host": "100067.connect.garena.com",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip, deflate, br", "Connection": "close"
    }
    data = {
        "uid": uid, "password": password, "response_type": "token", "client_type": "2",
        "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
        "client_id": "100067"
    }
    for attempt in range(5):
        try:
            r = await client.post(url, headers=hdrs, data=data)
            if r.status_code == 200:
                d = r.json()
                oid = d.get("open_id"); at = d.get("access_token"); pf = d.get("platform", 4)
                if oid and at: return oid, at, pf
            if r.status_code == 429: await asyncio.sleep(1); continue
        except Exception: pass
        await asyncio.sleep(0.5)
    return None

async def parse_results(parsed):
    out = {}
    for r in parsed:
        fd = {"wire_type": r.wire_type}
        if r.wire_type in ("varint", "string", "bytes"): fd["data"] = r.data
        elif r.wire_type == "length_delimited":
            fd["data"] = await parse_results(r.data.results)
        out[r.field] = fd
    return out

async def decode_protobuf(data):
    parsed = Parser().parse(data)
    return json.dumps(await parse_results(parsed))

def decode_protobuf_dict_safe(data):
    try:
        parsed = Parser().parse(data)
        return _recursive_dict(parsed)
    except Exception: return {}

def _recursive_dict(results):
    out = {}
    for r in results:
        fd = {"wire_type": r.wire_type}
        if r.wire_type in ("varint", "string", "bytes"): fd["data"] = r.data
        elif r.wire_type == "length_delimited":
            if hasattr(r.data, "results"): fd["data"] = _recursive_dict(r.data.results)
            elif isinstance(r.data, list): fd["data"] = _recursive_dict(r.data)
            else: fd["data"] = str(r.data)
        out[str(r.field)] = fd
    return out

def _decode_proto_manual(data):
    """Manual protobuf decode (fallback)."""
    result = {}; offset = 0; data_len = len(data)
    while offset < data_len:
        header = 0; shift = 0
        while offset < data_len:
            b = data[offset]; offset += 1
            header |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
            if shift > 28: break
        if header == 0: break
        fn = header >> 3; wt = header & 0x7
        if fn == 0: break
        if wt == 0:
            v = 0; shift = 0
            while offset < data_len:
                b = data[offset]; offset += 1
                v |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
                if shift > 63: break
            result[fn] = v
        elif wt == 2:
            length = 0; shift = 0
            while offset < data_len:
                b = data[offset]; offset += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
                if shift > 28: break
            if length > data_len - offset: break
            value = data[offset:offset+length]; offset += length
            try:
                s = value.decode('utf-8')
                if s and all(31 < ord(c) < 127 or ord(c) in (9,10,13) for c in s):
                    result[fn] = s
                else:
                    nested = _decode_proto_manual(value)
                    result[fn] = nested if nested else value.hex()
            except UnicodeDecodeError:
                nested = _decode_proto_manual(value)
                result[fn] = nested if nested else value.hex()
        elif wt == 5: offset += 4
        elif wt == 1: offset += 8
        else: break
    return result

# =========================== MANUAL PROTOBUF ENCODE ===========================
def encode_varint(n):
    if n < 0: return b''
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: b |= 0x80
        out.append(b)
        if not n: break
    return bytes(out)

def build_proto(fields):
    parts = []
    for k, v in fields.items():
        if isinstance(v, dict):
            nested = build_proto(v)
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(nested)) + nested)
        elif isinstance(v, int):
            parts.append(encode_varint((k << 3) | 0) + encode_varint(v))
        elif isinstance(v, (str, bytes)):
            ev = v.encode() if isinstance(v, str) else v
            parts.append(encode_varint((k << 3) | 2) + encode_varint(len(ev)) + ev)
    return b''.join(parts)

def encrypt_api(plain_hex):
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(
        pad(bytes.fromhex(plain_hex), AES.block_size)).hex()

def encrypt_api_bytes(plain_bytes):
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(plain_bytes, AES.block_size))

# =========================== SQUAD PROTOBUF BUILDERS ===========================
def _pb_varint(fn, val): return encode_varint((fn << 3) | 0) + encode_varint(val)
def _pb_length(fn, val):
    enc = val.encode('utf-8') if isinstance(val, str) else val
    return encode_varint((fn << 3) | 2) + encode_varint(len(enc)) + enc

def create_proto(fields):
    packet = bytearray()
    for field, value in fields.items():
        if isinstance(field, str): field = int(field)
        if isinstance(value, dict):
            packet.extend(_pb_length(field, bytes(create_proto(value))))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    packet.extend(_pb_length(field, bytes(create_proto(item))))
                elif isinstance(item, str): packet.extend(_pb_length(field, item))
                elif isinstance(item, (bytes, bytearray)): packet.extend(_pb_length(field, bytes(item)))
                elif isinstance(item, bool): packet.extend(_pb_varint(field, 1 if item else 0))
                elif isinstance(item, int): packet.extend(_pb_varint(field, item))
        elif isinstance(value, bool): packet.extend(_pb_varint(field, 1 if value else 0))
        elif isinstance(value, int): packet.extend(_pb_varint(field, value))
        elif isinstance(value, str): packet.extend(_pb_length(field, value))
        elif isinstance(value, (bytes, bytearray)): packet.extend(_pb_length(field, bytes(value)))
    return bytes(packet)

def create_packet(Pk, N, K, V):
    if isinstance(K, str): K = bytes.fromhex(K) if len(K) == 32 else K.encode()
    if isinstance(V, str): V = bytes.fromhex(V) if len(V) == 32 else V.encode()
    PkEnc = AES.new(K, AES.MODE_CBC, V).encrypt(pad(bytes.fromhex(Pk), 16)).hex()
    L = len(PkEnc) // 2
    hexlen = hex(L)[2:]
    if len(hexlen) % 2 != 0: hexlen = '0' + hexlen
    pad_len = 12 - len(N) - len(hexlen)
    if pad_len < 0: pad_len = 0
    return bytes.fromhex(N + ('0' * pad_len) + hexlen + PkEnc)

# =========== SQUAD PACKETS ===========
def _sq_prefix(base, region="ID"):
    reg = str(region).upper() if region else "ID"
    if reg == "IND":
        return base + "14"
    return base + "15"   # ID/default - capture-verified

def create_team(key, iv, game_version="1.132.9"):
    fields = {
        1: 1, 2: {2: bytes.fromhex("01"), 3: 1, 4: 3, 5: "en",
            8: {1: "IDC3", 2: 123, 3: "ID"}, 9: 1,
            10: bytes.fromhex("01090a0b1219202729"), 11: 2, 13: 1,
            14: {1: "08FBA33B4BABE2790205390333330000009700050092005F72697EB2134DCBB2467625142201040e38e848dc0e748c3f6aa04d81000000ff1113090dcacfa16d",
                 2: 478, 3: "7e585d55", 4: "y[Z]", 6: 12, 7: "15686677735a7506101c",
                 8: str(game_version), 9: 3, 10: 1, 11: "03626253513637754174"},
            19: 329, 21: bytes.fromhex("374f521955"), 24: {1: 21},
            27: "a_6534489873065906362"}}
    return create_packet(create_proto(fields).hex(), "0515", key, iv)

def team_fresh(account_id, key, iv):
    return create_packet(create_proto({1: 72, 2: {1: int(account_id)}}).hex(), "0515", key, iv)

def invite_player_in_team(target_uid, key, iv, region="ID"):
    return create_packet(create_proto({1: 2, 2: {1: int(target_uid), 2: region, 4: 1}}).hex(),
                         _sq_prefix("05", region), key, iv)

def accept_team_invite(target_uid, group_id, key, iv, region="ID"):
    fields = {1: 4, 2: {1: int(target_uid), 3: int(target_uid),
        4: bytes.fromhex("0107090a0b120f19202729"), 8: 1,
        9: {1: "08FBA33B0522052002041B0222220007000C00010004000547E265AE1080779546762514110104076fa2e8770e748c3f6a68ed75000000ff08080500cacfa16d",
            2: 235, 3: {14: 80, 11: 86}, 4: "{Y\\R", 6: 13, 7: {2: 4},
            8: "1.132.9", 9: 2, 10: 1, 11: "0362625351"},
        10: str(group_id),
        11: {"1": "IDC3", "2": 107, "3": "ID"}, 13: "en", 16: "374f5219",
        20: {1: 21}, 23: "a_6534489873065906362",
        24: "https://dl-sg-production.freefiremobile.com/",
        27: {1: 2, 2: 4}}}
    return create_packet(create_proto(fields).hex(), _sq_prefix("05", region), key, iv)

def start_match_team(account_id, key, iv, region="ID"):
    fields = {1: 9, 2: {1: int(account_id), 7: {1: "IDC3", 2: 410, 3: region}}}
    return create_packet(create_proto(fields).hex(), _sq_prefix("05", region), key, iv)

def ready_for_game(uid, key, iv):
    return create_packet(create_proto({1: 15, 2: {1: uid, 2: 1}}).hex(), "0519", key, iv)

# =========================== MAJORLOGIN / GETLOGIN ===========================
async def build_majorlogin_payload(open_id, access_token, platform, client_version, dev):
    try:
        p = {
            3: str(datetime.now())[:-7], 4: "free fire", 5: "1",
            7: str(client_version) if client_version else "1.132.8",
            8: str(dev.get("system_software", "Android OS 14 / API-34")),
            9: "Handheld", 10: "Telkomsel", 11: "CarrierDataNetwork",
            12: str(dev.get("screen_width", 1639)),
            13: str(dev.get("screen_height", 720)),
            14: str(dev.get("screen_dpi", 320)),
            15: str(dev.get("processor_details", "ARM64 FP ASIMD AES | 2000 | 8")),
            16: str(dev.get("memory", 5653)),
            17: str(dev.get("gpu_renderer", "Mali-G52 MC2")),
            18: "OpenGL ES 3.2 v1.r49p1-03bet0",
            19: str(dev.get("unique_device_id", f"Google|{uuid.uuid4()}")),
            20: str(dev.get("client_ip", "182.5.135.225")),
            21: "en", 22: str(open_id), 23: str(platform), 24: "Handheld",
            25: str(dev.get("model", "Xiaomi")), 26: "ID",
            29: str(access_token), 30: "1", 41: "Telkomsel", 42: "4G",
            57: "7428b253defc164018c604a1ebbfebdf", 60: "109278", 61: "55402",
            62: "868", 64: "55927", 65: "109278", 66: "55927", 67: "109278",
            73: 2,
            74: "/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/lib/arm64",
            76: 1,
            77: "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~6Cw028g_ld-8Z0z2qhrOLA==/com.dts.freefireth-i3wiaEmpx8CbiRVhUMx_ag==/base.apk",
            78: 6, 79: 1, 81: "32", 83: "2019121229", 85: 3,
            86: "OpenGLES2", 87: 3071, 88: 4, 89: 14466, 93: "3rd_party",
            94: "KqsHTyb7yHjsAiyNWUUZa0DSOD6U0AhDQ76smAo1WeHHHpSmJ8KMRyRrxb2ttgJqO9YOqO/K6AORAvDMAX/qBS8+JahIkrM7kHfzlfULbgRAoZ9r",
            95: "111207", 96: "{\"cur_rate\":[60,90,120],\"support_etc2\":true}", 97: "1",
            98: "1", 99: str(platform), 100: str(platform),
            103: "1", 104: "52837", 105: "1",
            106: "https://dl.gmc.freefiremobile.com/live/ABHotUpdates/|211c933168f55902c7dfbfd8c4e2957d",
            107: "1.3fb8adbf45f44ce9",
        }
        return encrypt_api_bytes(build_proto(p))
    except Exception as e:
        print_error(f"build_majorlogin_payload: {e}"); return None

async def send_majorlogin(data, release_version, server_url):
    try:
        url = f"{server_url}MajorLogin" if server_url.endswith('/') else f"{server_url}/MajorLogin"
        hdrs = headers.copy(); hdrs["ReleaseVersion"] = str(release_version)
        hdrs["X-GA-SV"] = str(int(time.time()))
        r = await client.post(url, headers=hdrs, data=data)
        if r.status_code != 200:
            print_error(f"[MAJORLOGIN] HTTP {r.status_code}"); return None
        c = r.content
        if len(c) < 40: return None
        for off in [0, 64] + list(range(128)):
            try:
                m = thunderFF_pb2.MajorLoginRes(); m.ParseFromString(c[off:])
                if m.region and m.token: return m
            except Exception: continue
        return None
    except Exception as e:
        print_error(f"[MAJORLOGIN] {e}"); return None

async def send_getlogin(data, base_url, token, release_version):
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        hdrs = headers.copy()
        hdrs["ReleaseVersion"] = release_version
        hdrs["X-GA-SV"] = str(int(time.time()))
        hdrs['Authorization'] = f"Bearer {token}"
        try:
            from urllib.parse import urlparse as _urlp
            hdrs['Host'] = (_urlp(base_url).hostname or "clientbp.ppmainecoonghj.com")
        except Exception:
            hdrs['Host'] = "clientbp.ppmainecoonghj.com"
        r = await client.post(url, headers=hdrs, data=data)
        if r.status_code != 200: return None
        c = r.content
        rp = thunderFF_pb2.GetLoginDataRes()
        ok = False
        try:
            rp.ParseFromString(c)
            if rp.functional_addrs or rp.informational_addrs: ok = True
        except Exception: pass
        if not ok:
            for off in range(min(128, len(c))):
                try:
                    cand = thunderFF_pb2.GetLoginDataRes(); cand.ParseFromString(c[off:])
                    if cand.functional_addrs or cand.informational_addrs:
                        rp = cand; break
                except Exception: continue
        dd = {}
        try:
            parsed = Parser().parse(c.hex())
            dd = await parse_results(parsed)
        except Exception: pass
        return rp, dd
    except Exception: return None

# =========================== HTTP PING ===========================
async def send_ping(account_id, token, release_version, ping_url=None):
    try:
        try: acc = int(account_id)
        except Exception: acc = 0
        now_ms = int(time.time() * 1000)
        payload = struct.pack("<Q", acc & 0xFFFFFFFFFFFFFFFF) + struct.pack("<Q", now_ms & 0xFFFFFFFFFFFFFFFF)
        req_id = secrets.token_urlsafe(18)[:22]
        hdrs = {
            'accept': '*/*', 'accept-encoding': 'deflate, gzip',
            'authorization': f'Bearer {token}',
            'content-type': 'application/x-www-form-urlencoded',
            'releaseversion': release_version,
            'user-agent': 'UnityPlayer/2018.4.12f1',
            'x-ga': 'v1 1', 'x-ga-sv': str(int(time.time())),
            'x-railway-edge': 'sin1', 'x-railway-request-id': req_id,
            'x-real-ip': PING_REAL_IP, 'x-request-start': str(now_ms),
            'x-unity-version': '2018.4.12f1',
        }
        r = await client.post(str(ping_url or PING_URL), headers=hdrs, content=payload, timeout=PING_TIMEOUT)
        return r.status_code == 200
    except Exception: return False

async def ping_worker(account_id, token, release_version, region="ID", ping_url=None):
    if not ENABLE_HTTP_PING: return
    _purl = str(ping_url or PING_URL)
    uid_str = str(account_id); ok_count = 0; fails = 0
    try:
        await asyncio.sleep(5.0 + random.uniform(0, 2.0))
        while True:
            ok = await send_ping(uid_str, token, release_version, _purl)
            if ok:
                ok_count += 1; fails = 0
                if ok_count % 10 == 0: print_info(f"[PING] {uid_str} OK total={ok_count}")
            else:
                fails += 1
                if fails <= 3: print_warning(f"[PING] {uid_str} fail #{fails}")
            await asyncio.sleep(max(5.0, PING_INTERVAL + random.uniform(-3.0, 3.0)))
    except asyncio.CancelledError: pass
    finally: print_info(f"[PING] Stopped {uid_str} OK={ok_count}")

# =========================== TCP STARTUP ===========================
async def build_tcp_startup_packet(account_id, token, server_time, key, iv, region="ID", typ='OnLine'):
    uid_hex = f"{int(account_id):016x}"
    ts_hex = f"{int(server_time):08x}"
    enc = (await aes_encrypt(token.encode(), key, iv)).hex()
    enc_len = f"{len(enc)//2:08x}"
    reg = str(region).upper() if region else "ID"
    if typ == 'OnLine':
        pfx = '7119' if reg == 'ID' else ('7114' if reg == 'IND' else '7115')
        return f"{pfx}{uid_hex}{ts_hex}00000000{enc_len}{enc}"
    pfx = '9219' if reg == 'ID' else ('9214' if reg == 'IND' else '9215')
    return f"{pfx}{uid_hex}{ts_hex}{enc_len}{enc}"

async def send_keep_alive(region="ID"):
    try:
        reg = str(region).upper() if region else "ID"
        return bytes.fromhex("0219" if reg == "ID" else ("0214" if reg == "IND" else "0215"))
    except Exception:
        return bytes.fromhex("0219")

# =========================== MATCH SEARCH PACKETS ===========================
BR_HEX = (
    "080112800a0a010110013a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f5045"
    "40014a0801090a0b1219202758016291090a80013038384638324246303241393637363730323031303130303030"
    "30303030303030303031363030303130303135303030323232463937454545304630303030303034363736323531"
    "34303030303030303030303030303030303030303030303030303030303030303030303030306666303030303030"
    "3030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e"
    "1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e7519600147"
    "7c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d"
    "675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b62"
    "5f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d61"
    "5f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e50"
    "78487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b44"
    "1b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57"
    "047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516"
    "134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f47777648"
    "4f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b324752526837"
    "7a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c"
    "34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c"
    "30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d"
    "337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a56507256"
    "4b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c"
    "314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c443474447870677138553359"
    "5973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e42"
    "49725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78"
    "704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f"
    "583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551"
    "303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d7475"
    "7077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a2"
    "01050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815"
)

async def start_game_battle_royale(region, client_version, writer, key, iv):
    packet = bytes.fromhex(BR_HEX)
    try:
        proto = FYTFF_pb2.StartMatch(); proto.ParseFromString(packet)
        if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
            proto.main.region_list[0].region = region
            if len(proto.main.region_list) > 1: proto.main.region_list[1].region = region
        if hasattr(proto.main, 'client_version'):
            proto.main.client_version.remote_version = client_version
        packet = proto.SerializeToString()
    except Exception: pass
    enc = (await aes_encrypt(packet, key, iv)).hex()
    plen = len(enc) // 2
    hl = hex(plen)[2:]
    hl = hl if len(hl) > 1 else "0" + hl
    reg = str(region).upper() if region else "BD"
    pfx = "031500" if reg == "ID" else ("031400" if reg == "IND" else "031500")
    final = pfx + "0" * (6 - len(hl)) + hl + enc
    writer.write(bytes.fromhex(final)); await writer.drain()
    print_info(f"[BR] search sent ({plen}B) R={reg}")

# =========================== UDP HELPERS ===========================
async def has_ssan_zig(n):
    z = (n << 1) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while z >= 0x80: out.append((z & 0x7F) | 0x80); z >>= 7
    out.append(z); return bytes(out)

async def uleb_encode(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: b |= 0x80
        out.append(b)
        if not n: break
    return bytes(out)

async def zigzag_encode_signed(n):
    try: n = int(n)
    except Exception: n = 0
    zz = (n << 1) ^ (n >> 63)
    if zz < 0: zz = -zz
    out = bytearray()
    while True:
        b = zz & 0x7F; zz >>= 7
        if zz: b |= 0x80
        out.append(b)
        if not zz: break
    return bytes(out)

async def tea_enc(v0, v1, k0, k1, k2, k3):
    s = 0
    for _ in range(_ROUNDS):
        s = (s + _DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((((v1<<4)&0xFFFFFFFF)+k0)&0xFFFFFFFF ^ ((v1+s)&0xFFFFFFFF) ^ (((v1>>5)+k1)&0xFFFFFFFF)))) & 0xFFFFFFFF
        v1 = (v1 + (((((v0<<4)&0xFFFFFFFF)+k2)&0xFFFFFFFF ^ ((v0+s)&0xFFFFFFFF) ^ (((v0>>5)+k3)&0xFFFFFFFF)))) & 0xFFFFFFFF
    return v0, v1

async def tea_dec(v0, v1, k0, k1, k2, k3):
    s = (_DELTA * _ROUNDS) & 0xFFFFFFFF
    for _ in range(_ROUNDS):
        v1 = (v1 - (((((v0<<4)&0xFFFFFFFF)+k2)&0xFFFFFFFF ^ ((v0+s)&0xFFFFFFFF) ^ (((v0>>5)+k3)&0xFFFFFFFF)))) & 0xFFFFFFFF
        v0 = (v0 - (((((v1<<4)&0xFFFFFFFF)+k0)&0xFFFFFFFF ^ ((v1+s)&0xFFFFFFFF) ^ (((v1>>5)+k1)&0xFFFFFFFF)))) & 0xFFFFFFFF
        s = (s - _DELTA) & 0xFFFFFFFF
    return v0, v1

async def tea_cbc_encrypt(padded, key_bytes):
    k0,k1,k2,k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0,4,8,12))
    out = bytearray(len(padded)); pc = bytearray(8); pi = bytearray(8)
    for i in range(0, len(padded), 8):
        x = bytearray(8)
        for j in range(8): x[j] = padded[i+j] ^ pc[j]
        e0,e1 = await tea_enc(struct.unpack_from("<I", x, 0)[0],
                              struct.unpack_from("<I", x, 4)[0], k0,k1,k2,k3)
        enc = bytearray(8)
        struct.pack_into("<I", enc, 0, e0); struct.pack_into("<I", enc, 4, e1)
        for j in range(8): out[i+j] = enc[j] ^ pi[j]
        pc[:] = out[i:i+8]; pi[:] = x
    return bytes(out)

async def tea_cbc_decrypt(body, key_bytes):
    k0,k1,k2,k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0,4,8,12))
    out = bytearray(len(body)); pi = bytearray(8); pc = bytearray(8)
    x = bytearray(8); dec = bytearray(8)
    for i in range(0, len(body), 8):
        for j in range(8): x[j] = body[i+j] ^ pi[j]
        d0,d1 = await tea_dec(struct.unpack_from("<I", x, 0)[0],
                              struct.unpack_from("<I", x, 4)[0], k0,k1,k2,k3)
        struct.pack_into("<I", dec, 0, d0); struct.pack_into("<I", dec, 4, d1)
        for j in range(8): out[i+j] = dec[j] ^ pc[j]
        pc[:] = body[i:i+8]; pi[:] = dec
    return bytes(out)

async def build_padded(content):
    pad_len = (8 - (len(content) + 10) % 8) % 8
    return bytes([pad_len, 0, 0]) + b"\x00" * pad_len + content + b"\x00" * 7

async def encode_header(layout, so, cmd, oid, flags, length, k, v80):
    out = bytearray()
    for code in layout:
        val = {0:so, 1:cmd, 2:oid, 3:flags, 4:length}[code]
        if _FIELD_SIZES[code] == 1: out.append((val & 0xFF) ^ k)
        else:
            v = ((val & 0xFFFF) ^ v80) & 0xFFFF
            out.append(v & 0xFF); out.append((v >> 8) & 0xFF)
    return bytes(out)

async def crc7_buff(crc, buf):
    c = crc & 0x7F
    for b in buf: c = CRC7_TABLE[((2*(c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F

async def sv_frame(msg_key, layout, so, cmd, oid, flags, content, key, encrypted=True):
    k = key[0]; v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0]) + await encode_header(layout, so, cmd, oid, flags, len(body), k, v80)
    pkt = bytearray(hdr + body)
    pkt[1] = await crc7_buff(0, bytes(pkt[2:])) & 0x7F
    return bytes(pkt)

async def build_packet(msg_key, layout, so, cmd, oid, flags, content, key, encrypted=True):
    k = key[0]; v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0])
    for code in layout:
        val = {0:so, 1:cmd, 2:oid, 3:flags, 4:len(body)}[code]
        if _FIELD_SIZES[code] == 1: hdr.append((val & 0xFF) ^ k)
        else:
            v = ((val & 0xFFFF) ^ v80) & 0xFFFF
            hdr.append(v & 0xFF); hdr.append((v >> 8) & 0xFF)
    pkt = bytearray(hdr + body)
    pkt[1] = await crc7_buff(0, bytes(pkt[2:])) & 0x7F
    return bytes(pkt)

async def layouts_from_mask(mask):
    ru = [int(c) for c in str(mask).strip()]
    nr = [c for c in ru if c != 2]
    return ru, nr

async def build_hello_packet(text, key, layout):
    data = text.encode("utf-8")
    if len(data) > 25: raise ValueError("too long")
    content = b"\x10\x00\x00\x00" + data + b"\x00" * (29 - 4 - len(data))
    k = key[0] if key else 10
    v80 = ((k << 8) | k) & 0xFFFF
    layout = [int(c) for c in str(layout).strip()] if isinstance(layout, str) else list(layout)
    padded = await build_padded(content)
    enc_body = await tea_cbc_encrypt(padded, key)
    hdr = await encode_header(layout, 1, 1, 0, 1, len(enc_body), k, v80)
    pkt = bytearray([0x63, 0x00]) + hdr + enc_body
    pkt[1] = await crc7_buff(0, pkt[2:]) & 0x7F
    return bytes(pkt).hex()

async def classify(frame):
    cmd = frame["cmd"]
    name = MESSAGE_ID_TO_NAME.get(cmd, f"UNKNOWN_{cmd}")
    if name == "UDP_HELLO": return "HELLO"
    if name == "UDP_ACK": return "ACK"
    if name == "UDP_PING": return "PING"
    if name == "RUDP_JOIN_MATCH": return "JOIN_MATCH"
    if name.startswith("RUDP_"): return name
    if name.startswith("UDP_"): return name
    return "DATA"

async def reply_for(frame, key, mask, ack_key=0x68, ping_key=0x6D, hello_key=0x5B, ack_style="short"):
    _, nr = await layouts_from_mask(mask)
    typ = await classify(frame)
    if typ == "HELLO":
        if ack_style == "echo":
            content = frame["content"] if frame["content"] else b"\x10\x00\x00\x00"
            return typ, await build_packet(hello_key, nr, 1, 1, None, 1, content, key)
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x01\x00", key)
    if typ == "ACK":
        content = frame["content"] if frame["content"] else b"\x01\x00"
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, content, key)
    if typ == "PING":
        c = frame["content"]
        counter = c[:4] if len(c) >= 4 else c
        return typ, await build_packet(ping_key, nr, 0, 3, None, 0, counter + b"\x00\x00\x00", key, encrypted=False)
    if typ == "JOIN_MATCH":
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x02\x00", key)
    return typ, None

async def keepalive_ping(sock, ip, port, key_bytes, mask, stop_event):
    nr = (await layouts_from_mask(mask))[1]
    ping_keys = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
    loop = asyncio.get_event_loop(); i = 0
    while not stop_event.is_set():
        pk = ping_keys[i % len(ping_keys)]
        counter = int(time.time() * 1000) & 0xFFFFFFFF
        pkt = await build_packet(pk, nr, 0, 3, None, 0, struct.pack("<I", counter) + b"\x00\x00\x00",
                                 key_bytes, encrypted=False)
        try: await loop.sock_sendto(sock, pkt, (ip, port))
        except Exception: pass
        i += 1
        try: await asyncio.wait_for(stop_event.wait(), timeout=bypass_packet_jitter(3.0, 0.4))
        except asyncio.TimeoutError: pass

async def try_header(buf, layout, k, v80):
    off = 2; out = {}
    for code in layout:
        size = _FIELD_SIZES[code]
        if off + size > len(buf): return None
        out[_FIELD_NAMES[code]] = ((buf[off] ^ k) if size == 1
                                    else ((buf[off] | (buf[off+1] << 8)) ^ v80) & 0xFFFF)
        off += size
    out["headerLen"] = off
    return out

async def oicq_unpad(padded):
    if not padded or len(padded) < 8: return None
    if not all(padded[-1-i] == 0 for i in range(7)): return None
    pad_len = padded[0] & 0x07
    s = 3 + pad_len; e = len(padded) - 7
    return padded[s:e] if s < e else b""

async def decode_packet(packet, key, mask=None):
    data = bytes(packet) if isinstance(packet, bytes) else bytes.fromhex(packet)
    if len(data) < 8: return None
    k = key[0]; v80 = ((k << 8) | k) & 0xFFFF
    crc_ok = (data[1] & 0x7F) == await crc7_buff(0, data[2:])
    candidates = []
    if mask:
        ru, nr = await layouts_from_mask(mask)
        layouts = [("RUDP", ru), ("nonRUDP", nr)]
    else:
        layouts = [("RUDP", list(p)) for p in itertools.permutations([0,1,2,3,4])]
        layouts += [("nonRUDP", list(p)) for p in itertools.permutations([0,1,3,4])]
    for kind, layout in layouts:
        f = await try_header(data, layout, k, v80)
        if not f: continue
        if f["flags"] > 7 or f["sendOption"] > 7: continue
        if f["length"] != len(data) - f["headerLen"]: continue
        body = data[f["headerLen"]:f["headerLen"]+f["length"]]
        content = None
        if f["flags"] & 1:
            if len(body) < 8 or len(body) % 8 != 0: continue
            padded = await tea_cbc_decrypt(body, key)
            content = await oicq_unpad(padded)
            if content is None: continue
        else: content = body
        score = (1 if crc_ok else 0) + (1 if content is not None else 0)
        candidates.append({"kind":kind, "layout":layout, "headerLen":f["headerLen"],
            "msgKey":data[0], "cmd":f["cmd"], "flags":f["flags"],
            "sendOption":f["sendOption"], "orderId":f.get("orderId"),
            "length":f["length"], "content":content, "crcOk":crc_ok,
            "score":score, "total":len(data)})
    if not candidates: return None
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[0]

async def build_match_startup_packets(token, udp_key, match_code, account_id, block_val,
                                      server_ip="", region="ID", client_version="1.132.6",
                                      client_version_code="2019121229", access_token="",
                                      mode_id=BR_MODE_ID, map_id=BR_MAP_ID):
    token = token.strip()
    udp_key = bytes.fromhex(udp_key)
    match_code = [int(ch) for ch in str(match_code).strip()]
    thunder_jwt = token[:660] if len(token) > 660 else token
    sharma_jwt = token[660:] if len(token) > 660 else ""
    eth = thunder_jwt.encode() if isinstance(thunder_jwt, str) else thunder_jwt
    esh = sharma_jwt.encode() if isinstance(sharma_jwt, str) else sharma_jwt
    garena420 = await has_ssan_zig(len(eth)) + eth
    reg = str(region).upper() if region else "ID"
    csoversea_block = bytes.fromhex(
        "ca0163736f7665727365612e7374726f6e67686f6c642e66726565666972656d6f62696c652e636f6d"
        "3b302e302e302e303b33342e3132362e37362e34353b33342e38372e3137372e31343b33342e38372e"
        "3137302e3233303b33352e3138352e3138332e35370000000000000100000000000000000000000001"
        "00000800000100000000000100a8a2d7bebd8d8bdf110200")
    mid = bytes.fromhex('0000000001000102030101') + await has_ssan_zig(len(reg)) + reg.encode()
    mid += bytes.fromhex('0001030003000004')
    mid += await has_ssan_zig(len(client_version)) + client_version.encode()
    mid += await has_ssan_zig(len(client_version_code)) + client_version_code.encode()
    mid += csoversea_block
    clean_ip = server_ip.split(':')[0] if server_ip else "0.0.0.0"
    mid += await has_ssan_zig(len(clean_ip)) + clean_ip.encode()
    if access_token:
        clean_acc = access_token.strip()
        mid += await has_ssan_zig(len(clean_acc)) + clean_acc.encode()
    mid += await has_ssan_zig(len(esh)) + esh
    tg = (await uleb_encode(int(account_id)) + await uleb_encode(int(block_val)) +
          await uleb_encode(1) + await uleb_encode(int(mode_id)) +
          await uleb_encode(int(block_val)) + await uleb_encode(int(map_id)) + mid)
    process = await sv_frame(0x5E, match_code, 2, 447, 0, 1, garena420, udp_key)
    loading = await sv_frame(0x5A, match_code, 2, 448, 1, 1, tg, udp_key)
    return process.hex(), loading.hex()

# =========================== MOVEMENT 2001 ===========================
async def build_movement_packet_2001(key_bytes, msg_key, player_state, x, y, z,
                                     tick, counter, move_state, account_uid,
                                     room_code, phase="arena"):
    if phase == "game":
        TAIL = (b"\x00"*7 + b"\x93\xca\xd5\xf9\x0b" + b"\x8e\xfe\xfc\xf7\x0b"
                + b"\xf4\xe0\xfe\xf7\x03" + b"\x00\x00")
    else:
        TAIL = (b"\x00"*7 + b"\xbd\xb6\xe1\xf3\x03" + b"\x00"
                + b"\xc4\xb6\xe1\xfb\x0b" + b"\x00\x00")
    body = bytearray()
    body += await uleb_encode(int(account_uid))
    body += await uleb_encode(int(player_state))
    body += await zigzag_encode_signed(int(x))
    body += await zigzag_encode_signed(int(y))
    body += await zigzag_encode_signed(int(z))
    body += b"\x00"
    body += await uleb_encode(int(move_state))
    body += TAIL
    body += await uleb_encode(int(tick))
    body += b"\x00\x00\x00"
    body += await uleb_encode(int(counter))
    body = bytes(body)
    k = key_bytes[0]; v80 = ((k << 8) | k) & 0xFFFF
    h = bytearray()
    h.append(msg_key & 0xFF); h.append(0x00); h.append(0x00 ^ k)
    h += ((2001 ^ v80) & 0xFFFF).to_bytes(2, "little")
    h += ((len(body) ^ v80) & 0xFFFF).to_bytes(2, "little")
    h.append(0x00 ^ k)
    h[1] = (await crc7_buff(0, bytes(h[2:]) + body)) & 0x7F
    return bytes(h) + body

class MovementTracker:
    PLAYER_STATE = {"idle":0, "walk":1, "run":2, "sprint":3, "jump":4, "crouch":5, "prone":6}
    def __init__(self, pattern="circle", radius=180):
        self.pattern = pattern; self.radius = int(radius)
        self.tick = 0; self.counter = 0
        self.angle = random.uniform(0, 6.2832)
        self.speed_multiplier = 1.0; self.dir_flip = 1
        self._next_speed_roll = time.time()
        self.state = "walk"; self.state_start = time.time()
        self.state_duration = random.uniform(2.0, 4.0)
        self._roll_new_speed()
    def _roll_new_speed(self):
        try:
            lo, hi = float(MOVEMENT_SPEED_MIN), float(MOVEMENT_SPEED_MAX)
            if hi < lo: lo, hi = hi, lo
            self.speed_multiplier = round(random.uniform(lo, hi), 2)
        except Exception: self.speed_multiplier = 1.0
        self._next_speed_roll = time.time() + MOVEMENT_UPDATE_SPEED_EVERY
    def _pick_next_state(self):
        r = random.random()
        ci = MOVEMENT_STATE_IDLE_CHANCE
        cw = ci + MOVEMENT_STATE_WALK_CHANCE
        cr = cw + MOVEMENT_STATE_RUN_CHANCE
        cs = cr + MOVEMENT_STATE_SPRINT_CHANCE
        cj = cs + MOVEMENT_STATE_JUMP_CHANCE
        cc = cj + MOVEMENT_STATE_CROUCH_CHANCE
        if r < ci: return "idle", random.uniform(0.6, 1.6)
        elif r < cw: return "walk", random.uniform(1.6, 3.0)
        elif r < cr: return "run", random.uniform(2.0, 4.0)
        elif r < cs: return "sprint", random.uniform(1.2, 2.5)
        elif r < cj: return "jump", random.uniform(0.35, 0.65)
        elif r < cc: return "crouch", random.uniform(1.2, 2.5)
        else: return "prone", random.uniform(1.5, 3.5)
    def _generate_movement(self):
        p = (self.pattern or "circle").lower()
        x = y = z = 0; ms = 0
        if p == "circle":
            self.angle += 0.35 * self.dir_flip
            if self.angle > 6.2832: self.angle -= 6.2832
            elif self.angle < 0: self.angle += 6.2832
            x = int(self.radius * random.uniform(0.9, 1.1) * (1.0 if self.angle % 3.14 < 1.57 else -1.0))
            y = int(self.radius * random.uniform(0.4, 0.6))
            z = int(self.radius * random.uniform(0.9, 1.1) * (1.0 if self.angle % 6.28 < 3.14 else -1.0))
            ms = 0x0F
        else:
            x = random.randint(-self.radius, self.radius)
            y = random.randint(-40, 40)
            z = random.randint(-self.radius, self.radius)
            ms = random.choice([0x01, 0x02, 0x04, 0x08, 0x0F])
        return x, y, z, ms
    def next(self):
        now = time.time()
        if now >= self._next_speed_roll: self._roll_new_speed()
        if now - self.state_start > self.state_duration:
            old = self.state
            self.state, self.state_duration = self._pick_next_state()
            self.state_start = now
            if old != self.state:
                print_info(f"[MOVE] {old} -> {self.state} ({self.state_duration:.2f}s)")
        self.tick = (self.tick + 1) & 0xFFFFFFFF
        self.counter = (self.counter + 1) & 0xFFFFFFFF
        ps = self.PLAYER_STATE.get(self.state, 1)
        if self.state in ("idle", "crouch", "prone"): x = y = z = 0; move_state = 0
        else: x, y, z, move_state = self._generate_movement()
        return ps, x, y, z, move_state, self.tick, self.counter, self.speed_multiplier, self.state

async def movement_worker(sock, ip, port, udp_key_bytes, match_code, account_id, stop_event, match_index):
    loop = asyncio.get_event_loop(); addr = (ip, port)
    tracker = MovementTracker(pattern=MOVEMENT_PATTERN, radius=MOVEMENT_RADIUS)
    sent = 0
    try:
        await asyncio.sleep(bypass_jitter(3.0, 0.5))
        while not stop_event.is_set():
            try:
                ps, x, y, z, ms, tick, cnt, sm, st = tracker.next()
                pkt = await build_movement_packet_2001(udp_key_bytes, 0x6B, ps, x, y, z, tick, cnt, ms,
                                                       account_id, match_code, phase="arena")
                await loop.sock_sendto(sock, pkt, addr); sent += 1
                if sent % 30 == 0: print_info(f"[MOVE] #{match_index} sent={sent} {st}({ps}) rate={sm}x")
                base = MOVEMENT_TICK_MS / 1000.0
                try: smx = max(0.7, min(float(sm), 1.6))
                except Exception: smx = 1.0
                wait = max(0.10, min(base / smx, 0.40))
                wait = bypass_packet_jitter(wait, 0.20)
                try: await asyncio.wait_for(stop_event.wait(), timeout=wait); break
                except asyncio.TimeoutError: pass
            except asyncio.CancelledError: break
            except Exception: await asyncio.sleep(0.5)
    except asyncio.CancelledError: pass
    finally: print_info(f"[MOVE] #{match_index} stopped sent={sent}")

async def send_anti_afk_fire(sock, addr, udp_key_bytes, match_code, account_id):
    try:
        _, nr = await layouts_from_mask(match_code)
        try: pid = int(account_id)
        except Exception: pid = 0
        ts = int(time.time() * 1000) & 0xFFFFFFFF
        wid = random.choice([101, 102, 103, 201, 202])
        fire = struct.pack("<IIHB", pid & 0xFFFFFFFF, ts, wid, 1)
        fp = await build_packet(0x68, nr, 0, 104, None, 0, fire, udp_key_bytes, encrypted=False)
        loop = asyncio.get_event_loop()
        await loop.sock_sendto(sock, fp, addr)
        await asyncio.sleep(bypass_jitter(random.uniform(0.15, 0.30), 0.05))
        stop = struct.pack("<IIHB", pid & 0xFFFFFFFF, ts + 200, wid, 0)
        sp = await build_packet(0x68, nr, 0, 105, None, 0, stop, udp_key_bytes, encrypted=False)
        await loop.sock_sendto(sock, sp, addr)
        return True
    except Exception: return False

async def anti_afk_worker(sock, ip, port, udp_key_bytes, match_code, account_id, stop_event, match_index):
    addr = (ip, port); fires = 0
    try:
        await asyncio.sleep(bypass_jitter(2.5, 0.5))
        while not stop_event.is_set():
            try:
                if await send_anti_afk_fire(sock, addr, udp_key_bytes, match_code, account_id): fires += 1
                w = random.uniform(ANTI_AFK_MIN_INTERVAL, ANTI_AFK_MAX_INTERVAL)
                w = bypass_packet_jitter(w, 0.25)
                try: await asyncio.wait_for(stop_event.wait(), timeout=w); break
                except asyncio.TimeoutError: pass
            except asyncio.CancelledError: break
            except Exception: await asyncio.sleep(1.0)
    except asyncio.CancelledError: pass
    finally:
        if fires > 0: print_info(f"[AFK] #{match_index} Fires={fires}")

# =========================== PLAY GAME ===========================
async def play_game(server_ip_port, thunder, sharma, udp_key, match_code,
                    account_id, player_region, client_version, key, iv,
                    match_index, on_match_complete=None):
    t0 = time.time()
    ping_task = anti_task = move_task = None
    sock = None
    ping_stop = asyncio.Event(); anti_stop = asyncio.Event(); move_stop = asyncio.Event()
    uid_str = str(account_id); completed = False
    try:
        ip, port = server_ip_port.split(":"); port = int(port)
        rip = await resolve_host_cloudflare(ip)
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try: sock.bind(('0.0.0.0', 0))
        except Exception: pass
        optimize_udp_socket(sock); sock.setblocking(False)
        udp_key_bytes = bytes.fromhex(udp_key)
        await asyncio.sleep(bypass_hello_delay())
        hello = await build_hello_packet(f"{account_id}_2585", udp_key_bytes, match_code)
        await loop.sock_sendto(sock, bytes.fromhex(hello), (rip, port))
        ack_state = "waiting_for_hello_reply"
        thunder_sent = False; join_match_rcv = False; local_closed = False
        send_lock = asyncio.Lock(); anti_started = False
        ping_task = asyncio.create_task(keepalive_ping(sock, rip, port, udp_key_bytes, match_code, ping_stop))
        last_activity = time.time()
        print_colored(f"[MATCH #{match_index}] UDP -> {server_ip_port} (DNS:{rip})", Colors.MAGENTA)

        async def start_workers():
            nonlocal anti_task, move_task, anti_started
            if anti_started: return
            anti_started = True
            anti_task = asyncio.create_task(anti_afk_worker(sock, rip, port, udp_key_bytes, match_code,
                                                            account_id, anti_stop, match_index))
            print_success(f"[AFK] Worker #{match_index}")
            if ENABLE_SPEED_MOVEMENT:
                move_task = asyncio.create_task(movement_worker(sock, rip, port, udp_key_bytes, match_code,
                                                                account_id, move_stop, match_index))
                print_success(f"[MOVE] Worker #{match_index} | {MOVEMENT_PATTERN}")

        async def send_startup():
            nonlocal ack_state, thunder_sent
            if thunder_sent: return
            async with send_lock:
                if thunder_sent: return
                try:
                    await loop.sock_sendto(sock, bytes.fromhex(thunder), (rip, port))
                    thunder_sent = True
                    await asyncio.sleep(bypass_jitter(0.1, 0.05))
                    _, nr = await layouts_from_mask(match_code)
                    ack = await build_packet(0x68, nr, 0, 2, None, 1, b"\x01\x00", udp_key_bytes)
                    await loop.sock_sendto(sock, ack, (rip, port))
                    await asyncio.sleep(bypass_jitter(0.2, 0.05))
                    await loop.sock_sendto(sock, bytes.fromhex(sharma), (rip, port))
                    ack_state = "thunder_sharma_sent"
                    print_success(f"[MATCH #{match_index}] Startup delivered")
                    await start_workers()
                except Exception as e: print_error(f"[MATCH #{match_index}] startup: {e}")

        while not local_closed:
            if time.time() - t0 > MAX_MATCH_DURATION: break
            try:
                resp, saddr = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=1.5)
                if resp:
                    last_activity = time.time()
                    frame = await decode_packet(resp, udp_key_bytes, match_code)
                    if frame:
                        pt = await classify(frame)
                        if frame['cmd'] in [103, 107]:
                            print_success(f"[MATCH #{match_index}] END cmd={frame['cmd']}")
                            try: await add_glory(1)
                            except Exception: pass
                            completed = True; local_closed = True; continue
                        if frame['cmd'] == 101:
                            try:
                                _, nr = await layouts_from_mask(match_code)
                                a = await build_packet(0x68, nr, 0, 2, None, 1, b"\x01\x00", udp_key_bytes)
                                await loop.sock_sendto(sock, a, saddr)
                            except Exception: pass
                            continue
                        if pt in ["ACK", "PING", "HELLO", "JOIN_MATCH"]:
                            if pt == "HELLO" and ack_state == "waiting_for_hello_reply":
                                _, r = await reply_for(frame, udp_key_bytes, match_code, ack_style="short")
                                if r: await loop.sock_sendto(sock, r, saddr)
                                ack_state = "ack_sent_waiting"
                            elif pt == "ACK":
                                if ack_state == "waiting_for_hello_reply":
                                    _, r = await reply_for(frame, udp_key_bytes, match_code)
                                    if r: await loop.sock_sendto(sock, r, saddr)
                                    ack_state = "ready_to_send_thunder"
                                elif ack_state == "ack_sent_waiting":
                                    ack_state = "ready_to_send_thunder"
                                else:
                                    _, r = await reply_for(frame, udp_key_bytes, match_code)
                                    if r: await loop.sock_sendto(sock, r, saddr)
                            elif pt == "PING":
                                _, r = await reply_for(frame, udp_key_bytes, match_code)
                                if r: await loop.sock_sendto(sock, r, saddr)
                            elif pt == "JOIN_MATCH" and not join_match_rcv:
                                _, r = await reply_for(frame, udp_key_bytes, match_code)
                                if r: await loop.sock_sendto(sock, r, saddr); join_match_rcv = True
            except asyncio.TimeoutError:
                if ack_state == "ready_to_send_thunder" and not thunder_sent:
                    await send_startup()
                elif ack_state == "waiting_for_hello_reply":
                    if (time.time() - last_activity) > 3.0:
                        try:
                            hp = await build_hello_packet(f"{account_id}_2585", udp_key_bytes, match_code)
                            await loop.sock_sendto(sock, bytes.fromhex(hp), (rip, port))
                        except Exception: pass
                        last_activity = time.time()
                    if (time.time() - t0) > 12.0:
                        print_warning(f"[MATCH #{match_index}] Handshake timeout"); break
                elif ack_state == "thunder_sharma_sent":
                    if (time.time() - last_activity) > MATCH_IDLE_TIMEOUT:
                        print_success(f"[MATCH #{match_index}] Finished naturally")
                        completed = True; break
                continue
            except BlockingIOError: await asyncio.sleep(0.05)
            except OSError: await asyncio.sleep(0.5); continue
            except Exception: await asyncio.sleep(0.5); continue
            if ack_state == "ready_to_send_thunder" and not thunder_sent:
                await start_startup_safe(send_startup)
        return f"match #{match_index} finished"
    except Exception as e:
        print_error(f"[MATCH #{match_index}] {e}"); return f"match #{match_index} error"
    finally:
        if completed:
            try: bot_state.increment_match(uid_str)
            except Exception: pass
            if on_match_complete:
                try: asyncio.create_task(on_match_complete())
                except Exception: pass
        anti_stop.set(); move_stop.set()
        for t in (anti_task, move_task):
            if t:
                try: await asyncio.wait_for(t, timeout=2.0)
                except Exception:
                    t.cancel()
                    try: await t
                    except Exception: pass
        ping_stop.set()
        if ping_task:
            ping_task.cancel()
            try: await ping_task
            except asyncio.CancelledError: pass
        if sock:
            try: sock.close()
            except Exception: pass
        rem = await _dec_match(uid_str)
        total = await _get_total_match_count()
        print_info(f"[MATCH #{match_index}] Closed. UID:{rem} Total:{total}")
        try: bot_state.update_status(uid_str, "IN_MATCH" if rem > 0 else "ONLINE", rem)
        except Exception: pass

async def start_startup_safe(fn):
    try: await fn()
    except Exception: pass

# =========================== CLAN JOIN (dedup) ===========================
def _get_region_from_jwt(jwt_token):
    try:
        p = jwt_token.split('.')[1]
        p += '=' * (-len(p) % 4)
        d = json.loads(base64.urlsafe_b64decode(p))
        return (d.get('lock_region') or d.get('noti_region') or 'ID').upper()
    except Exception:
        return "ID"

def _get_region_url(region):
    r = str(region).upper()
    if r == "IND": return "https://client.ind.freefiremobile.com"
    if r in ("BR", "US", "NA"): return "https://client.us.freefiremobile.com"
    return "https://clientbp.ggpolarbear.com"

def _create_clan_payload(clan_id):
    try:
        raw = encode_varint(0x08) + encode_varint(int(clan_id))   # field 1 varint - no pb2 needed
        return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(raw, AES.block_size))
    except Exception:
        return None

async def join_clan(jwt_token, clan_id):
    try:
        region = _get_region_from_jwt(jwt_token)
        base_url = _get_region_url(region)
        payload = _create_clan_payload(clan_id)
        if not payload: return False, "payload build failed"
        hdrs = {
            "Expect": "100-continue",
            "Authorization": f"Bearer {jwt_token}",
            "X-Unity-Version": "2018.4.11f1", "X-GA": "v1 1",
            "ReleaseVersion": "OB55", "Content-Type": "application/octet-stream",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; SM-A305F Build/RP1A.200720.012)",
            "Host": base_url.replace("https://", ""),
            "Connection": "Keep-Alive", "Accept-Encoding": "gzip",
        }
        r = await client.post(base_url.rstrip("/") + "/RequestJoinClan", headers=hdrs,
                              content=payload, timeout=15.0)
        print_info(f"[CLAN] Join status: {r.status_code} | Region: {region}")
        if r.status_code == 200:
            print_success(f"[CLAN] Join request sent for clan {clan_id}")
            return True, ""
        return False, r.text.strip()[:120]
    except Exception as e:
        print_error(f"[CLAN] Join error: {e}")
        return False, str(e)


# =========================== FRIEND REQUEST ===========================
async def _process_social_packet(raw_bytes, account_data, tag="MAIN"):
    try:
        hex_data = raw_bytes.hex()
        candidates = []
        if len(raw_bytes) >= 6:
            plen = raw_bytes[4]
            if 0 < plen <= (len(raw_bytes) - 5):
                try:
                    inner = bytes(raw_bytes[5:5 + plen])
                    d0 = decode_protobuf_dict_safe(inner) or _wrap_manual(inner)
                    if d0: candidates.append((5, d0))
                except Exception: pass
        for off in [5, 6, 10, 8, 12, 4, 0]:
            if off >= len(hex_data): continue
            try: inner = bytes.fromhex(hex_data[off:])
            except Exception: continue
            decoded = decode_protobuf_dict_safe(inner) or _wrap_manual(inner)
            if decoded: candidates.append((off, decoded))
        if not candidates: return
        candidates.sort(key=lambda x: len(x[1]) if isinstance(x[1], dict) else 0, reverse=True)
        best_off, decoded = candidates[0]
        def _v(c, k):
            if not isinstance(c, dict): return None
            v = c.get(k) if k in c else c.get(str(k))
            if isinstance(v, dict) and "data" in v: return v["data"]
            return v
        f5w = decoded.get("5") or decoded.get(5) if isinstance(decoded, dict) else None
        f5 = f5w["data"] if isinstance(f5w, dict) and "data" in f5w else f5w
        target = None
        if isinstance(f5, dict):
            for fk in [1, 2, 3, 4, 6]:
                c = _v(f5, fk)
                if c and str(c).isdigit() and 6 <= len(str(c)) <= 15:
                    target = str(c); break
        if not target: return
        own = str(account_data.get('account_id', '')) if account_data else ''
        if own and target == own: return
        if not _accept_friend_request or not account_data: return
        jwt = account_data.get('token'); srv = account_data.get('server_url')
        rel = account_data.get('release_version'); own_id = account_data.get('account_id')
        if not (jwt and srv and rel and own_id): return
        url = f"{srv.rstrip('/')}/ConfirmFriendRequest"
        fields = {"1": int(target), "2": int(own_id)}
        try:
            payload = bytes.fromhex(encrypt_api(create_proto(fields).hex()))
            hdrs = {
                "authorization": f"Bearer {jwt}",
                "host": srv.replace("https://", "").replace("http://", "").split("/")[0],
                "x-ga": "v1 1", "content-type": "application/x-www-form-urlencoded",
                "accept-encoding": "deflate, gzip", "releaseversion": f"{rel}",
                "user-agent": "UnityPlayer/2022.3.47f1", "accept": "*/*",
                "x-unity-version": "2022.3.47f1",
            }
            async with httpx.AsyncClient(verify=False, timeout=8.0) as s:
                r = await s.post(url, headers=hdrs, content=payload)
                if r.status_code == 200 and len(r.content) > 0:
                    print_success(f"[FRIEND-{tag}]  {target}")
        except Exception: pass
    except Exception: pass

def _wrap_manual(inner):
    try:
        alt = _decode_proto_manual(inner)
        if alt and len(alt) > 0:
            return {str(k): {"data": v} for k, v in alt.items()}
    except Exception: pass
    return None

def _raw_fields_multi(data: bytes) -> dict:
    out = {}
    offset = 0; data_len = len(data)
    while offset < data_len:
        header = 0; shift = 0
        while offset < data_len:
            b = data[offset]; offset += 1
            header |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
            if shift > 28: break
        if header == 0: break
        fn = header >> 3; wt = header & 0x7
        if wt == 0:
            v = 0; shift = 0
            while offset < data_len:
                b = data[offset]; offset += 1
                v |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
        elif wt == 2:
            ln = 0; shift = 0
            while offset < data_len:
                b = data[offset]; offset += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            if ln > data_len - offset: break
            v = bytes(data[offset:offset+ln]); offset += ln
        elif wt == 5: v = bytes(data[offset:offset+4]); offset += 4
        elif wt == 1: v = bytes(data[offset:offset+8]); offset += 8
        else: break
        out.setdefault(fn, []).append(v)
    return out


def _parse_team_roster(inner: bytes):
    """0500 team state -> (team_code, member_uids). Captain ka join yahi se detect hota hai."""
    team_code = None
    uids = set()
    try:
        for f5 in _raw_fields_multi(inner).get(5, []):
            if not isinstance(f5, (bytes, bytearray)): continue
            mf = _raw_fields_multi(bytes(f5))
            for code_b in mf.get(11, []):
                if isinstance(code_b, (bytes, bytearray)):
                    s = bytes(code_b).decode("utf-8", "ignore").strip()
                    if s.isdigit() and len(s) == 7: team_code = s
            for m in mf.get(6, []):
                if isinstance(m, (bytes, bytearray)):
                    mm = _raw_fields_multi(bytes(m))
                    for v in mm.get(1, []):
                        if isinstance(v, int) and v > 1000:
                            uids.add(str(v)); break
    except Exception:
        pass
    return team_code, uids


# =========================== TEAM INVITE HANDLER ===========================
async def _handle_team_invite(hex_data, pl, uid_str, current_account_data,
                              current_key, current_iv, writer, team_key):
    try:
        if DEBUG_TEAM_PACKETS:
            print_info(f"[TEAM-IN {uid_str}] len={pl} hex={hex_data[:80]}")

        ts = _get_team(team_key)
        #  server roster (member list + code) — captain join yahi se detect hota hai
        try:
            for _roff in (10, 8, 6):
                if len(hex_data) <= _roff + 8: continue
                try:
                    _tc, _uids = _parse_team_roster(bytes.fromhex(hex_data[_roff:]))
                except Exception:
                    continue
                if _tc:
                    async with ts["lock"]:
                        if not ts.get("team_code"):
                            ts["team_code"] = _tc
                for _u in _uids:
                    await _team_member_join(_u, team_key)
                if _tc or _uids:
                    break
        except Exception:
            pass

        candidates = []
        for off in [10, 8, 6, 12, 14, 4, 16, 0, 20, 2]:
            if off >= len(hex_data) or len(hex_data) - off < 4: continue
            try: inner = bytes.fromhex(hex_data[off:])
            except Exception: continue

            dec = decode_protobuf_dict_safe(inner)
            if dec and isinstance(dec, dict) and len(dec) > 0:
                candidates.append((off, "parser", dec)); continue

            alt = _decode_proto_manual(inner)
            if alt and len(alt) > 0:
                candidates.append((off, "manual", {str(k): {"data": v} for k, v in alt.items()}))

        if not candidates:
            return False

        candidates.sort(key=lambda x: len(x[2]) if isinstance(x[2], dict) else 0, reverse=True)
        best_off, decoder, dec = candidates[0]
        print_info(f"[TEAM-IN {uid_str}] decoded@{best_off}/{decoder} fields={list(dec.keys())[:20]}")

        def _unwrap(d, keys):
            if not isinstance(d, dict): return None
            for k in keys:
                v = d.get(k) if k in d else d.get(str(k))
                if isinstance(v, dict) and "data" in v: return v["data"]
                if isinstance(v, dict): return v
            return None

        target_dict = None
        for kp in [["5"], ["4"], ["6"], ["2"], ["1"]]:
            u = _unwrap(dec, kp)
            if isinstance(u, dict) and len(u) >= 3:
                target_dict = u
                print_info(f"[TEAM-IN {uid_str}] nested@{kp[0]}")
                break
        if target_dict is None: target_dict = dec

        def _g(d, k):
            if not isinstance(d, dict): return None
            v = d.get(k) if k in d else d.get(str(k))
            if isinstance(v, dict) and "data" in v: return v["data"]
            if isinstance(v, dict):
                for kk in ["1","2","3","4","5","6","7","8"]:
                    vv = v.get(kk) if kk in v else v.get(int(kk))
                    if isinstance(vv, dict) and "data" in vv: return vv["data"]
            return v

        group_id = None
        for k in list(target_dict.keys()):
            c = _g(target_dict, k)
            if isinstance(c, str) and 20 <= len(c) <= 40:
                group_id = c
                print_success(f"[TEAM-IN {uid_str}]  group_id@f{k}: {c[:24]}...")
                break
        if not group_id:
            for k in list(dec.keys()):
                c = _g(dec, k)
                if isinstance(c, str) and 20 <= len(c) <= 40:
                    group_id = c
                    print_success(f"[TEAM-IN {uid_str}]  group_id@root.f{k}: {c[:24]}...")
                    break

        own_acc = str(current_account_data.get('account_id')) if current_account_data else None
        inviter = None
        all_f = list(target_dict.keys()) + list(dec.keys())
        for k in all_f:
            c = _g(target_dict, k) or _g(dec, k)
            if c and str(c).isdigit() and 6 <= len(str(c)) <= 15:
                if str(c) != uid_str and str(c) != own_acc:
                    inviter = str(c)
                    print_success(f"[TEAM-IN {uid_str}]  inviter@f{k}: {c}")
                    break

        tc = None
        for k in all_f:
            c = _g(target_dict, k) or _g(dec, k)
            if c and str(c).isdigit() and len(str(c)) == 7:
                tc = str(c); break
        if tc:
            async with ts["lock"]:
                ts["team_code"] = tc
            print_success(f"[TEAM-IN {uid_str}]  Team Code: {tc}")

        is_member = (ts["leader_uid"] != uid_str)
        if inviter and group_id and is_member:
            async with ts["members_lock"]:
                already = uid_str in ts["members"]
            if already:
                return True
            print_success(f"[TEAM-IN {uid_str}]  ACCEPT: inviter={inviter} group={group_id[:16]}...")
            accepted = False
            for attempt in range(3):
                try:
                    _reg = str(current_account_data.get('region', 'ID')) if isinstance(current_account_data, dict) else 'ID'
                    acc = accept_team_invite(int(inviter), group_id, current_key, current_iv, region=_reg)
                    if writer and not writer.is_closing():
                        writer.write(acc); await writer.drain()
                        print_success(f"[TEAM-IN {uid_str}]  accept #{attempt+1} ({len(acc)}B)")
                        accepted = True; break
                except Exception as e:
                    print_warning(f"[TEAM-IN {uid_str}] accept err #{attempt+1}: {e}")
                await asyncio.sleep(0.4)
            if accepted:
                await _team_member_join(uid_str, team_key)
            return True
        return False
    except Exception as e:
        print_warning(f"[TEAM-IN {uid_str}] error: {e}")
        return False

# =========================== FUNCTIONAL (MAIN) — BATTLE ROYALE ONLY ===========================
async def functional_battle_royale(addrs, starter_packet, account_region, client_version,
                                    key, iv, account_id="", account_data=None,
                                    account_level=2, max_reconnects=10):
    reconnects = 0
    ip, port = addrs.split(":")
    play_matches: List[asyncio.Task] = []
    no_response_count = 0
    search_attempts = 0
    last_start_time = 0.0
    uid_str = str(account_id)
    current_level = int(account_level or 2)
    consecutive_parse_failures = 0
    current_token = starter_packet
    current_key = key; current_iv = iv
    current_account_data = account_data
    reconnect_count_total = 0
    region_index = 0
    current_search_region = REGION_ROTATION_LIST[0]
    _writer_ref = {"w": None}
    _team_active = {"flag": True}
    # team_key scopes this account's squad state so concurrent clients (each
    # running their own 3-4 bot squad) never see each other's peers/leader/
    # match code. Falls back to the account's own uid when run standalone
    # (no client_id in account_data) so solo-worker usage still behaves.
    team_key = str((account_data or {}).get("client_id") or uid_str)
    ts = _get_team(team_key)
    team_bots = int(_cfg(account_data, "team_bots", TEAM_BOTS))
    user_game_uid = str(_cfg(account_data, "captain_uid", USER_GAME_UID) or "")

    async def trigger_post_match_refresh():
        await asyncio.sleep(EXP_REFRESH_AFTER_MATCH_DELAY)
        try:
            cd = bot_state.account_credentials.get(uid_str)
            if cd: await refresh_account_profile(cd)
        except Exception: pass

    # =========== LEADER: create  invite-loop  ready  start ===========
    async def _team_coordinator():
        try:
            deadline = time.time() + TEAM_PEER_WAIT_TIMEOUT
            while time.time() < deadline:
                async with ts["lock"]:
                    n = len(ts["peers"])
                if n >= team_bots: break
                if bot_state.is_paused(uid_str): return
                await asyncio.sleep(0.5)

            async with ts["lock"]:
                n = len(ts["peers"])
                is_leader = (ts["leader_uid"] == uid_str)
                peers_snapshot = dict(ts["peers"])

            if not is_leader:
                print_info(f"[TEAM] {uid_str} waiting as member...")
                return

            # parallel-match gate - wait while the account is at MAX
            while True:
                _am = await _get_match_count(uid_str)
                if _am < MAX_CONCURRENT_MATCHES: break
                if bot_state.is_paused(uid_str): return
                print_info(f"[TEAM] {uid_str} active matches {_am}/{MAX_CONCURRENT_MATCHES} - hold")
                await asyncio.sleep(3.0)
            if n < team_bots:
                print_warning(f"[TEAM] Only {n}/{team_bots} bot peers")
            else:
                print_success(f"[TEAM]  All {n} bot peers online")

            await asyncio.sleep(TEAM_READY_DELAY)
            print_success(f"[TEAM]  {uid_str} creating team...")

            async with ts["lock"]:
                ts["team_code"] = None
                ts["pk_code"] = None

            own_id_num = None
            if current_account_data: own_id_num = current_account_data.get('account_id')
            if not own_id_num:
                try: own_id_num = int(uid_str)
                except Exception: own_id_num = 0

            try:
                pkt = create_team(current_key, current_iv, client_version)
                if _writer_ref["w"] and not _writer_ref["w"].is_closing():
                    _writer_ref["w"].write(pkt); await _writer_ref["w"].drain()
                    print_info(f"[TEAM] create_team sent ({len(pkt)}B)")
                    await _team_member_reset(team_key, keep_leader=False)
                    await _team_member_join(uid_str, team_key)
            except Exception as e:
                print_error(f"[TEAM] create_team: {e}")

            await asyncio.sleep(TEAM_CREATE_WAIT)

            peer_uids = []
            for peer_uid, info in peers_snapshot.items():
                if peer_uid == uid_str: continue
                pa = None
                try: pa = info["data"].get("account_id")
                except Exception: pass
                if not pa: pa = peer_uid
                peer_uids.append(int(pa))
            invite_targets = list(peer_uids)
            if user_game_uid:
                invite_targets.append(int(user_game_uid))
            print_info(f"[TEAM] Will invite {len(invite_targets)}: {invite_targets}")

            heartbeat_stop = asyncio.Event()
            async def heartbeat_loop():
                while not heartbeat_stop.is_set():
                    try:
                        if _writer_ref["w"] and not _writer_ref["w"].is_closing():
                            pkt = team_fresh(int(own_id_num), current_key, current_iv)
                            _writer_ref["w"].write(pkt); await _writer_ref["w"].drain()
                    except Exception: pass
                    try: await asyncio.wait_for(heartbeat_stop.wait(), timeout=TEAM_HEARTBEAT_INTERVAL)
                    except asyncio.TimeoutError: pass
            hb_task = asyncio.create_task(heartbeat_loop())

            print_success(f"[TEAM]  Invite-loop start (max {int(TEAM_INVITE_MAX_TIME)}s)")
            invite_start = time.time()
            cycle = 0
            while time.time() - invite_start < TEAM_INVITE_MAX_TIME:
                if not _team_active["flag"]: break
                if bot_state.is_paused(uid_str): break
                cnt = await _team_member_count(team_key)
                if cnt >= TEAM_SIZE:
                    print_success(f"[TEAM]  Full team ({cnt}/{TEAM_SIZE})!")
                    break
                cycle += 1
                for i, tuid in enumerate(invite_targets):
                    async with ts["members_lock"]:
                        joined = str(tuid) in ts["members"]
                    if joined: continue
                    try:
                        inv = invite_player_in_team(tuid, current_key, current_iv, region=account_region)
                        if _writer_ref["w"] and not _writer_ref["w"].is_closing():
                            _writer_ref["w"].write(inv); await _writer_ref["w"].drain()
                            print_success(f"[TEAM]  invite {tuid} cycle#{cycle}")
                    except Exception as e:
                        print_warning(f"[TEAM] invite err: {e}")
                    await asyncio.sleep(0.2)
                try: await asyncio.wait_for(heartbeat_stop.wait(), timeout=TEAM_INVITE_CYCLE_WAIT)
                except asyncio.TimeoutError: pass

            final_cnt = await _team_member_count(team_key)
            print_colored(f"[TEAM] == FINAL: In team ({final_cnt}/{TEAM_SIZE}) ==", Colors.MAGENTA)

            if final_cnt < TEAM_SIZE:
                print_error(f"[TEAM] Team incomplete ({final_cnt}/{TEAM_SIZE}) - abort (no start, no solo drops)")
                heartbeat_stop.set()
                try: hb_task.cancel(); await asyncio.gather(hb_task, return_exceptions=True)
                except Exception: pass
                return

            await asyncio.sleep(TEAM_FINAL_WAIT)
            heartbeat_stop.set()
            try: hb_task.cancel(); await asyncio.gather(hb_task, return_exceptions=True)
            except Exception: pass

            print_success(f"[TEAM]  {uid_str} loop: ready  1s  start (every 5s) - FULL TEAM gate")
            while _team_active["flag"]:
                try:
                    if bot_state.is_paused(uid_str): break
                    async with ts["lock"]:
                        if ts["leader_uid"] != uid_str: break
                    _tcnt = await _team_member_count(team_key)
                    if _tcnt < TEAM_SIZE:
                        await asyncio.sleep(2.0)
                        continue
                    oid = current_account_data.get('account_id', own_id_num) if current_account_data else own_id_num
                    try:
                        rdy = ready_for_game(int(oid), current_key, current_iv)
                        if _writer_ref["w"] and not _writer_ref["w"].is_closing():
                            _writer_ref["w"].write(rdy); await _writer_ref["w"].drain()
                    except Exception as e: print_warning(f"[TEAM] ready err: {e}")
                    await asyncio.sleep(1.0)
                    sm = start_match_team(int(oid), current_key, current_iv, region=account_region)
                    if _writer_ref["w"] and not _writer_ref["w"].is_closing():
                        _writer_ref["w"].write(sm); await _writer_ref["w"].drain()
                except Exception as e: print_warning(f"[TEAM] start err: {e}")
                try: await asyncio.wait_for(asyncio.sleep(5.0), timeout=5.5)
                except asyncio.TimeoutError: pass
        except asyncio.CancelledError: raise
        except Exception as e:
            print_error(f"[TEAM] coordinator: {e}")
            traceback.print_exc()

    async def _member_match_trigger():
        try:
            await asyncio.sleep(TEAM_READY_DELAY + TEAM_MEMBER_START_DELAY)
            async with ts["lock"]:
                if ts["leader_uid"] == uid_str: return
            # parallel-match gate
            while _team_active["flag"]:
                _am = await _get_match_count(uid_str)
                if _am < MAX_CONCURRENT_MATCHES: break
                await asyncio.sleep(3.0)
            if not _team_active["flag"]: return
            print_success(f"[TEAM]  Member {uid_str}: passive (READY only, no START)")
            my_id = current_account_data.get('account_id', uid_str) if current_account_data else uid_str
            while _team_active["flag"]:
                try:
                    if bot_state.is_paused(uid_str): break
                    #  members never start - only leader cmd 1:9; READY only when full team
                    if await _team_member_count(team_key) < TEAM_SIZE:
                        await asyncio.sleep(2.0)
                        continue
                    if _writer_ref["w"] and not _writer_ref["w"].is_closing():
                        try:
                            rdy = ready_for_game(int(my_id), current_key, current_iv)
                            _writer_ref["w"].write(rdy); await _writer_ref["w"].drain()
                        except Exception as e: print_warning(f"[TEAM] member ready err: {e}")
                except Exception as e: print_warning(f"[TEAM] member trigger: {e}")
                try: await asyncio.wait_for(asyncio.sleep(5.0), timeout=5.5)
                except asyncio.TimeoutError: pass
        except asyncio.CancelledError: raise
        except Exception as e: print_warning(f"[TEAM] member outer: {e}")

    try:
        while True:
            writer = None; gateway_ping_task = None
            team_coord_task = None; member_trigger_task = None
            try:
                if current_account_data:
                    fresh = None
                    if current_account_data.get('auth_type') == 'guest' and current_account_data.get('auth_uid'):
                        fresh = cache_get(str(current_account_data['auth_uid']))
                    elif current_account_data.get('auth_type') == 'token' and current_account_data.get('auth_token'):
                        fresh = cache_get(f"tok_{current_account_data['auth_token'][:20]}")
                    if fresh:
                        # cache_get returns the cached login blob as it was
                        # saved pre-stamp — carry client_id/team_bots/captain_uid/
                        # clan_id/auto_join_clan forward or team_key reverts to
                        # uid_str on every reconnect (see _client_account_loop).
                        for _k in ('client_id', 'team_bots', 'captain_uid', 'clan_id', 'auto_join_clan'):
                            if _k in current_account_data:
                                fresh[_k] = current_account_data[_k]
                        current_account_data = fresh
                        current_key = fresh['aes_ak']; current_iv = fresh['iv_i']
                        current_token = await build_tcp_startup_packet(
                            fresh['account_id'], fresh['token'], fresh['server_time'],
                            current_key, current_iv,
                            region=fresh.get('region', account_region), typ='OnLine')
                        try:
                            lv = int(fresh.get('level', current_level) or current_level)
                            if lv != current_level: current_level = lv
                        except Exception: pass
                    else:
                        print_warning(f"[FUNC] Cache miss {uid_str} -> re-login")
                        try:
                            if current_account_data.get('auth_uid'):
                                cache_invalidate(str(current_account_data['auth_uid']))
                            if current_account_data.get('auth_token'):
                                cache_invalidate(f"tok_{current_account_data['auth_token'][:20]}")
                        except Exception: pass
                        raise ConnectionError("Cache expired")

                try:
                    if uid_str in bot_state.accounts:
                        ll = int(bot_state.accounts[uid_str].get('level', current_level) or current_level)
                        if ll != current_level:
                            print_info(f"[LEVEL] {uid_str}: {current_level} -> {ll}")
                            current_level = ll
                except Exception: pass

                rip = await resolve_host_cloudflare(ip)
                reader, writer = await asyncio.open_connection(rip, int(port))
                _writer_ref["w"] = writer
                raw_sock = writer.get_extra_info('socket')
                if raw_sock: optimize_tcp_socket(raw_sock)
                await asyncio.sleep(bypass_hello_delay())
                writer.write(bytes.fromhex(current_token)); await writer.drain()
                try:
                    ka = await send_keep_alive(account_region)
                    if ka and not writer.is_closing():
                        writer.write(ka); await asyncio.wait_for(writer.drain(), timeout=3)
                except Exception: pass

                async def func_gateway_keepalive():
                    ka_bytes = await send_keep_alive(account_region)
                    while True:
                        await asyncio.sleep(bypass_ka_jitter(3.0))
                        try:
                            if writer and not writer.is_closing():
                                writer.write(ka_bytes); await writer.drain()
                        except Exception: break
                gateway_ping_task = asyncio.create_task(func_gateway_keepalive())

                connection_start_time = asyncio.get_running_loop().time()
                last_watchdog_check = connection_start_time
                search_attempts = 0
                if reconnect_count_total > 0 and (reconnect_count_total % REGION_ROTATE_EVERY_N == 0):
                    region_index = (region_index + 1) % len(REGION_ROTATION_LIST)
                    current_search_region = REGION_ROTATION_LIST[region_index]
                    print_warning(f"[REGION ROTATE] {uid_str} -> {current_search_region}")
                else:
                    current_search_region = REGION_ROTATION_LIST[region_index]

                print_success(f"[FUNC] TCP connected {uid_str} (DNS:{rip} Lvl:{current_level} R:{current_search_region})")
                reconnects = 0; no_response_count = 0; last_start_time = 0.0

                async with ts["lock"]:
                    ts["peers"][uid_str] = {
                        "writer": writer, "data": current_account_data,
                        "key": current_key, "iv": current_iv}
                    # deterministic leader = lowest uid (no split-team race on reconnect)
                    if ts["leader_uid"] is None or uid_str < str(ts["leader_uid"]):
                        ts["leader_uid"] = uid_str
                        print_success(f"[TEAM]  {uid_str} is leader")

                if TEAM_MODE:
                    team_coord_task = asyncio.create_task(_team_coordinator())
                    member_trigger_task = asyncio.create_task(_member_match_trigger())
                else:
                    #  Always Battle Royale
                    await start_game_battle_royale(current_search_region, client_version, writer, current_key, current_iv)

                while True:
                    play_matches[:] = [m for m in play_matches if not m.done()]
                    active = await _get_match_count(uid_str)
                    try: bot_state.update_status(uid_str, "ONLINE" if active == 0 else "IN_MATCH", active)
                    except Exception: pass

                    now = asyncio.get_running_loop().time()
                    if now - last_watchdog_check >= MATCH_WATCHDOG_CHECK:
                        last_watchdog_check = now
                        search_attempts += 1
                        sd = now - connection_start_time
                        if sd > 30: print_warning(f"[WATCHDOG] {uid_str} searching {int(sd)}s att={search_attempts}")
                        if sd > MATCH_SEARCH_TIMEOUT or search_attempts >= MAX_SEARCH_ATTEMPTS:
                            raise ConnectionError("Search watchdog")
                    if search_attempts >= MAX_SEARCH_ATTEMPTS:
                        raise ConnectionError("Max attempts")

                    try:
                        data = await asyncio.wait_for(reader.read(8192), timeout=0.3)
                    except asyncio.TimeoutError:
                        no_response_count += 1
                        if TEAM_MODE:
                            if no_response_count > TEAM_IDLE_ITERS_TEAM:
                                raise ConnectionError("Team idle timeout")
                        else:
                            if no_response_count > 80: raise ConnectionError("idle")
                        continue

                    if not data: raise ConnectionError("closed")
                    hex_data = data.hex()
                    pl = len(data)
                    no_response_count = 0

                    if DEBUG_TEAM_PACKETS and not hex_data.startswith("0300") and not hex_data.startswith("0600"):
                        print_info(f"[RAW-IN {uid_str}] len={pl} prefix={hex_data[:24]}")

                    if hex_data.startswith("0600") and pl > 20:
                        await _process_social_packet(data, current_account_data, tag="MAIN")
                        continue

                    if hex_data.startswith("05") and pl > 10:
                        await _handle_team_invite(hex_data, pl, uid_str, current_account_data,
                                                  current_key, current_iv, writer, team_key)
                        continue

                    if hex_data.startswith("0300") and 10 < pl < 30:
                        continue

                    if hex_data.startswith("0300") and pl >= 300:
                        print_success("MATCH FOUND - UDP connect ho raha hai")

                        for tk in (team_coord_task, member_trigger_task):
                            if tk and not tk.done():
                                tk.cancel()
                                try: await tk
                                except asyncio.CancelledError: pass
                        team_coord_task = None; member_trigger_task = None
                        _team_active["flag"] = False

                        try:
                            res = json.loads(await decode_protobuf(hex_data[10:]))
                            token = udp_key = match_code = server_ip_port = None
                            match_account_id = block_val = None
                            if '42' in res and 'data' in res['42']:
                                match_code = res['42']['data']
                            if '5' in res and 'data' in res['5']:
                                r5 = res['5']['data']
                                server_ip_port = r5.get('2', {}).get('data')
                                udp_key = r5.get('3', {}).get('data')
                                token = r5.get('4', {}).get('data')
                                if '42' in r5: match_code = r5['42']['data']
                            if '1' in res and 'data' in res['1']:
                                match_account_id = res['1']['data']
                            if '5' in res and 'data' in res['5']:
                                block_val = res['5']['data'].get('1', {}).get('data')

                            effective = match_account_id or account_id or "BD_BOT"
                            if token and udp_key and match_code and server_ip_port:
                                try:
                                    async with ts["lock"]:
                                        ts["pk_code"] = str(match_code)
                                        ts["team_code"] = str(match_code)
                                    print_colored(f"[PK]  Room Code: {match_code}", Colors.MAGENTA)
                                    print_colored(f"[PK]  Server: {server_ip_port}", Colors.MAGENTA)
                                    print_colored(f"[PK]  UDP Key: {udp_key}", Colors.MAGENTA)
                                except Exception: pass

                                acc_tok = current_account_data.get('access_token', '') if current_account_data else ""
                                #  Always Battle Royale
                                mid, mp = BR_MODE_ID, BR_MAP_ID
                                tag = "BATTLE_ROYALE"
                                print_info(f"[TARGET] {tag} mid={mid} mp={mp}")
                                thunder, sharma = await build_match_startup_packets(
                                    token, udp_key, match_code, effective, block_val or 0,
                                    server_ip=server_ip_port, region=account_region,
                                    client_version=client_version, access_token=acc_tok,
                                    mode_id=mid, map_id=mp)
                                match_index = await _inc_match(uid_str)
                                total = await _get_total_match_count()
                                print_colored(f"[MATCH #{match_index}] UDP -> {server_ip_port} ({tag})", Colors.CYAN)

                                new_match = asyncio.create_task(play_game(
                                    server_ip_port, thunder, sharma, udp_key, match_code,
                                    effective, current_search_region, client_version,
                                    current_key, current_iv,
                                    match_index=match_index, on_match_complete=trigger_post_match_refresh))
                                play_matches.append(new_match)
                                consecutive_parse_failures = 0
                                active_now = await _get_match_count(uid_str)
                                print_info(f"[MATCH] #{match_index} live for {uid_str} (active={active_now})")

                                # non-blocking: close TCP, immediately search again (match runs in background)
                                try: writer.close(); await writer.wait_closed()
                                except Exception: pass
                                await asyncio.sleep(NEW_MATCH_DELAY)
                                reconnect_count_total += 1
                                break
                            else:
                                consecutive_parse_failures += 1
                                print_warning(f"[FUNC] Non-match big pkt #{consecutive_parse_failures}")
                                if consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES:
                                    consecutive_parse_failures = 0
                                try: writer.close(); await writer.wait_closed()
                                except Exception: pass
                                await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                                break
                        except Exception as e:
                            print_error(f"[FUNC] Match parse: {e}")
                            consecutive_parse_failures += 1
                            try: writer.close(); await writer.wait_closed()
                            except Exception: pass
                            await asyncio.sleep(NON_MATCH_RECONNECT_DELAY)
                            break

                    if 30 <= pl <= 40: continue

            except asyncio.CancelledError:
                print_warning(f"[FUNC] Cancelled — cancel {len(play_matches)} UDP")
                for m in play_matches:
                    if not m.done(): m.cancel()
                if play_matches:
                    await asyncio.gather(*play_matches, return_exceptions=True)
                play_matches.clear()
                if gateway_ping_task: gateway_ping_task.cancel()
                async with ts["lock"]:
                    ts["peers"].pop(uid_str, None)
                    if ts["leader_uid"] == uid_str:
                        ts["leader_uid"] = min(ts["peers"].keys()) if ts["peers"] else None
                        if ts["leader_uid"]:
                            print_info(f"[TEAM] new leader: {ts['leader_uid']}")
                await _team_member_leave(uid_str, team_key)
                raise
            except Exception as e:
                if gateway_ping_task: gateway_ping_task.cancel()
                print_error(f"[FUNC] TCP state {uid_str}: {e}")
                play_matches[:] = [m for m in play_matches if not m.done()]
                if writer:
                    try: writer.close(); await writer.wait_closed()
                    except Exception: pass
                if "Cache expired" in str(e):
                    print_warning(f"[FUNC] re-login {uid_str}")
                    break
                reconnect_count_total += 1
                reconnects += 1
                if reconnects > max_reconnects:
                    reconnects = 0
                    await asyncio.sleep(3)
                    continue
                await asyncio.sleep(bypass_human_reconnect_delay(reconnects))
            finally:
                _team_active["flag"] = False
                for tk in (team_coord_task, member_trigger_task):
                    if tk and not tk.done():
                        tk.cancel()
                        try: await tk
                        except (asyncio.CancelledError, Exception): pass
                if gateway_ping_task: gateway_ping_task.cancel()
                if writer:
                    try: writer.close(); await writer.wait_closed()
                    except Exception: pass
                async with ts["lock"]:
                    ts["peers"].pop(uid_str, None)
                    if ts["leader_uid"] == uid_str:
                        ts["leader_uid"] = min(ts["peers"].keys()) if ts["peers"] else None
                        if ts["leader_uid"]:
                            print_info(f"[TEAM] new leader: {ts['leader_uid']}")
                await _team_member_leave(uid_str, team_key)
                _team_active["flag"] = True
    except asyncio.CancelledError:
        for m in play_matches:
            if not m.done(): m.cancel()
        if play_matches: await asyncio.gather(*play_matches, return_exceptions=True)
        play_matches.clear()
        raise

# =========================== INFORMATIONAL ===========================
async def informational(addrs, starter_packet, key, iv, region="ID", max_reconnects=3):
    reconnects = 0
    ip, port = addrs.split(":")
    while True:
        writer = None; ping_task = None
        try:
            rip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(rip, int(port))
            rs = writer.get_extra_info('socket')
            if rs: optimize_tcp_socket(rs)
            writer.write(bytes.fromhex(starter_packet)); await writer.drain()
            reconnects = 0
            try:
                ka = await send_keep_alive(region)
                if ka and not writer.is_closing():
                    writer.write(ka); await asyncio.wait_for(writer.drain(), timeout=3)
            except Exception: pass
            async def info_ka():
                ka = await send_keep_alive(region)
                while True:
                    await asyncio.sleep(bypass_ka_jitter(3.0))
                    try:
                        if writer and not writer.is_closing():
                            writer.write(ka); await writer.drain()
                    except Exception: break
            ping_task = asyncio.create_task(info_ka())
            while True:
                data = await reader.read(8192)
                if not data: raise ConnectionError("closed")
        except asyncio.CancelledError:
            if ping_task: ping_task.cancel()
            if writer:
                try: writer.close(); await writer.wait_closed()
                except Exception: pass
            raise
        except Exception:
            if ping_task: ping_task.cancel()
            if writer:
                try: writer.close(); await writer.wait_closed()
                except Exception: pass
            reconnects += 1
            if reconnects > max_reconnects:
                await asyncio.sleep(3); reconnects = 0
            else: await asyncio.sleep(1)

# =========================== ACCOUNT HELPERS ===========================
def _register_credentials(ad):
    try:
        acc_id = str(ad['account_id'])
        bot_state.account_credentials[acc_id] = ad
        if ad.get('auth_uid'): bot_state.account_credentials[str(ad['auth_uid'])] = ad
        if ad.get('auth_token'): bot_state.account_credentials[f"tok_{ad['auth_token'][:20]}"] = ad
    except Exception: pass

async def refresh_account_profile(ad_or_uid):
    try:
        if isinstance(ad_or_uid, str):
            uid = str(ad_or_uid); ad = bot_state.account_credentials.get(uid)
        else:
            ad = ad_or_uid; uid = str(ad.get('account_id')) if ad else "?"
        if not ad: return
        url = ad.get('server_url'); token = ad.get('token')
        rel = ad.get('release_version'); payload = ad.get('login_payload_data')
        if not (url and token and rel and payload): return
        res = await send_getlogin(payload, url, token, rel)
        if not res: return
        rp, dd = res
        def _si(v, d):
            try:
                if v is None or v == "": return d
                return int(v)
            except Exception: return d
        level = _si(get_proto_field(dd, 6, None), None)
        exp = _si(get_proto_field(dd, 7, None), None)
        likes = _si(get_proto_field(dd, 8, None), None)
        try: nick = rp.nickname or get_proto_field(dd, 4, "") or ""
        except Exception: nick = ""
        if level is None and exp is None: return
        level = level if level is not None else 1
        exp = exp if exp is not None else 0
        likes = likes if likes is not None else 0
        acc_id = str(ad['account_id'])
        old_lv = 0; old_ex = 0
        try:
            if acc_id in bot_state.accounts:
                old_lv = int(bot_state.accounts[acc_id].get('level', 0) or 0)
                old_ex = int(bot_state.accounts[acc_id].get('exp', 0) or 0)
        except Exception: pass
        try: bot_state.update_exp(acc_id, exp, level)
        except Exception: pass
        if likes > 0 and acc_id in bot_state.accounts:
            bot_state.accounts[acc_id]["likes"] = likes
        if nick and acc_id in bot_state.accounts:
            bot_state.accounts[acc_id]["nickname"] = nick
        _update_cache_profile(ad, level, exp)
        ad['level'] = level; ad['exp'] = exp
        if likes > 0: ad['likes'] = likes
        if nick: ad['nickname'] = nick
        pass
    except Exception: pass

async def process_account_uid_pass(uid, password):
    cached = cache_get(uid)
    if cached:
        print_success(f"[CACHE] {uid} loaded")
        acc_id = str(cached['account_id'])
        bot_state.register_account(uid=acc_id,
            nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'ID'),
            level=cached.get('level', 1), exp=cached.get('exp', 0),
            likes=cached.get('likes', 0))
        _register_credentials(cached); return cached
    print_info(f"[LOGIN] UID {uid}...")
    try:
        vc = await version_config()
        if not vc: return None
        rel, cv, su = vc
        tg = await get_access_token(uid, password)
        if not tg: return None
        oid, at, pf = tg
        dev = get_device_for_account(uid)
        lpd = await build_majorlogin_payload(oid, at, pf, cv, dev)
        if not lpd: return None
        mresp = await send_majorlogin(lpd, rel, su)
        if mresp is None: return None
        gl = await send_getlogin(lpd, mresp.url, mresp.token, rel)
        if gl is None: return None
        rp, dd = gl
        acc_id = str(mresp.account_id)
        level = int(get_proto_field(dd, 6, 1) or 1)
        exp = int(get_proto_field(dd, 7, 0) or 0)
        likes = int(get_proto_field(dd, 8, 0) or 0)
        nick = rp.nickname or get_proto_field(dd, 4, f"Player_{acc_id}")
        region = mresp.region or get_proto_field(dd, 3, "ID")
        bot_state.register_account(uid=acc_id, nickname=nick, region=region,
                                    level=level, exp=exp, likes=likes)
        print_success(f"[OK] {acc_id} | {nick} | Lvl{level} | BATTLE ROYALE")
        ad = {
            'account_id': mresp.account_id, 'nickname': nick, 'region': region,
            'level': level, 'exp': exp, 'likes': likes,
            'open_id': oid, 'access_token': at, 'platform': str(pf),
            'token': mresp.token, 'server_time': mresp.server_time,
            'aes_ak': mresp.aes_ak, 'iv_i': mresp.iv_i,
            'functional_addrs': rp.functional_addrs or get_proto_field(dd, 14),
            'informational_addrs': rp.informational_addrs or get_proto_field(dd, 32),
            'release_version': rel, 'client_version': cv, 'server_url': mresp.url,
            'login_host': str(su),
            'login_payload_data': lpd,
            'auth_type': 'guest', 'auth_uid': uid, 'auth_password': password,
        }
        _register_credentials(ad); cache_set(uid, ad); return ad
    except Exception as e:
        print_error(f"process_account_uid_pass: {e}")
        traceback.print_exc(); return None

async def process_account_token(access_token):
    cache_key = f"tok_{access_token[:20]}"
    cached = cache_get(cache_key)
    if cached:
        acc_id = str(cached['account_id'])
        bot_state.register_account(uid=acc_id,
            nickname=cached.get('nickname', f"Player_{acc_id}"),
            region=cached.get('region', 'ID'),
            level=cached.get('level', 1), exp=cached.get('exp', 0),
            likes=cached.get('likes', 0))
        _register_credentials(cached); return cached
    print_info("[LOGIN] Token...")
    try:
        vc = await version_config()
        if not vc: return None
        rel, cv, su = vc
        import requests
        url = f"https://100067.connect.garena.com/oauth/token/inspect?token={access_token}"
        hdrs = {"User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 9;en;US;)"}
        resp = await asyncio.to_thread(requests.get, url, headers=hdrs, timeout=10)
        d = resp.json()
        if 'error' in d: return None
        oid = d.get('open_id'); pf = d.get('platform', 4)
        if not oid: return None
        dev = get_device_for_account(oid)
        lpd = await build_majorlogin_payload(oid, access_token, str(pf), cv, dev)
        if not lpd: return None
        mresp = await send_majorlogin(lpd, rel, su)
        if mresp is None: return None
        gl = await send_getlogin(lpd, mresp.url, mresp.token, rel)
        if gl is None: return None
        rp, dd = gl
        acc_id = str(mresp.account_id)
        level = int(get_proto_field(dd, 6, 1) or 1)
        exp = int(get_proto_field(dd, 7, 0) or 0)
        likes = int(get_proto_field(dd, 8, 0) or 0)
        nick = rp.nickname or get_proto_field(dd, 4, f"Player_{acc_id}")
        region = mresp.region or get_proto_field(dd, 3, "ID")
        bot_state.register_account(uid=acc_id, nickname=nick, region=region,
                                    level=level, exp=exp, likes=likes)
        ad = {
            'account_id': mresp.account_id, 'nickname': nick, 'region': region,
            'level': level, 'exp': exp, 'likes': likes,
            'open_id': oid, 'access_token': access_token, 'platform': str(pf),
            'token': mresp.token, 'server_time': mresp.server_time,
            'aes_ak': mresp.aes_ak, 'iv_i': mresp.iv_i,
            'functional_addrs': rp.functional_addrs or get_proto_field(dd, 14),
            'informational_addrs': rp.informational_addrs or get_proto_field(dd, 32),
            'release_version': rel, 'client_version': cv, 'server_url': mresp.url,
            'login_payload_data': lpd,
            'auth_type': 'token', 'auth_token': access_token,
        }
        _register_credentials(ad); cache_set(cache_key, ad); return ad
    except Exception as e:
        print_error(f"process_account_token: {e}"); return None

async def run_account_worker(ad, label):
    acc_id = str(ad['account_id'])
    info_task = None; exp_task = None; ping_task = None
    auto_join_clan = bool(_cfg(ad, "auto_join_clan", AUTO_JOIN_CLAN))
    clan_id = str(_cfg(ad, "clan_id", CLAN_ID) or "")
    user_game_uid = str(_cfg(ad, "captain_uid", USER_GAME_UID) or "")
    try:
        reg = ad.get('region', 'ID')
        tcp_online = await build_tcp_startup_packet(
            ad['account_id'], ad['token'], ad['server_time'],
            ad['aes_ak'], ad['iv_i'], region=reg, typ='OnLine')
        tcp_chat = await build_tcp_startup_packet(
            ad['account_id'], ad['token'], ad['server_time'],
            ad['aes_ak'], ad['iv_i'], region=reg, typ='ChaT')

        cap_task = None
        if auto_join_clan and clan_id:
            #  dedup: pehle se clan mein ho toh request mat bhejo
            _auid = str(ad.get('auth_uid') or acc_id)
            _done = set()
            if os.path.exists(CLAN_STATE_FILE):
                try:
                    with open(CLAN_STATE_FILE, "r", encoding="utf-8") as _cf:
                        _done = set(json.load(_cf))
                except Exception:
                    _done = set()
            if _auid in _done:
                print_info(f"[CLAN] {_auid} already in clan (saved) - skip")
            else:
                _okj = False
                for _ct in range(2):
                    try:
                        await asyncio.sleep(1.0 if _ct == 0 else 15.0)
                        _okj, _errj = await join_clan(ad['token'], clan_id)
                        if _okj: break
                        if "ALREADY_IN" in str(_errj).upper():
                            print_info(f"[CLAN] {_auid} already in a clan - done")
                            _okj = True; break
                    except Exception as _ce:
                        print_warning(f"[CLAN] {_ce}")
                if _okj:
                    _done.add(_auid)
                    try:
                        with open(CLAN_STATE_FILE, "w", encoding="utf-8") as _cf:
                            json.dump(sorted(_done), _cf)
                    except Exception:
                        pass

        if user_game_uid:
            #  captain ki friend request har 20s auto-accept (invite tabhi deliver hota hai)
            async def _cap_friend_loop():
                while True:
                    await asyncio.sleep(20)
                    try:
                        _url = f"{ad['server_url'].rstrip('/')}/ConfirmFriendRequest"
                        _pl = bytes.fromhex(encrypt_api(create_proto({"1": int(user_game_uid), "2": int(acc_id)}).hex()))
                        _hd = {
                            "authorization": f"Bearer {ad['token']}",
                            "host": ad['server_url'].replace("https://", "").split("/")[0],
                            "x-ga": "v1 1", "content-type": "application/x-www-form-urlencoded",
                            "accept-encoding": "deflate, gzip",
                            "releaseversion": str(ad['release_version']),
                            "user-agent": "UnityPlayer/2022.3.47f1", "accept": "*/*",
                            "x-unity-version": "2022.3.47f1",
                        }
                        await client.post(_url, headers=_hd, content=_pl, timeout=8.0)
                    except Exception:
                        pass
            cap_task = asyncio.create_task(_cap_friend_loop())
        info_task = asyncio.create_task(informational(ad['informational_addrs'], tcp_chat,
                                                      ad['aes_ak'], ad['iv_i'], region=reg))
        if ENABLE_HTTP_PING:
            _phost = str(ad.get('login_host') or PING_URL).rstrip('/')
            _purl = _phost if _phost.endswith('/Ping') else (_phost + '/Ping')
            ping_task = asyncio.create_task(ping_worker(acc_id, ad['token'], ad['release_version'], region=reg, ping_url=_purl))
            print_success(f"[PING] Started {acc_id}")

        async def exp_refresher():
            try:
                f = bot_state.account_credentials.get(acc_id)
                if f: await refresh_account_profile(f)
            except Exception: pass
            while True:
                await asyncio.sleep(EXP_REFRESH_INTERVAL + random.uniform(-3.0, 3.0))
                try:
                    f = bot_state.account_credentials.get(acc_id)
                    if f: await refresh_account_profile(f)
                except Exception: pass
        exp_task = asyncio.create_task(exp_refresher())
        #  Always Battle Royale
        await functional_battle_royale(ad['functional_addrs'], tcp_online, ad['region'], ad['client_version'],
            ad['aes_ak'], ad['iv_i'], account_id=acc_id, account_data=ad,
            account_level=int(ad.get('level', 2) or 2))
    except asyncio.CancelledError: raise
    except Exception as e: print_error(f"run_account_worker {label}: {e}")
    finally:
        for t in (info_task, exp_task, ping_task, cap_task):
            if t and not t.done(): t.cancel()
        for t in (info_task, exp_task, ping_task, cap_task):
            if t:
                try: await t
                except Exception: pass

async def account_loop_guest(uid, password):
    while True:
        try:
            print_info(f"[LOGIN] Guest {uid}...")
            try: bot_state.update_status(str(uid), "CONNECTING")
            except Exception: pass
            ad = await process_account_uid_pass(uid, password)
            if not ad:
                print_error(f"Login failed {uid}, retry 15s")
                try: bot_state.update_status(str(uid), "ERROR")
                except Exception: pass
                await asyncio.sleep(15); continue
            await run_account_worker(ad, uid)
            print_warning(f"Session end {uid}, reconnect 3s")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Worker {uid} stopped")
            try: bot_state.update_status(str(uid), "OFFLINE")
            except Exception: pass
            break
        except Exception as e:
            print_error(f"Error {uid}: {e}, retry 10s")
            await asyncio.sleep(10)

async def account_loop_token(token):
    label = token[:10]
    while True:
        try:
            ad = await process_account_token(token)
            if not ad:
                print_error("Login token fail, retry 15s")
                await asyncio.sleep(15); continue
            acc_id = str(ad['account_id'])
            await run_account_worker(ad, acc_id)
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            print_warning(f"Token {label} stopped"); break
        except Exception as e:
            print_error(f"Token error: {e}, retry 10s")
            await asyncio.sleep(10)


# =========================== CLIENT ORCHESTRATION ===========================
# Replaces the old stdin-driven main(). A "client" here = one escort/server:
# a squad of TEAM_BOTS bot accounts (+ optional real captain UID) running
# together under one team_key so the dashboard can group them as one card.

def load_accounts():
    accounts = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    accounts = data
        except Exception as e:
            print_error(f"load: {e}")
    return accounts


def _unused_account_slice(n: int, exclude_uids: set) -> List[Dict[str, Any]]:
    pool = [a for a in load_accounts() if str(a.get("uid")) not in exclude_uids]
    return pool[:n]


async def ensure_bot_accounts(count: int, exclude_uids: set, name_prefix: str = "FYT_") -> List[Dict[str, Any]]:
    """Reuse existing saved accounts first; generate the shortfall."""
    global GEN_NAME_PREFIX
    have = _unused_account_slice(count, exclude_uids)
    need = count - len(have)
    if need > 0:
        if name_prefix:
            GEN_NAME_PREFIX = name_prefix if name_prefix.endswith("_") else name_prefix + "_"
        print_info(f"[GEN] need {need} more bot account(s), generating {GEN_NAME_PREFIX}xxxx")
        await auto_generate_accounts(need)
        have = _unused_account_slice(count, exclude_uids)
    return have


class ClientRuntime:
    """Runtime handle for one running client (escort). Tracks its worker
    tasks so the dashboard can start/stop/restart the whole squad."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.tasks: Dict[str, asyncio.Task] = {}   # keyed by account uid
        self.bot_uids: List[str] = []
        self.captain_uid: Optional[str] = None

    def is_running(self) -> bool:
        return any(not t.done() for t in self.tasks.values())

    async def stop(self):
        for uid, t in list(self.tasks.items()):
            if not t.done():
                t.cancel()
            bot_state.account_workers.pop(uid, None)
        for t in list(self.tasks.values()):
            try:
                await t
            except Exception:
                pass
        self.tasks.clear()
        for uid in self.bot_uids:
            bot_state.update_status(uid, "OFFLINE")
        _drop_team(self.client_id)


_client_runtimes: Dict[str, ClientRuntime] = {}


def get_runtime(client_id: str) -> ClientRuntime:
    if client_id not in _client_runtimes:
        _client_runtimes[client_id] = ClientRuntime(client_id)
    return _client_runtimes[client_id]


async def _client_account_loop(uid: str, password: str, client_id: str, slot: int, role: str,
                                team_bots: int, captain_uid: str, clan_id: str, auto_join_clan: bool):
    """Same reconnect loop as account_loop_guest, but tags every account
    registration with client_id/slot so the dashboard can place it on
    the right escort card in the right seat, AND stamps this client's squad
    config onto `ad` so functional_battle_royale/run_account_worker resolve
    team_key + team_bots/captain_uid/clan_id from account_data (_cfg) instead
    of the shared globals — required for 2+ clients to run concurrently
    without racing, and for team_key to ever resolve to anything other than
    the bot's own uid (its no-peers-ever fallback)."""
    while True:
        try:
            bot_state.update_status(uid, "CONNECTING")
            ad = await process_account_uid_pass(uid, password)
            if not ad:
                bot_state.update_status(uid, "ERROR")
                print_error(f"Login failed {uid}, retry 15s", )
                await asyncio.sleep(15)
                continue
            # stamp per-client squad config — this is the actual fix: without
            # client_id here, team_key in functional_battle_royale falls back
            # to this bot's own uid, so every bot forms a "squad" of itself
            # and _team_coordinator never sees peers, never invites, never starts.
            ad['client_id'] = client_id
            ad['team_bots'] = team_bots
            ad['captain_uid'] = captain_uid
            ad['clan_id'] = clan_id
            ad['auto_join_clan'] = auto_join_clan
            acc_id = str(ad['account_id'])
            bot_state.register_account(uid=acc_id, nickname=ad.get('nickname', ''),
                                        region=ad.get('region', 'ID'), level=ad.get('level', 1),
                                        exp=ad.get('exp', 0), likes=ad.get('likes', 0),
                                        client_id=client_id, slot=slot, role=role)
            bot_state.update_status(acc_id, "ONLINE")
            await run_account_worker(ad, uid)
            bot_state.update_status(uid, "OFFLINE")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            bot_state.update_status(uid, "OFFLINE")
            raise
        except Exception as e:
            print_error(f"Error {uid} ({client_id}): {e}, retry 10s")
            bot_state.update_status(uid, "ERROR")
            await asyncio.sleep(10)


async def start_client(client: Dict[str, Any]) -> ClientRuntime:
    """client dict shape (see app/core/clients.py):
    { id, name, mode: 'with_me'|'normal', captain_uid, clan_id, name_prefix, bot_count }
    mode 'with_me'  -> TEAM_BOTS = bot_count (default 3), captain joins via friend+invite, USER_GAME_UID set
    mode 'normal'   -> TEAM_BOTS = bot_count (default 4), fully self-contained squad
    """
    client_id = client["id"]
    runtime = get_runtime(client_id)
    if runtime.is_running():
        return runtime

    mode = client.get("mode", "normal")
    bot_count = int(client.get("bot_count") or (3 if mode == "with_me" else 4))
    clan_id = (client.get("clan_id") or "").strip()
    captain_uid = (client.get("captain_uid") or "").strip() if mode == "with_me" else ""
    auto_join_clan = bool(clan_id)
    name_prefix = client.get("name_prefix") or "FYT_"

    # squad config is passed straight into _client_account_loop and stamped
    # onto each bot's `ad` dict (client_id/team_bots/captain_uid/clan_id/
    # auto_join_clan) instead of mutated on shared module globals — N clients
    # now run concurrently without racing each other's TEAM_BOTS/USER_GAME_UID.

    exclude = set()
    for rt in _client_runtimes.values():
        exclude.update(rt.bot_uids)

    accs = await ensure_bot_accounts(bot_count, exclude, name_prefix)
    if len(accs) < bot_count:
        bot_state.log(f"Only {len(accs)}/{bot_count} bot accounts available for client {client_id}", "warning")

    runtime.bot_uids = [str(a["uid"]) for a in accs]
    runtime.captain_uid = captain_uid or None

    for slot, acc in enumerate(accs, start=1):
        uid, pwd = str(acc["uid"]), str(acc["password"])
        task = asyncio.create_task(_client_account_loop(
            uid, pwd, client_id, slot, "bot",
            bot_count, captain_uid, clan_id, auto_join_clan))
        runtime.tasks[uid] = task
        bot_state.account_workers[uid] = task

    bot_state.log(f"Client '{client.get('name', client_id)}' started — {len(accs)} bot(s), mode={mode}",
                   "success", client_id=client_id)
    return runtime


async def stop_client(client_id: str):
    if client_id in _client_runtimes:
        await _client_runtimes[client_id].stop()
        bot_state.log(f"Client {client_id} stopped", "warning", client_id=client_id)


async def restart_client(client: Dict[str, Any]):
    await stop_client(client["id"])
    await asyncio.sleep(1)
    return await start_client(client)


def client_live_slots(client_id: str) -> List[Dict[str, Any]]:
    """Per-slot status for the detail view: filling / standing / in-match."""
    accs = sorted(bot_state.accounts_for_client(client_id), key=lambda a: a.get("slot") or 99)
    out = []
    for a in accs:
        status = a.get("status", "CONNECTING")
        match_state = a.get("match_state", "IDLE")
        if status != "ONLINE":
            ui_state = "connecting" if status == "CONNECTING" else "error"
        elif match_state in ("SEARCHING", "QUEUED"):
            ui_state = "filling"
        elif match_state in ("IN_MATCH", "PLAYING"):
            ui_state = "in_match"
        else:
            ui_state = "standing"
        out.append({**a, "ui_state": ui_state})
    return out
