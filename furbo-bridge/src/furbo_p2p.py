#!/usr/bin/env python3
"""Furbo 360 over TUTK P2P: live video, device state and controls.

The protocol is what the Furbo Android app speaks, taken from the decompiled
app in apk/decompiled. Sequence:

  1. TUTK_SDK_Set_License_Key (the app's key), IOTC_Initialize2 on a random
     UDP port, avInitialize. The app never sets a region.
  2. IOTC_Connect_ByUIDEx with the device's P2PUuid and its 8 character
     AuthKey (authentication_type 0).
  3. avClientStartEx with P2PAccountId / P2PAccountKey, security_mode 1
     (DTLS, PSK-AES128-CBC-SHA256). P2PAccountKey is reissued by the cloud on
     every /v5/device/p2p_connection/get call, so it is fetched just before.
  4. Furbo's own IOCTRL opcodes (0x900 range) for state and control, the
     standard 0x1FF to start video with a quality number, 0x300 for audio.

A legacy camera (an FB002) is given none of those credentials: the cloud
answers AuthKey, P2PAccountId and P2PAccountKey as null, and the camera
authenticates from its own device record instead -- IOTC_Connect_ByUID_Parallel
with no auth key, then avClientStartEx with the account id and the device's
P2PAccessToken, in the clear (security_mode 0, auth_type 1). Steps 1 and 4 are
the same either way. Which path is taken follows from the missing AuthKey, not
from the model, so a camera the cloud issues credentials for cannot reach it.
Contributed with working hardware values in #3; see p2p_auth().

Needs a TUTK SDK 4.x shared library for this machine. The one that
docker-wyze-bridge ships (app/lib/lib.amd64) works: FURBO_TUTK_LIB or --lib.

Usage:
    furbo_p2p.py login                  # emails the MFA code, saves furbo_session.json
    furbo_p2p.py status                 # cloud: devices, alerts, subscription, events
    furbo_p2p.py p2p-status             # camera over P2P: power, volume, night mode, ...
    furbo_p2p.py p2p-set --camera on --volume 5 --night auto --toss
    furbo_p2p.py stream > out.h264      # or | ffplay -f h264 -
    furbo_p2p.py stream | ffmpeg -f h264 -i - -c copy -f rtsp rtsp://127.0.0.1:8554/furbo
    furbo_p2p.py serve --token secret   # HTTP API for the Home Assistant integration
    furbo_p2p.py talk < audio.ulaw      # send G.711 mu-law 16k mono to the speaker
"""

from __future__ import annotations

import argparse
import asyncio
from ctypes import (
    CDLL,
    CFUNCTYPE,
    POINTER,
    RTLD_GLOBAL,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_int8,
    c_int32,
    c_uint,
    c_uint8,
    c_uint16,
    c_uint32,
    c_void_p,
    create_string_buffer,
    sizeof,
)
import datetime as dt
import json
import os
from pathlib import Path
import random
import struct
import sys
import threading
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from furbo_cloud import (
    FurboClient,
    FurboError,
    FurboLoginError,
    FurboMfaError,
    configure_logging,
    encrypt_password,
    new_mobile_id,
)

SESSION_FILE = Path(os.environ.get("FURBO_SESSION_FILE", "furbo_session.json"))

# com.tomofun.furbo.device.p2p.a: TUTKGlobalAPIs.TUTK_SDK_Set_License_Key(...)
FURBO_LICENSE_KEY = b"AQAAAImKg/eLaZN7zGTUrAl9TqgL27EnRkTZm6iu5GvTVNWh7OW4gmiNUfkGqX9ooTOoSogLX7Cnp+bovyklByEGrbr3QS9f+XeoMknVSXd3p1ZxNDQIqXfE4CtzDVEdrFWQ7akXnuUHcySIy2KGhXjQdITBl2y3hJITKVi30pPAbxGuaiYCI/o9KFwtfgQS+8lhtkJGY4MmO9CXoFsUwpZZifAb"

# Standard TUTK opcodes the app uses (com.tutk.IOTC.AVIOCTRLDEFs).
IPCAM_START = 0x1FF  # payload: int32 LE quality number
IPCAM_STOP = 0x2FF
# The video opcodes are not in the CMD tables, so without this they log as bare
# hex and a camera that answers (or does not) is hard to read in a log.
VIDEO_NAME = {IPCAM_START: "IPCAM_START", IPCAM_STOP: "IPCAM_STOP"}
AUDIO_START = 0x300
AUDIO_STOP = 0x301
SETSTREAMCTRL_REQ = 0x320
SETSTREAMCTRL_RESP = 0x321
GETAUDIOOUTFORMAT_REQ = 0x32A
GETAUDIOOUTFORMAT_RESP = 0x32B

# Furbo opcodes (FurboP2PCmdImplTutkV2.CUSTOM_P2P_COMMAND_*). Replies are req + 1
# and start with a status byte, 0 meaning success, unless noted.
CMD = {
    "SET_VIDEO_QUALITY": 2307,
    "SET_NIGHT_VISION": 2309,  # [mode] 0 auto, 1 on, 2 off
    "GET_NIGHT_VISION": 2311,  # reply [0, mode]
    "SET_BARKING": 2313,  # [sensitivity] 1 low, 2 medium, 3 high
    "GET_BARKING": 2315,  # reply [sensitivity, status]
    "SET_VOLUME": 2317,  # [volume]
    "GET_VOLUME": 2319,  # reply [0, volume, muted]
    "TOSS": 2321,
    "GET_NEW_IMAGE": 2325,  # firmware versions, reply is 4 ascii fields of 8
    "UPDATE_FIRMWARE": 2329,
    "SET_FURBO_POWER": 2331,  # [on]
    "GET_FURBO_POWER": 2333,  # reply [0, on]
    "SET_TIMEZONE": 2335,
    "GET_TIMEZONE": 2337,
    "SET_SNACKCALL": 2339,  # nul terminated url
    "GET_SNACKCALL": 2341,  # reply [0, type] 0 default, 1 custom, 2 mute
    "REBOOT": 2343,
    "GET_DEVICEINFO": 2345,  # [force]; reply is JSON
    "SET_DEVICE_TOKEN": 2347,
    "GET_AVINFO": 2353,  # encoder stats
    "IPCAM_START_RESP": 2355,  # camera's answer to 0x1FF
    "NOTIFY_SERVICE_STATE": 2357,
    "GET_SERVICE_STATE": 2359,
    "SET_SCHEDULE": 2367,  # JSON
    "GET_SCHEDULE": 2369,  # [1]; reply is JSON
    "NOTIFY_CLEAR_BUFF": 2371,  # unsolicited, camera asks us to flush
    "PLAY_TREAT_SOUND": 2373,
    "GET_AUTO_CALM": 2375,
}
CMD_NAME = {v: k for k, v in CMD.items()}

# Furbo 360 / 360 CAT / Mini 2 / Mini 3 (FurboP2pCmdImplTutkV3) speak a
# different opcode set in the 0x10000 (get/set), 0x20000 (actions) and 0x30000
# (notify) ranges. Replies are req + 1. The Furbo 360 (FB0030) is a V3 device.
CMD3 = {
    "GET_DEVICE_INFO": 65537,  # reply is a status byte then a JSON blob
    "SET_DEVICE_TOKEN": 65539,
    "GET_TIMEZONE": 65541,
    "SET_TIMEZONE": 65543,
    "GET_CAMERA_ON": 65545,  # reply [status, on]
    "SET_CAMERA_ON": 65547,
    "GET_CAMERA_SCHEDULE": 65549,
    "SET_CAMERA_SCHEDULE": 65551,
    "GET_UPGRADE_INFO": 65553,  # firmware versions
    "GET_NIGHT_VISION": 65555,  # reply [status, mode]
    "SET_NIGHT_VISION": 65557,
    "GET_BARKING": 65559,  # audio detection sensitivity
    "SET_BARKING": 65561,
    "GET_VOLUME": 65563,  # reply [status, volume, muted]
    "SET_VOLUME": 65565,
    "GET_SNACKCALL": 65567,  # treat tossing sound
    "SET_SNACKCALL": 65569,
    "GET_TOSS_PROFILE": 65571,
    "SET_TOSS_PROFILE": 65573,
    "GET_AUTO_TRACKING": 65575,
    "SET_AUTO_TRACKING": 65577,
    "GET_AUTO_ZOOM": 65579,
    "SET_AUTO_ZOOM": 65581,
    "GET_AUTO_CALM": 65584,
    "SET_AUTO_CALM": 65586,
    "GET_VOICE_CONTROL": 65590,
    "SET_VOICE_CONTROL": 65592,
    "GET_TREAT_TROUBLESHOOT": 65596,
    "UPGRADE_FIRMWARE": 131073,
    "REBOOT": 131075,
    "UPLOAD_LOGS": 131077,
    "TOSS": 131079,
    "PAN": 131081,
    "RESET_DEVICE": 131083,
    "PLAY_TREAT_SOUND": 131085,
    "STOP_AUTO_ZOOM": 131091,
    "NOTIFY_AV_STATUS": 196609,
    "NOTIFY_CAMERA_ON_OFF": 196611,
    "NOTIFY_TREAT_TOSSING": 196613,
    "NOTIFY_PAN": 196615,
    "NOTIFY_TREAT_TROUBLESHOOT": 196617,
}
CMD3_NAME = {v: k for k, v in CMD3.items()}

# ProductId -> which command implementation the app uses. FurboType ordinals
# 1..6 (FB0030, FB0035, FBC0030, FBC0035, MC0020, MC0030) use V3; 7..10
# (FB001, FB002, FB0025, MC0010) use V2 (com.tomofun.furbo...device.p2p.a).
PROTO_BY_PRODUCT = {
    "FB0030": "v3",
    "FB0035": "v3",
    "FBC0030": "v3",
    "FBC0035": "v3",
    "MC0020": "v3",
    "MC0030": "v3",
    "FB001": "v2",
    "FB002": "v2",
    "FB0025": "v2",
    "MC0010": "v2",
}
# The camera answers a request it will not serve with this opcode and a
# payload of [status, opcode_lo, opcode_hi, 0, 0]. The app logs and ignores it.
CMD_REJECTED = 0x40001
# How long one V3 get waits for its reply before the sweep moves on. A reply
# that is recognised ends the wait as soon as it lands; this is what an
# unrecognised one costs, and it is deliberately no more than the fixed sleep
# this replaced, so a matching bug cannot make the sweep slower than it was.
REPLY_TIMEOUT = 0.4
# A short catch-all after the sweep, for anything the camera sent unprompted.
TRAILING_DRAIN = 0.2
NIGHT_MODES = {0: "auto", 1: "on", 2: "off"}
SENSITIVITY = {1: "low", 2: "medium", 3: "high"}
SENSITIVITY_V3 = {0: "off", 1: "low", 2: "medium", 3: "high"}  # SoundSensitivity
SNACK_CALL = {0: "default", 1: "custom", 2: "mute"}
# IPCAM_START (0x1FF) payload is a little-endian quality number. The mapping
# is getVideoResolutionNum in the app and differs by protocol.
QUALITY = {"1080p": 1, "720p": 2, "360p": 3, "1440p": 0}  # V2
BARK_V3 = {"off": 0, "low": 1, "medium": 2, "high": 3}  # SoundSensitivity
TREAT_SIZE = {"large": 0, "small": 1}  # TreatSizeType
TREAT_SIZE_NAME = {v: k for k, v in TREAT_SIZE.items()}
PAN_DIR = {"left": 1, "right": 2}  # setRotate direction
QUALITY_V3 = {"1080p": 0, "720p": 1, "360p": 2, "1440p": 3}  # V3

AV_ER_DATA_NOREADY = -20012
AV_ER_INCOMPLETE_FRAME = -20013
AV_ER_LOSED_THIS_FRAME = -20014

ERRORS = {
    -6: "IOTC_ER_FAIL_CREATE_SOCKET",
    -10: "IOTC_ER_UNLICENSE",
    -13: "IOTC_ER_TIMEOUT",
    -19: "IOTC_ER_CAN_NOT_FIND_DEVICE",
    -22: "IOTC_ER_SESSION_CLOSE_BY_REMOTE",
    -23: "IOTC_ER_REMOTE_TIMEOUT_DISCONNECT",
    -24: "IOTC_ER_DEVICE_NOT_LISTENING (camera offline)",
    -40: "IOTC_ER_NO_PERMISSION",
    -41: "IOTC_ER_NETWORK_UNREACHABLE",
    -42: "IOTC_ER_FAIL_SETUP_RELAY",
    -46: "IOTC_ER_INVALID_ARG",
    -90: "IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY",
    -1002: "TUTK_ER_INVALID_ARG",
    -1004: "TUTK_ER_INVALID_LICENSE_KEY",
    -20009: "AV_ER_WRONG_VIEWACCorPWD",
    -20011: "AV_ER_TIMEOUT",
    -20015: "AV_ER_SESSION_CLOSE_BY_REMOTE",
    -20016: "AV_ER_REMOTE_TIMEOUT_DISCONNECT",
    -20018: "AV_ER_SERVNOTSUPPORT_SECURITY",
    -20040: "AV_ER_DTLS_WRONG_PWD",
    -20041: "AV_ER_DTLS_AUTH_FAIL",
}


def err(code: int) -> str:
    return f"{code} {ERRORS.get(code, '')}".strip()


def log(*args) -> None:
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


# --- ctypes structures (TUTK SDK 4.x) ---------------------------------------


class St_IOTCConnectInput(Structure):
    _fields_ = [
        ("cb", c_uint32),
        ("authentication_type", c_uint),
        ("auth_key", c_char * 8),
        ("timeout", c_uint32),
    ]


class St_SInfoEx(Structure):
    _fields_ = [
        ("size", c_uint32),
        ("mode", c_uint8),
        ("c_or_d", c_int8),
        ("uid", c_char * 21),
        ("remote_ip", c_char * 47),
        ("remote_port", c_uint16),
        ("tx_packet_count", c_uint32),
        ("rx_packet_count", c_uint32),
        ("iotc_version", c_uint32),
        ("vendor_id", c_uint16),
        ("product_id", c_uint16),
        ("group_id", c_uint16),
        ("is_secure", c_uint8),
        ("local_nat_type", c_uint8),
        ("remote_nat_type", c_uint8),
        ("relay_type", c_uint8),
        ("net_state", c_uint32),
        ("remote_wan_ip", c_char * 47),
        ("remote_wan_port", c_uint16),
        ("is_nebula", c_uint8),
    ]


class AVClientStartInConfig(Structure):
    # Layout of TUTK 4.2 as used by docker-wyze-bridge. The app's build adds
    # dtls_cipher_suites at the end; the library reads cb to know which.
    _fields_ = [
        ("cb", c_uint32),
        ("iotc_session_id", c_uint32),
        ("iotc_channel_id", c_uint8),
        ("timeout_sec", c_uint32),
        ("account_or_identity", c_char_p),
        ("password_or_token", c_char_p),
        ("resend", c_int32),
        ("security_mode", c_uint32),
        ("auth_type", c_uint32),
        ("sync_recv_data", c_int32),
    ]


class AVClientStartOutConfig(Structure):
    _fields_ = [
        ("cb", c_uint32),
        ("server_type", c_uint32),
        ("resend", c_int32),
        ("two_way_streaming", c_int32),
        ("sync_recv_data", c_int32),
        ("security_mode", c_uint32),
    ]


# --- talk-back (audio server) ctypes -----------------------------------------
# avServStartEx's St_AVServStartInConfig, inferred from the app's Java struct
# and the client config layout docker-wyze-bridge documents (cb first, then the
# session/channel/timeout/type/resend/security ints, the seven auth callbacks,
# and the DTLS cipher suites string). The SDK reads cb to know the size.
_PW_AUTH = CFUNCTYPE(c_int, c_char_p, POINTER(c_char_p))
_TOKEN_AUTH = CFUNCTYPE(c_int, c_char_p, POINTER(c_char_p))
_TOKEN_REQUEST = CFUNCTYPE(c_int, c_int, c_char_p, c_char_p, POINTER(c_char_p))
_TOKEN_DELETE = CFUNCTYPE(c_int, c_int, c_char_p)
_IDENTITY_ARRAY = CFUNCTYPE(None, c_int, c_void_p, c_void_p)
_ABILITY = CFUNCTYPE(None, c_int, c_void_p)
_CHANGE_PW = CFUNCTYPE(c_int, c_int, c_char_p, c_char_p, c_char_p, c_char_p)


# Offsets verified by disassembling avServStartEx in the TUTK 4.2 library:
# it reads session at 0x4, channel (byte) at 0x8, ints at 0xc/0x10/0x14/0x18,
# and six callback pointers at 0x30-0x58 (48-88); the config size (cb) must be
# > 0x5f. So 20 reserved bytes sit between security_mode and the callbacks.
class AVServStartInConfig(Structure):
    _fields_ = [
        ("cb", c_uint32),  # 0
        ("iotc_session_id", c_uint32),  # 4
        ("iotc_channel_id", c_uint8),  # 8
        ("_pad0", c_uint8 * 3),  # 9-11
        ("timeout_sec", c_uint32),  # 12
        ("server_type", c_uint32),  # 16
        ("resend", c_int32),  # 20
        ("security_mode", c_uint32),  # 24
        ("_reserved", c_uint8 * 20),  # 28-47
        ("password_auth", _PW_AUTH),  # 48
        ("token_auth", _TOKEN_AUTH),  # 56
        ("token_request", _TOKEN_REQUEST),  # 64
        ("token_delete", _TOKEN_DELETE),  # 72
        ("identity_array_request", _IDENTITY_ARRAY),  # 80
        ("ability_request", _ABILITY),  # 88
        ("change_password_request", _CHANGE_PW),  # 96
        ("json_request", c_void_p),  # 104
        ("dtls_cipher_suites", c_char_p),  # 112
    ]


# avServStartEx requires the out config's cb to be > 0x10f (271 bytes), so pad
# it well past that; only the first fields are written back.
class AVServStartOutConfig(Structure):
    _fields_ = [
        ("cb", c_uint32),
        ("server_type", c_uint32),
        ("resend", c_int32),
        ("two_way_streaming", c_int32),
        ("security_mode", c_uint32),
        ("_reserved", c_uint8 * 300),
    ]


def _accept_all_serv_callbacks() -> dict:
    """Auth callbacks that accept unconditionally (return 0), like the app."""
    return {
        "password_auth": _PW_AUTH(lambda account, password: 0),
        "token_auth": _TOKEN_AUTH(lambda identity, token: 0),
        "token_request": _TOKEN_REQUEST(lambda i, ident, desc, token: 0),
        "token_delete": _TOKEN_DELETE(lambda i, ident: 1),
        "identity_array_request": _IDENTITY_ARRAY(lambda i, arr, status: None),
        "ability_request": _ABILITY(lambda i, ability: None),
        "change_password_request": _CHANGE_PW(lambda i, a, o, n, k: 1),
    }


class FrameInfo(Structure):
    _fields_ = [
        ("codec_id", c_uint16),
        ("is_keyframe", c_uint8),
        ("cam_index", c_uint8),
        ("online_num", c_uint8),
        ("framerate", c_uint8),
        ("frame_size", c_uint8),
        ("bitrate", c_uint8),
        ("timestamp_ms", c_uint32),
        ("timestamp", c_uint32),
        ("frame_len", c_uint32),
        ("frame_no", c_uint32),
        ("ac_mac_addr", c_char * 12),
        ("n_play_token", c_int32),
    ]


# The TUTK library is initialised once per process, not once per session: a
# bridge serving several cameras holds a session each, and the initialise and
# deinitialise calls are global to the library.
_SDK_LOCK = threading.Lock()
_sdk_ready = False
# The library the process initialised, kept so it can be shut down without a
# session to reach it through.
_sdk_lib: Any = None


def deinitialize_sdk() -> None:
    """Shut the SDK down. For process exit, after every session is closed."""
    global _sdk_ready, _sdk_lib
    with _SDK_LOCK:
        if not _sdk_ready or _sdk_lib is None:
            return
        _sdk_lib.avDeInitialize()
        _sdk_lib.IOTC_DeInitialize()
        _sdk_ready = False
        _sdk_lib = None
        log("SDK shut down")


# --- cloud side -------------------------------------------------------------


def _load_session() -> dict:
    if not SESSION_FILE.exists():
        raise SystemExit("No session. Run: furbo_p2p.py login")
    return json.loads(SESSION_FILE.read_text())


def _device_file() -> Path:
    """Where the device identity lives.

    Deliberately not the session file: 'reset_session' deletes that, and
    clearing a dead token should not also change who this bridge claims to be.
    """
    return SESSION_FILE.with_name("furbo_device.json")


def _stored_mobile_id() -> str | None:
    """The device id this bridge last logged in with, if there is one.

    Furbo treats MobileId as the identity of the client. Minting a new one per
    login makes every login look like a new phone, which is what makes the
    cloud email a verification code every time, so it is kept and reused. The
    session file is read as a fallback for bridges that stored it there before
    it had a file of its own.
    """
    for path in (_device_file(), SESSION_FILE):
        try:
            value = json.loads(path.read_text()).get("mobile_id")
        except (OSError, ValueError):
            continue
        if isinstance(value, str) and value:
            return value
    return None


def _remember_mobile_id(mobile_id: str) -> None:
    """Record the device id where a session reset will not remove it."""
    try:
        _device_file().write_text(json.dumps({"mobile_id": mobile_id}, indent=2))
    except OSError as exc:  # a read-only data dir should not fail a login
        log(f"could not save the device id: {exc}")


class MfaRequired(Exception):
    """The cloud wants an emailed code, which only a person can supply."""


class LoginRequired(Exception):
    """Only a person can get this bridge signed in again.

    Kept apart from every other login failure: a client told this should ask
    someone to sign in, where one told the cloud could not be reached should
    simply come back later.
    """


def _cloud_fail(exc: FurboError) -> SystemExit:
    if exc.code == 12002:
        return SystemExit(f"Cloud rejected the token ({exc}). Run: furbo_p2p.py login")
    return SystemExit(f"Cloud call failed: {exc}")


PENDING_FILE = SESSION_FILE.with_name(SESSION_FILE.stem + ".pending.json")


def _env_file() -> dict:
    """FURBO_EMAIL / FURBO_PASSWORD from a .env in the working directory or next to this script."""
    out = {}
    for env in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out.setdefault(k.strip(), v.strip().strip("'\""))
    return out


async def cloud_login(code: str | None = None, send_only: bool = False) -> dict:
    """Log in. With MFA on the account this is two steps: the first call emails
    a code and (in --send-only mode) parks the login in a pending file, the
    second call passes --code. Interactive use asks for the code on stdin."""
    import aiohttp

    env = _env_file()
    email = (
        os.environ.get("FURBO_EMAIL") or env.get("FURBO_EMAIL") or input("Furbo email: ").strip()
    )
    password = (
        os.environ.get("FURBO_PASSWORD")
        or env.get("FURBO_PASSWORD")
        or input("Furbo password: ").strip()
    )
    async with aiohttp.ClientSession() as http:
        client = FurboClient(http)
        if code and PENDING_FILE.exists():
            pending = json.loads(PENDING_FILE.read_text())
            enc, mobile_id, candidate = pending["enc"], pending["mobile_id"], pending["candidate"]
        else:
            enc = encrypt_password(password)
            mobile_id = _stored_mobile_id() or new_mobile_id()
            candidate = await client.start_login(email, enc, mobile_id)
            if candidate:
                candidate = await client.send_mfa_code(candidate)
                if send_only:
                    PENDING_FILE.write_text(
                        json.dumps({"enc": enc, "mobile_id": mobile_id, "candidate": candidate})
                    )
                    log(
                        f"verification code emailed to {email}; finish with: furbo_p2p.py login --code NNNN"
                    )
                    return {}
                code = code or input(f"Verification code emailed to {email}: ").strip()
        if candidate:
            await client.complete_login(email, enc, mobile_id, candidate, code)
        devices = await client.get_devices()
    if PENDING_FILE.exists():
        PENDING_FILE.unlink()
    _remember_mobile_id(mobile_id)
    session = {
        "account_id": client.account_id,
        "cognito_token": client.cognito_token,
        "mobile_id": mobile_id,
        "devices": devices,
    }
    SESSION_FILE.write_text(json.dumps(session, indent=2))
    log(f"logged in, {len(devices)} device(s), session saved to {SESSION_FILE}")
    return session


async def cloud_status(days: int) -> None:
    """Print what the cloud knows: account, cameras, alerts, subscription, events."""
    import aiohttp

    session = _load_session()
    today = dt.date.today()
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(days)]
    async with aiohttp.ClientSession() as http:
        client = FurboClient(http, session["account_id"], session["cognito_token"])
        try:
            devices = await client.get_devices()
            licenses = (await client.get_license()).get("DevicesLicense", {})
            pets = await client.get_pet_profiles()
            alerts = {d["Id"]: await client.get_alert_settings(d["Id"]) for d in devices}
            activity = await client.get_activity_report(dates)
            events = {}
            for i, d in enumerate(dates):
                if i:  # same endpoint again within ~10 s gets rate limited
                    await asyncio.sleep(12)
                events[d] = await client.get_notable_events(d)
        except FurboError as exc:
            raise _cloud_fail(exc) from exc
    session["devices"] = devices
    SESSION_FILE.write_text(json.dumps(session, indent=2))

    for pet in pets:
        print(f"Pet: {pet.get('Name')} ({pet.get('Species')}, {pet.get('Breed')})")
    for d in devices:
        print(
            f"\n{d['DeviceName']}  id={d['Id']}  model={d['ProductId']}  firmware={d['FirmwareVersion']}"
            f"  status={d['DeviceStatus']}  p2p={d['P2PVendor']} uid={d['P2PUuid']}"
        )
        for lic in licenses.get(d["Id"], []):
            print(
                f"  subscription: {lic['ServicePlanName']} {lic['SubscriptionStatus']},"
                f" {lic['TimeLeftDays']} days left, renews {lic['NextBillingAttempt']}"
            )
        a = alerts[d["Id"]]
        on = sorted(k for k, v in a.items() if not k.startswith("Frequency:") and v == "1")
        off = sorted(k for k, v in a.items() if not k.startswith("Frequency:") and v == "0")
        print("  alerts on:  " + ", ".join(on))
        print("  alerts off: " + ", ".join(off))

    print("\nActivity (hourly counts summed per day):")
    for date in dates:
        data = activity.get(date, {}).get("Data") or {}
        print(f"  {date}: " + ", ".join(f"{k}={sum(v)}" for k, v in sorted(data.items())))

    print("\nNotable events:")
    for date in dates:
        for e in events[date]:
            print(
                f"  {e['LocalTime']}  {e.get('Caption') or e.get('ActionCaption')}  ({e['DeviceId']})"
            )
        if not events[date]:
            print(f"  {date}: none")


# Every login goes through this, so two of them never run at once. Each camera
# reconnects in a thread of its own and the HTTP API runs in the event loop, so
# a dead token is noticed in several places at the same moment, and a burst of
# logins is what the cloud's rate limiter (80002) answers with a lockout.
_LOGIN_LOCK = threading.Lock()
# Long enough for the login in front to finish (three cloud calls, each capped
# at 20 seconds) and short enough that a caller is told to come back rather
# than held forever.
_LOGIN_WAIT = 65.0


async def silent_relogin(stale_token: str | None = None) -> dict:
    """Log in again with the stored device id, keeping the same session file.

    Pass the token that was refused to skip the login when someone else has
    already replaced it: the caller then gets the session that is now stored.

    Raises MfaRequired when the cloud wants an emailed code, which nothing here
    can answer, and SystemExit when there are no credentials to try.
    """
    if not _LOGIN_LOCK.acquire(timeout=_LOGIN_WAIT):
        raise SystemExit("a login is already running; try again shortly")
    try:
        if stale_token is not None:
            current = _session_or_none()
            token = (current or {}).get("cognito_token")
            if current is not None and token and token != stale_token:
                log("another login has already refreshed the session")
                return current
        return await _login_again()
    finally:
        _LOGIN_LOCK.release()


def _session_or_none() -> dict | None:
    """The stored session, or None when there is not one to read."""
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return None


async def _login_again() -> dict:
    """Log in with the stored credentials. Callers hold ``_LOGIN_LOCK``."""
    import aiohttp

    env = _env_file()
    email = os.environ.get("FURBO_EMAIL") or env.get("FURBO_EMAIL")
    password = os.environ.get("FURBO_PASSWORD") or env.get("FURBO_PASSWORD")
    if not email or not password:
        raise SystemExit("no stored credentials to log in with")
    mobile_id = _stored_mobile_id() or new_mobile_id()
    async with aiohttp.ClientSession() as http:
        client = FurboClient(http)
        candidate = await client.start_login(email, encrypt_password(password), mobile_id)
        if candidate:
            raise MfaRequired("the cloud wants a verification code")
        devices = await client.get_devices()
    _remember_mobile_id(mobile_id)
    session = {
        "account_id": client.account_id,
        "cognito_token": client.cognito_token,
        "mobile_id": mobile_id,
        "devices": devices,
    }
    SESSION_FILE.write_text(json.dumps(session, indent=2))
    log(f"logged in again, {len(devices)} device(s)")
    return session


async def relogin_or_explain(stale_token: str) -> dict:
    """Log in again, turning every way it can fail into an answerable one.

    Both callers -- a camera reconnecting and the cloud-token endpoint -- need
    the same three outcomes, and they used to spell them out separately: the
    camera path caught only MfaRequired, so a refused login escaped it raw,
    reached the HTTP layer as a server error with a traceback, and left the
    worker with no reason to hold off. It then tried again on the next frame
    request, which is how an account gets rate-limited out.
    """
    try:
        return await silent_relogin(stale_token)
    except MfaRequired as mfa:
        raise LoginRequired(
            f"Cloud rejected the token and {mfa}. Set a fresh mfa_code and "
            "reset_session in the add-on options, then restart it."
        ) from None
    except (FurboLoginError, FurboMfaError) as refused:
        raise LoginRequired(
            f"the cloud refused the stored credentials ({refused}). Check the "
            "add-on's email and password, then set a fresh mfa_code with "
            "reset_session on."
        ) from None
    except FurboError as exc:
        # Could not reach the cloud, or the cloud faltered. Worth retrying.
        raise _cloud_fail(exc) from exc


async def current_credentials() -> dict[str, str]:
    """The account id and a cloud token that the cloud has just accepted.

    The cloud issues no refresh token and the one it does issue is short
    lived, so a client that cannot log in for itself (the integration does not
    store the account password, deliberately) can take a current one from here
    instead of asking a person for a code every day.

    The stored token is checked rather than trusted. Nothing else renews it on
    a schedule: a camera whose P2P session stays up does not call the cloud
    for days, so the token in the session file can be as dead as the caller's
    own by the time it is asked for.
    """
    import aiohttp

    session = _load_session()
    account_id = session.get("account_id")
    token = session.get("cognito_token")
    if not account_id or not token:
        raise SystemExit("no cloud session yet")
    async with aiohttp.ClientSession() as http:
        client = FurboClient(http, str(account_id), str(token))
        try:
            await client.get_account_info()
        except FurboError as exc:
            if exc.code != 12002:
                raise _cloud_fail(exc) from exc
            log("cloud rejected the stored token; logging in again")
            session = await relogin_or_explain(str(token))
    return {
        "account_id": str(session["account_id"]),
        "cognito_token": str(session["cognito_token"]),
    }


def refreshed_credentials() -> dict[str, str]:
    """Blocking form of :func:`current_credentials`, to run in a thread.

    The renewal it may do takes the process-wide login lock, which the camera
    threads hold across their own logins, so it must not run on the event loop.
    """
    return asyncio.run(current_credentials())


def session_devices() -> list[dict[str, str]]:
    """The cameras the stored session knows about, without calling the cloud.

    Used at startup to decide which cameras to serve, so a bridge can bring up
    one session per camera before anything asks for video.
    """
    devices = _load_session().get("devices") or []
    return [
        {
            "device_id": str(d["Id"]),
            "name": d.get("DeviceName") or str(d["Id"]),
            "product": d.get("ProductId") or "",
        }
        for d in devices
        if d.get("Id") is not None
    ]


MODERN_P2P_KEYS = ("AuthKey", "P2PAccountId", "P2PAccountKey")


def p2p_auth(device: dict, p2p: dict, account_id: str) -> tuple[str, str, str, bool]:
    """Return (auth_key, account, password, legacy) for one camera.

    The cloud answers in one of two shapes, and only two are accepted:

    All three of AuthKey, P2PAccountId and P2PAccountKey, which is a modern
    camera, connected with its auth key and DTLS.

    None of them, which is a legacy camera -- an FB002, reported in #3. It
    authenticates from its own device record instead: the account id, and the
    device's P2PAccessToken, in the clear.

    Anything in between is refused rather than guessed at. Half a modern
    response is not a legacy camera, and treating it as one would quietly move
    a camera that should be using DTLS onto the unencrypted path. It is also
    not usable as a modern one, and pressing on there turns a null into the
    literal string "None" and fails to authenticate for no visible reason.

    Which path is taken follows from what the cloud sent, never from the
    product id, so a camera that is issued real credentials cannot reach the
    legacy path however it is named.
    """
    have = {k: p2p.get(k) for k in MODERN_P2P_KEYS}
    usable = {k: v for k, v in have.items() if isinstance(v, str) and v}
    name = device.get("DeviceName") or "this camera"

    if len(usable) == len(MODERN_P2P_KEYS):
        return usable["AuthKey"], usable["P2PAccountId"], usable["P2PAccountKey"], False

    if usable:
        absent = ", ".join(k for k in MODERN_P2P_KEYS if k not in usable)
        raise SystemExit(
            f"the cloud sent {name} only part of its P2P credentials "
            f"(no {absent}); refusing to guess which kind of camera this is"
        )

    token = device.get("P2PAccessToken")
    if not isinstance(token, str) or not token:
        raise SystemExit(
            f"{name} was given no P2P credentials by the cloud and has no "
            "P2PAccessToken to fall back on"
        )
    return "", account_id, token, True


async def fetch_p2p_credentials(device_id: str | None) -> dict:
    """Uid, auth key and a fresh P2P password for one device.

    With more than one camera on the account a device_id must be given, so the
    bridge never silently serves the wrong camera; the resolved cloud device id
    is returned as ``device_id`` for the integration to verify.
    """
    import aiohttp

    session = _load_session()
    devices = session["devices"]
    if device_id:
        device = next((d for d in devices if str(d["Id"]) == str(device_id)), None)
        if device is None:
            ids = ", ".join(str(d["Id"]) for d in devices)
            raise SystemExit(f"device_id {device_id} not found; available: {ids}")
    elif len(devices) == 1:
        device = devices[0]
    else:
        ids = ", ".join(str(d["Id"]) for d in devices)
        raise SystemExit(
            f"this account has {len(devices)} cameras; set the 'device_id' option to one of: {ids}"
        )
    async with aiohttp.ClientSession() as http:
        client = FurboClient(http, session["account_id"], session["cognito_token"])
        try:
            p2p = await client.get_p2p_connection(device["Id"])
        except FurboError as exc:
            if exc.code != 12002:
                raise _cloud_fail(exc) from exc
            # The stored token is dead. Anything can kill it: the phone app
            # signing in, a password change, or the token simply ageing out.
            # Logging in again with the same device id usually goes through
            # without a code, so try that before giving up on a person.
            log("cloud rejected the stored token; logging in again")
            session = await relogin_or_explain(str(session["cognito_token"]))
            client = FurboClient(http, session["account_id"], session["cognito_token"])
            try:
                p2p = await client.get_p2p_connection(device["Id"])
            except FurboError as retry_exc:
                raise _cloud_fail(retry_exc) from retry_exc
    auth_key, account, password, legacy = p2p_auth(device, p2p, str(session["account_id"]))
    if legacy:
        log(f"{device['DeviceName']}: no AuthKey from the cloud; using legacy P2P auth")
    return {
        "name": device["DeviceName"],
        "device_id": str(device["Id"]),
        "uid": device["P2PUuid"],
        "auth_key": auth_key,
        "account": account,
        "password": password,
        "legacy": legacy,
        "device_token": device.get("DeviceToken") or "",
        "product": device.get("ProductId") or "",
        "proto": PROTO_BY_PRODUCT.get(device.get("ProductId") or "", "v2"),
    }


# --- TUTK side --------------------------------------------------------------


class FurboP2P:
    def __init__(
        self, lib_path: str, region: str | None, log_path: str | None, tcp_relay: bool = False
    ) -> None:
        self.lib = CDLL(lib_path, mode=RTLD_GLOBAL)
        self.tcp_relay = tcp_relay
        self.lib.IOTC_Get_Version_String.restype = c_char_p
        self.session_id = -1
        self.av_chan = -1
        self.region = region
        self.log_path = log_path
        self.proto = "v2"
        self.mode: str | None = None
        self.talk_channel = -1
        self.talk_av = -1
        self.state: dict = {}

    def initialize(self) -> None:
        """Bring the SDK up, once for the whole process.

        IOTC_Initialize2, avInitialize and their deinitialisers are
        process-wide, not per session. A bridge serving several cameras holds
        several sessions in one process, so initialising per session fails on
        the second camera, and deinitialising when one session closes would
        take the SDK out from under every other camera.
        """
        global _sdk_ready, _sdk_lib
        with _SDK_LOCK:
            if _sdk_ready:
                return
            lib = self.lib
            log("TUTK", lib.IOTC_Get_Version_String().decode())
            ret = lib.TUTK_SDK_Set_License_Key(c_char_p(FURBO_LICENSE_KEY))
            if ret < 0:
                raise SystemExit(f"license key rejected: {err(ret)}")
            if self.region:
                ret = lib.TUTK_SDK_Set_Region_Code(c_char_p(self.region.encode()))
                if ret < 0:
                    raise SystemExit(f"region {self.region!r} rejected: {err(ret)}")
            if self.log_path:
                lib.IOTC_Set_Log_Path(c_char_p(self.log_path.encode()), c_int(0))
            if self.tcp_relay:
                log("TCP relay only:", lib.IOTC_TCPRelayOnly_TurnOn())
            port = random.randint(10000, 19999)  # the app picks 10000 + now % 10000
            ret = lib.IOTC_Initialize2(c_uint16(port))
            if ret < 0:
                raise SystemExit(f"IOTC_Initialize2 failed: {err(ret)}")
            ret = lib.avInitialize(c_int(10))
            if ret < 0:
                raise SystemExit(f"avInitialize failed: {err(ret)}")
            _sdk_ready = True
            _sdk_lib = lib
            log(f"SDK initialised on udp/{port}")

    def connect(self, creds: dict, timeout: int) -> None:
        lib = self.lib
        sid = lib.IOTC_Get_SessionID()
        if sid < 0:
            raise SystemExit(f"IOTC_Get_SessionID failed: {err(sid)}")
        if creds.get("legacy"):
            # No AuthKey to present, so the connect that takes one is not an
            # option: a legacy camera is reached by the plain parallel connect.
            log(f"connecting to {creds['name']} uid={creds['uid']} (legacy, parallel)")
            ret = lib.IOTC_Connect_ByUID_Parallel(c_char_p(creds["uid"].encode()), c_int(sid))
            if ret < 0:
                lib.IOTC_Session_Close(c_int(sid))
                raise SystemExit(f"IOTC_Connect_ByUID_Parallel failed: {err(ret)}")
        else:
            cin = St_IOTCConnectInput()
            cin.cb = sizeof(cin)
            cin.authentication_type = 0
            cin.auth_key = creds["auth_key"].encode()
            cin.timeout = timeout
            log(f"connecting to {creds['name']} uid={creds['uid']} (timeout {timeout}s)")
            ret = lib.IOTC_Connect_ByUIDEx(c_char_p(creds["uid"].encode()), c_int(sid), byref(cin))
            if ret < 0:
                lib.IOTC_Session_Close(c_int(sid))
                raise SystemExit(f"IOTC_Connect_ByUIDEx failed: {err(ret)}")
        self.session_id = ret
        info = St_SInfoEx()
        info.size = sizeof(info)
        if lib.IOTC_Session_Check_Ex(c_int(self.session_id), byref(info)) >= 0:
            mode = {0: "P2P", 1: "relay", 2: "LAN"}.get(info.mode, info.mode)
            # Kept so the HTTP bridge can report the path in /api/status: a
            # relayed session is the difference between working and not.
            self.mode = str(mode)
            log(
                f"session {self.session_id}: {mode} via {info.remote_ip.decode()}:{info.remote_port}"
                f" secure={info.is_secure} nat={info.local_nat_type}/{info.remote_nat_type}"
            )

    def start_av(self, creds: dict, timeout: int = 15) -> None:
        lib = self.lib
        cin = AVClientStartInConfig()
        cin.cb = sizeof(cin)
        cin.iotc_session_id = self.session_id
        cin.iotc_channel_id = 0
        cin.timeout_sec = timeout
        legacy = bool(creds.get("legacy"))
        cin.account_or_identity = creds["account"].encode()
        cin.password_or_token = creds["password"].encode()
        cin.resend = 1
        # A legacy camera authenticates in the clear with the account id and
        # the device's P2PAccessToken. auth_type 1 is an empirical result from
        # the hardware in #3, not a value read off a matching TUTK header:
        # ours are older and do not define the field at all.
        cin.security_mode = 0 if legacy else 1  # else DTLS, as the app does
        cin.auth_type = 1 if legacy else 0
        cin.sync_recv_data = 0
        cout = AVClientStartOutConfig()
        cout.cb = sizeof(cout)
        ret = lib.avClientStartEx(byref(cin), byref(cout))
        if ret < 0:
            raise SystemExit(f"avClientStartEx failed: {err(ret)}")
        self.av_chan = ret
        log(
            f"AV channel {ret}: server_type={cout.server_type} resend={cout.resend}"
            f" two_way={cout.two_way_streaming} security_mode={cout.security_mode}"
        )
        lib.avClientSetMaxBufSize(c_uint(10 * 1024 * 1024))
        lib.avClientSetRecvBufMaxSize(c_int(self.av_chan), c_uint(10 * 1024 * 1024))

    # --- control channel ---

    def _name(self, opcode: int) -> str:
        return (
            CMD3_NAME.get(opcode)
            or CMD_NAME.get(opcode)
            or VIDEO_NAME.get(opcode)
            or f"0x{opcode:x}"
        )

    def send(self, opcode: int, data: bytes = b"\0\0\0\0") -> None:
        name = self._name(opcode)
        # Clear anything already queued BEFORE transmitting, never after.
        # avSendIOCtrl blocks long enough for the camera to answer, so a drain
        # placed after it consumes this command's own reply: the reply is
        # decoded into state, but a caller waiting for it never sees it and
        # sits out its timeout instead.
        while self.poll(0) is not None:
            pass
        ret = self.lib.avSendIOCtrl(c_int(self.av_chan), c_uint(opcode), data, c_int(len(data)))
        if ret < 0:
            log(f"send {name} failed: {err(ret)}")
        else:
            log(f"sent {name} {data[:32].hex()}")

    def poll(self, timeout_ms: int = 10) -> tuple[int, bytes] | None:
        opcode = c_uint()
        buf = create_string_buffer(4096)
        ret = self.lib.avRecvIOCtrl(
            c_int(self.av_chan), byref(opcode), buf, c_int(4096), c_uint(timeout_ms)
        )
        if ret < 0:
            return None
        data = buf.raw[:ret]
        self.decode(opcode.value, data)
        return opcode.value, data

    def decode(self, opcode: int, data: bytes) -> None:
        if self.proto == "v3":
            return self.decode_v3(opcode, data)
        return self.decode_v2(opcode, data)

    def decode_v3(self, opcode: int, data: bytes) -> None:
        """V3 replies (FurboP2pCmdImplTutkV3). The get/device-info reply is a
        status byte then a JSON blob; the simple gets are [status, value...].
        The full handler is not in the decompiled app, so a few offsets are
        confirmed empirically against the live camera and logged raw."""
        name = CMD3_NAME.get(opcode - 1) or CMD3_NAME.get(opcode) or VIDEO_NAME.get(opcode)
        name = name or CMD_NAME.get(opcode) or f"0x{opcode:x}"
        s = self.state
        ok = bool(data) and data[0] == 0
        if opcode == CMD3["GET_DEVICE_INFO"] + 1 and ok:
            # Binary struct: [0]=status, [1]=CAM, [5]=VOL, then four nul padded
            # ascii version fields (VER, NEW_VER, LIB, NEW_LIB), then flags and
            # a double hex encoded SSID. The dedicated gets are authoritative;
            # this fills in what only the struct carries.
            s["camera_on"] = data[1] == 1
            if len(data) > 5:
                s["volume"] = data[5]
            vers = _ascii_fields(data[9:], 4)
            if any(vers):
                s.setdefault("firmware", {}).update(
                    {"current": vers[0], "new": vers[1], "lib": vers[2], "new_lib": vers[3]}
                )
            ssid = _decode_hex_ascii(data)
            if ssid:
                s["wifi_ssid"] = ssid
        elif opcode == CMD3["GET_CAMERA_ON"] + 1 and len(data) > 1:
            s["camera_on"] = data[1] == 1
        elif opcode == CMD3["GET_VOLUME"] + 1 and len(data) > 1:
            s["volume"] = data[1]
            if len(data) > 2:
                s["muted"] = data[2] == 1
        elif opcode == CMD3["GET_NIGHT_VISION"] + 1 and len(data) > 1:
            s["night_mode"] = NIGHT_MODES.get(data[1], data[1])
        elif opcode == CMD3["GET_BARKING"] + 1 and len(data) > 1:
            s["bark_sensitivity"] = SENSITIVITY_V3.get(data[1], data[1])
        elif opcode == CMD3["GET_SNACKCALL"] + 1 and len(data) > 1:
            s["snack_call"] = SNACK_CALL.get(data[1], data[1])
        elif opcode == CMD3["GET_CAMERA_SCHEDULE"] + 1:
            s["schedule"] = _json(data[1:] if ok else data)
        elif opcode == CMD3["GET_AUTO_CALM"] + 1:
            s["auto_calm"] = _json(data[1:] if ok else data)
        elif opcode == CMD3["GET_UPGRADE_INFO"] + 1 and ok:
            vers = _ascii_fields(data[2:], 4)
            s.setdefault("firmware", {}).update(
                {"new": vers[0], "current": vers[1], "new_lib": vers[2], "lib": vers[3]}
            )
        elif opcode == CMD3["GET_AUTO_TRACKING"] + 1 and len(data) > 3:
            s["auto_tracking"] = {"cruise": data[1] == 1, "live": data[2] == 1}
        elif opcode == CMD3["GET_AUTO_ZOOM"] + 1 and len(data) > 2:
            s["auto_zoom"] = {"cruise": data[1] == 1, "live": data[2] == 1}
        elif opcode == CMD3["GET_VOICE_CONTROL"] + 1 and len(data) > 1:
            s["voice_control"] = data[1] == 1
        elif opcode == CMD3["GET_TOSS_PROFILE"] + 1 and len(data) > 1:
            s["treat_size"] = TREAT_SIZE_NAME.get(data[1], data[1])
        elif opcode == CMD["IPCAM_START_RESP"]:
            s["video_started"] = ok
        elif opcode == CMD3["NOTIFY_AV_STATUS"]:
            # The camera's own account of the video stream, and the only thing
            # that says why it is sending nothing. Small, and JSON, so it is
            # logged whole rather than truncated like the binary payloads.
            status = _json(data)
            s["av_status"] = status
            log(f"camera av status: {status}")
            return
        elif opcode == 0x40001 and len(data) >= 3:
            req = data[1] | (data[2] << 8)
            s.setdefault("rejected", []).append({"opcode": req, "status": data[0]})
            log(f"camera rejected 0x{req:x} with status {data[0]}")
            return
        log(f"recv {name} [0x{opcode:x}] ({len(data)} bytes) {data[:48].hex()}")

    def decode_v2(self, opcode: int, data: bytes) -> None:
        """Turn a camera reply into state, following the app's handleReceiveData."""
        name = CMD_NAME.get(opcode - 1) or CMD_NAME.get(opcode) or VIDEO_NAME.get(opcode)
        name = name or CMD_NAME.get(opcode) or f"0x{opcode:x}"
        s = self.state
        ok = bool(data) and data[0] == 0
        if opcode == CMD["GET_FURBO_POWER"] + 1 and ok:
            s["camera_on"] = data[1] == 1
        elif opcode == CMD["GET_VOLUME"] + 1 and ok:
            s["volume"] = data[1]
            s["muted"] = data[2] == 1
        elif opcode == CMD["GET_NIGHT_VISION"] + 1 and ok:
            s["night_mode"] = NIGHT_MODES.get(data[1], data[1])
        elif opcode == CMD["GET_BARKING"] + 1 and len(data) > 1 and data[1] == 0:
            s["bark_sensitivity"] = SENSITIVITY.get(data[0], data[0])
        elif opcode == CMD["GET_SNACKCALL"] + 1 and ok:
            s["snack_call"] = SNACK_CALL.get(data[1], data[1])
        elif opcode == CMD["GET_DEVICEINFO"] + 1:
            s["device_info"] = _json(data)
        elif opcode == CMD["GET_SCHEDULE"] + 1:
            s["schedule"] = _json(data)
        elif opcode == CMD["GET_NEW_IMAGE"] + 1 and len(data) >= 34:
            fields = [data[i : i + 8].decode(errors="ignore").strip("\0 ") for i in (2, 10, 18, 26)]
            s["firmware"] = {
                "new": fields[0],
                "current": fields[1],
                "new_lib": fields[2],
                "lib": fields[3],
            }
        elif opcode == CMD["GET_AVINFO"] + 1 and ok and len(data) >= 14:
            s["av_info"] = {
                "first_iframe_sent": data[1] == 1,
                "fps_1080p": data[2],
                "fps_720p": data[3],
                "fps_360p": data[4],
                "resend_buffer_pct": data[5],
                "send_fps": data[6],
                "lost_count": (data[7] << 8) | data[8],
                "send_kbps": (data[10] << 8) | data[11],
                "max_cpu_pct": data[12],
                "max_free_mem": data[13],
            }
        elif opcode == CMD["IPCAM_START_RESP"]:
            s["video_started"] = ok
        elif opcode == CMD["NOTIFY_CLEAR_BUFF"]:
            log("camera asked us to clear buffers")
        elif opcode == CMD_REJECTED and len(data) >= 3:
            req = data[1] | (data[2] << 8)
            s.setdefault("rejected", []).append(
                {"opcode": req, "name": CMD_NAME.get(req, hex(req)), "status": data[0]}
            )
            log(f"camera rejected {CMD_NAME.get(req, hex(req))} with status {data[0]}")
            return
        elif opcode in (
            CMD["TOSS"] + 1,
            CMD["PLAY_TREAT_SOUND"] + 1,
            CMD["SET_FURBO_POWER"] + 1,
            CMD["SET_VOLUME"] + 1,
            CMD["SET_NIGHT_VISION"] + 1,
            CMD["SET_BARKING"] + 1,
        ):
            s.setdefault("results", {})[name] = "ok" if ok else f"error {data[0] if data else '?'}"
        log(f"recv {name} [0x{opcode:x}] ({len(data)} bytes) {data[:32].hex()}")

    def register(self, creds: dict) -> None:
        """What the app does right after the channel opens. On V2 devices it
        hands the camera the account's DeviceToken; V3's setDeviceToken is a
        no-op, so nothing is sent there."""
        if self.proto == "v2" and creds.get("device_token"):
            self.send(CMD["SET_DEVICE_TOKEN"], creds["device_token"].encode() + b"\0")
            self.drain(1.0)

    def query_state(self, wait: float = 3.0) -> dict:
        """Read every setting the camera exposes.

        On V3 each command is awaited individually, so the sweep costs one
        round trip per setting rather than a fixed wait per setting; `wait`
        applies to the V2 path, which fires all its gets before waiting once.
        """
        if self.proto == "v3":
            z = b"\0\0\0\0"
            for op in (
                "GET_DEVICE_INFO",
                "GET_CAMERA_ON",
                "GET_VOLUME",
                "GET_NIGHT_VISION",
                "GET_BARKING",
                "GET_SNACKCALL",
                "GET_CAMERA_SCHEDULE",
                "GET_UPGRADE_INFO",
                "GET_AUTO_TRACKING",
                "GET_AUTO_ZOOM",
                "GET_VOICE_CONTROL",
                "GET_TOSS_PROFILE",
                "GET_AUTO_CALM",
            ):
                self.send(CMD3[op], z)
                if not self.await_reply(CMD3[op], REPLY_TIMEOUT):
                    log(f"no reply to {op} [0x{CMD3[op]:x}] within {REPLY_TIMEOUT}s")
            # Every command above has been answered or timed out, so this is
            # only here to pick up anything the camera sent unprompted.
            self.drain(TRAILING_DRAIN)
            return self.state
        self.send(CMD["GET_DEVICEINFO"], b"\1\0\0\0")
        self.send(CMD["GET_FURBO_POWER"])
        self.send(CMD["GET_VOLUME"], b"\1\0\0\0")
        self.send(CMD["GET_NIGHT_VISION"])
        self.send(CMD["GET_BARKING"])
        self.send(CMD["GET_SNACKCALL"])
        self.send(CMD["GET_SCHEDULE"], b"\1\0\0\0")
        self.send(CMD["GET_NEW_IMAGE"], b"\1\0\0\0")
        self.send(CMD["GET_AVINFO"])
        self.drain(wait)
        return self.state

    def await_reply(self, opcode: int, timeout: float) -> bool:
        """Poll until the camera answers `opcode`, or the timeout runs out.

        A reply carries either the request opcode or the request plus one; both
        are seen, which is why decode() looks up its name both ways. A refusal
        arrives as CMD_REJECTED with the low 16 bits of the request in its
        payload. Everything polled along the way is decoded as usual, so a
        reply that arrives out of order still lands in state. Returns whether
        the camera answered; a refusal counts as an answer.
        """
        end = time.time() + timeout
        seen: list[int] = []
        while time.time() < end:
            got = self.poll(100)
            if got is None:
                continue
            reply, data = got
            if reply in (opcode, opcode + 1):
                return True
            refused = reply == CMD_REJECTED and len(data) >= 3
            if refused and data[1] | (data[2] << 8) == opcode & 0xFFFF:
                return True
            seen.append(reply)
        # Naming what did arrive turns "no reply" from a dead end into a
        # diagnosis: an unexpected opcode here is a reply we failed to match.
        if seen:
            log(f"0x{opcode:x} unanswered; saw {[hex(o) for o in seen]}")
        return False

    def drain(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if self.poll(100) is None:
                time.sleep(0.02)

    # --- video ---

    def recv_frame(self, buf, info: FrameInfo):
        """Read one frame. Returns (status, size, expected).

        The frame length belongs to pnActualFrameSize. Some SDK builds also
        return it, others return 0 on success, so slicing the buffer by the
        return value alone yields empty frames on the builds that do not: the
        caller sees a successful read of nothing and a stream that carries no
        bytes while reporting no error.
        """
        out_size = c_int32()
        expected = c_int32()
        info_size = c_int32()
        frame_no = c_uint32()
        ret = self.lib.avRecvFrameData2(
            c_int(self.av_chan),
            buf,
            c_int(len(buf)),
            byref(out_size),
            byref(expected),
            byref(info),
            c_int(sizeof(info)),
            byref(info_size),
            byref(frame_no),
        )
        size = out_size.value or (ret if ret > 0 else 0)
        return ret, size, expected.value

    def alive(self) -> bool:
        """Whether the IOTC session still checks out (used by the HTTP bridge)."""
        if self.session_id < 0 or self.av_chan < 0:
            return False
        info = St_SInfoEx()
        info.size = sizeof(info)
        return self.lib.IOTC_Session_Check_Ex(c_int(self.session_id), byref(info)) >= 0

    # --- talk-back (two-way audio) ---
    # The app sends the mic to the camera as G.711 mu-law, 16 kHz, mono. It
    # opens a free IOTC channel, tells the camera to open its speaker
    # (IPCAM_SPEAKERSTART, opcode 848, payload = [channel int32 LE, 4 zero]),
    # starts an AV *server* on that channel, then pushes audio frames with
    # avSendAudioData. Each frame carries a 16-byte header: [codec_id int16 LE]
    # [flags][cam][online][7 reserved][timestamp int32 LE], where codec_id 137
    # is G711U and flags 14 = 16 kHz | 16-bit | mono.
    AUDIO_CODEC_G711U = 137
    AUDIO_FLAGS_16K_16BIT_MONO = 14
    SPEAKER_START = 848
    SPEAKER_STOP = 849

    def start_talk(self, creds: dict, timeout: int = 30) -> None:
        """Open the DTLS speaker channel so audio can be sent to the camera.

        The camera requires DTLS on the audio channel (avServStart2 without it
        is rejected with -20027), so this uses avServStartEx with security_mode
        3, matching the app. The auth callbacks accept unconditionally (return
        0), as the app's do."""
        lib = self.lib
        lib.IOTC_Session_Get_Free_Channel.restype = c_int
        ch = lib.IOTC_Session_Get_Free_Channel(c_int(self.session_id))
        if ch < 0:
            raise SystemExit(f"IOTC_Session_Get_Free_Channel failed: {err(ch)}")
        self.talk_channel = ch
        self.send(self.SPEAKER_START, struct.pack("<i", ch) + b"\0\0\0\0")

        cin = AVServStartInConfig()
        cin.cb = sizeof(cin)
        cin.iotc_session_id = self.session_id
        cin.iotc_channel_id = ch
        cin.timeout_sec = timeout
        cin.server_type = 0
        cin.resend = 0
        cin.security_mode = 3
        # Accept-all auth callbacks (return 0), like the app; kept referenced
        # so the CFUNCTYPE objects are not garbage-collected while in use.
        self._talk_cbs = _accept_all_serv_callbacks()
        cin.password_auth = self._talk_cbs["password_auth"]
        cin.token_auth = self._talk_cbs["token_auth"]
        cin.token_request = self._talk_cbs["token_request"]
        cin.token_delete = self._talk_cbs["token_delete"]
        cin.identity_array_request = self._talk_cbs["identity_array_request"]
        cin.ability_request = self._talk_cbs["ability_request"]
        cin.change_password_request = self._talk_cbs["change_password_request"]
        cin.dtls_cipher_suites = b"PSK-AES128-CBC-SHA256"
        cout = AVServStartOutConfig()
        cout.cb = sizeof(cout)
        lib.avServStartEx.restype = c_int
        av = lib.avServStartEx(byref(cin), byref(cout))
        if av < 0:
            raise SystemExit(f"avServStartEx failed: {err(av)}")
        self.talk_av = av
        # A zero resend buffer makes every frame exceed the max size (-20006);
        # give it room, as the app does when it hits that error.
        lib.avServSetResendSize(c_int(av), c_uint(512 * 1024))
        log(f"talk channel {ch}, av {av} open (DTLS, mu-law 16k mono)")

    def send_audio(self, ulaw: bytes) -> int:
        """Send one mu-law audio frame to the camera. Returns the SDK code."""
        header = struct.pack(
            "<hBBB7xI",
            self.AUDIO_CODEC_G711U,
            self.AUDIO_FLAGS_16K_16BIT_MONO,
            0,
            0,
            int(time.time() * 1000) & 0xFFFFFFFF,
        )
        return self.lib.avSendAudioData(
            c_int(self.talk_av), ulaw, c_int(len(ulaw)), header, c_int(len(header))
        )

    def stop_talk(self) -> None:
        """Close the speaker channel."""
        lib = self.lib
        if getattr(self, "talk_channel", -1) >= 0:
            self.send(self.SPEAKER_STOP, struct.pack("<i", self.talk_channel) + b"\0\0\0\0")
        if getattr(self, "talk_av", -1) >= 0:
            lib.avServStop(c_int(self.talk_av))
        if getattr(self, "talk_channel", -1) >= 0:
            lib.IOTC_Session_Channel_OFF(c_int(self.session_id), c_uint8(self.talk_channel))
        self.talk_av = -1
        self.talk_channel = -1

    def close(self) -> None:
        """Close this session only.

        Deliberately does not deinitialise the SDK: that is process-wide, and
        another camera in this process is very likely still using it. See
        deinitialize_sdk, which the process calls once on the way out.
        """
        lib = self.lib
        if getattr(self, "talk_av", -1) >= 0 or getattr(self, "talk_channel", -1) >= 0:
            self.stop_talk()
        if self.av_chan >= 0:
            lib.avSendIOCtrlExit(c_int(self.av_chan))
            lib.avClientStop(c_int(self.av_chan))
            self.av_chan = -1
        if self.session_id >= 0:
            lib.IOTC_Connect_Stop_BySID(c_int(self.session_id))
            lib.IOTC_Session_Close(c_int(self.session_id))
            self.session_id = -1
        log("closed")


def _ascii_fields(data: bytes, count: int) -> list[str]:
    """Split a run of fixed nul padded ascii fields. The camera pads each
    version field to an equal width, so infer the width from the total."""
    if not data or count <= 0:
        return [""] * count
    width = len(data) // count
    out = []
    for i in range(count):
        chunk = data[i * width : (i + 1) * width]
        out.append(chunk.split(b"\0", 1)[0].decode("ascii", "ignore").strip())
    return out


def _decode_hex_ascii(data: bytes) -> str:
    """The device info struct carries the SSID as ascii hex of ascii bytes,
    e.g. '4761726465...' -> '4761...' -> 'Garden...'. Pull the longest run."""
    import re

    text = data.decode("latin-1", "ignore")
    best = ""
    for m in re.finditer(r"[0-9a-fA-F]{8,}", text):
        raw = m.group()
        if len(raw) % 2:
            raw = raw[:-1]
        try:
            once = bytes.fromhex(raw)
            twice = (
                bytes.fromhex(once.decode("ascii")) if all(48 <= b <= 102 for b in once) else once
            )
            cand = twice.decode("ascii", "ignore").strip("\x00 ")
        except (ValueError, UnicodeDecodeError):
            continue
        if sum(c.isprintable() and c not in "\x00" for c in cand) >= 4 and len(cand) > len(best):
            best = cand
    return best


def _json(data: bytes):
    text = data.split(b"\0", 1)[0].decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def open_session(args) -> FurboP2P:
    creds = asyncio.run(fetch_p2p_credentials(args.device))
    p2p = FurboP2P(args.lib, args.region, args.tutk_log, args.tcp_relay)
    p2p.initialize()
    try:
        p2p.proto = creds.get("proto", "v2")
        p2p.connect(creds, args.timeout)
        p2p.start_av(creds)
        p2p.register(creds)
    except SystemExit:
        p2p.close()
        raise
    p2p.creds = creds
    return p2p


def cmd_talk(args) -> int:
    """Read G.711 mu-law (16 kHz, mono) from stdin and send it to the camera."""
    p2p = open_session(args)
    frames = 0
    # G.711 at 16 kHz is 16000 bytes/s, so a frame of N bytes is N/16000 s of
    # audio. Pace sending to real time so the camera plays it smoothly.
    seconds_per_frame = args.frame / 16000.0
    try:
        p2p.start_talk(p2p.creds)
        stdin = sys.stdin.buffer
        started = time.time()
        while True:
            chunk = stdin.read(args.frame)
            if not chunk:
                break
            ret = p2p.send_audio(chunk)
            frames += 1
            if frames == 1:
                log(f"first audio frame sent: {err(ret)} ({len(chunk)} bytes)")
            elif ret != 0 and frames % 50 == 0:
                log(f"avSendAudioData returned {err(ret)}")
            if args.pace:
                target = started + frames * seconds_per_frame
                delay = target - time.time()
                if delay > 0:
                    time.sleep(delay)
        log(f"talk finished, {frames} frames")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        p2p.stop_talk()
        p2p.close()


def cmd_p2p_status(args) -> int:
    p2p = open_session(args)
    try:
        state = p2p.query_state()
        print(json.dumps(state, indent=2))
    finally:
        p2p.close()
    return 0


def _set_v3(p2p, args) -> None:
    """Every writable Hallway Settings control, over V3 P2P."""
    if args.camera:
        p2p.send(CMD3["SET_CAMERA_ON"], bytes([args.camera == "on", 0, 0, 0]))
    if args.volume is not None:
        p2p.send(CMD3["SET_VOLUME"], bytes([max(0, min(100, args.volume)), 0, 0, 0]))
    if args.night:
        p2p.send(
            CMD3["SET_NIGHT_VISION"],
            bytes([{v: k for k, v in NIGHT_MODES.items()}[args.night], 0, 0, 0]),
        )
    if args.bark:
        p2p.send(CMD3["SET_BARKING"], bytes([BARK_V3[args.bark], 0, 0, 0]))
    if args.treat_size:
        p2p.send(CMD3["SET_TOSS_PROFILE"], bytes([TREAT_SIZE[args.treat_size], 0, 0, 0]))
    if args.voice:
        p2p.send(CMD3["SET_VOICE_CONTROL"], bytes([args.voice == "on", 0, 0, 0]))
    if args.tracking:
        # setAutoTracking(cruise, live, liveZoom): all three on/off together.
        on = 1 if args.tracking == "on" else 0
        p2p.send(CMD3["SET_AUTO_TRACKING"], bytes([on, on, on, 0]))
    if args.zoom:
        on = 1 if args.zoom == "on" else 0
        p2p.send(CMD3["SET_AUTO_ZOOM"], bytes([on, on, 0, 0]))
    if args.schedule is not None:
        # On/Off scheduling master switch, without touching the day grid.
        p2p.send(
            CMD3["SET_CAMERA_SCHEDULE"],
            (args.schedule and b'{"schedule_enable":1}\0') or b'{"schedule_enable":0}\0',
        )
    if args.pan:
        # setRotate(direction, degrees, 0): a relative rotation, ~60 deg per
        # press in the app (1=left, 2=right). Payload [dir, deg, 0, 0, 0, 0].
        deg = max(1, min(180, args.pan_degrees))
        p2p.send(CMD3["PAN"], bytes([PAN_DIR[args.pan], deg, 0, 0, 0, 0]))
    if args.treat_sound:
        p2p.send(CMD3["PLAY_TREAT_SOUND"])
    if args.toss:
        p2p.send(CMD3["TOSS"])


def _set_v2(p2p, args) -> None:
    if args.camera:
        p2p.send(CMD["SET_FURBO_POWER"], bytes([args.camera == "on", 0, 0, 0]))
    if args.volume is not None:
        p2p.send(CMD["SET_VOLUME"], bytes([args.volume % 256, 0, 0, 0]))
    if args.night:
        p2p.send(
            CMD["SET_NIGHT_VISION"],
            bytes([{v: k for k, v in NIGHT_MODES.items()}[args.night], 0, 0, 0]),
        )
    if args.bark:
        p2p.send(
            CMD["SET_BARKING"], bytes([{v: k for k, v in SENSITIVITY.items()}[args.bark], 0, 0, 0])
        )
    if args.treat_sound:
        p2p.send(CMD["PLAY_TREAT_SOUND"])
    if args.toss:
        p2p.send(CMD["TOSS"])


def cmd_p2p_set(args) -> int:
    p2p = open_session(args)
    try:
        if p2p.proto == "v3":
            _set_v3(p2p, args)
        else:
            _set_v2(p2p, args)
        p2p.drain(3.0)
        # Read the affected settings back so the caller sees the new state.
        state = p2p.query_state(wait=2.0)
        print(json.dumps(state, indent=2))
    finally:
        p2p.close()
    return 0


def cmd_stream(args) -> int:
    p2p = open_session(args)
    try:
        if p2p.proto == "v3":
            p2p.send(CMD3["GET_DEVICE_INFO"], b"\0\0\0\0")
            quality = QUALITY_V3[args.quality]
        else:
            p2p.send(CMD["GET_DEVICEINFO"], b"\1\0\0\0")
            quality = QUALITY[args.quality]
        p2p.drain(0.3)
        log(f"start video quality={args.quality} num={quality}")
        p2p.send(IPCAM_START, struct.pack("<i", quality))
        if args.audio:
            p2p.send(AUDIO_START, struct.pack("<i", 0))

        buf = create_string_buffer(2 * 1024 * 1024)
        info = FrameInfo()
        out = sys.stdout.buffer
        frames = 0
        started = time.time()
        last_report = started
        while True:
            p2p.poll(0)
            ret, size, expected = p2p.recv_frame(buf, info)
            if ret >= 0:
                out.write(buf.raw[:size])
                out.flush()
                frames += 1
                if frames == 1:
                    log(
                        f"first frame: codec={info.codec_id} key={info.is_keyframe} fps={info.framerate} bytes={size}"
                    )
                if time.time() - last_report > 5:
                    log(f"{frames} frames, {frames / (time.time() - started):.1f} fps")
                    last_report = time.time()
            elif ret == AV_ER_DATA_NOREADY:
                time.sleep(0.005)
            elif ret in (AV_ER_LOSED_THIS_FRAME, AV_ER_INCOMPLETE_FRAME):
                log(f"frame dropped ({err(ret)}) expected={expected}")
            else:
                log(f"avRecvFrameData2 failed: {err(ret)}")
                return 1
            if args.duration and time.time() - started > args.duration:
                log("duration reached")
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            p2p.send(IPCAM_STOP, struct.pack("<i", 0))
        except Exception:
            pass
        p2p.close()


def main() -> int:
    # Every command, not just the server. The login path is where the useful
    # debug lines are, and until now it ran at Python's default level whatever
    # the add-on's log_level said, so turning the option up did nothing for
    # the one thing somebody turns it up to see.
    configure_logging()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="log in to the Furbo cloud and save a session")
    lg.add_argument(
        "--send-only", action="store_true", help="email the MFA code and stop; finish with --code"
    )
    lg.add_argument("--code", help="the emailed verification code, to finish a --send-only login")
    st = sub.add_parser("status", help="cloud: devices, alerts, subscription, activity, events")
    st.add_argument("--days", type=int, default=2)

    def p2p_args(sp):
        sp.add_argument(
            "--lib", default=os.environ.get("FURBO_TUTK_LIB", "/usr/local/lib/libIOTCAPIs_ALL.so")
        )
        sp.add_argument(
            "--region",
            default=os.environ.get("FURBO_TUTK_REGION"),
            help="TUTK region code, normally unset like the app",
        )
        sp.add_argument(
            "--device",
            default=os.environ.get("FURBO_DEVICE") or None,
            help="cloud device id (defaults to $FURBO_DEVICE). The bridge "
            "serves every camera on the account when this is not set, and "
            "accepts a comma-separated list to serve only some of them.",
        )
        sp.add_argument("--timeout", type=int, default=10, help="connect timeout in seconds")
        sp.add_argument("--tutk-log", help="write the SDK's own debug log here")
        sp.add_argument(
            "--tcp-relay",
            action="store_true",
            help="IOTC_TCPRelayOnly_TurnOn: relay over TCP when UDP is blocked",
        )

    ps = sub.add_parser("p2p-status", help="connect over P2P and print camera state")
    p2p_args(ps)
    pset = sub.add_parser("p2p-set", help="change camera settings over P2P")
    p2p_args(pset)
    pset.add_argument("--camera", choices=["on", "off"])
    pset.add_argument("--volume", type=int, help="speaker volume 0-100")
    pset.add_argument("--night", choices=list(NIGHT_MODES.values()))
    pset.add_argument(
        "--bark", choices=["off", "low", "medium", "high"], help="barking alert sensitivity"
    )
    pset.add_argument("--treat-size", choices=list(TREAT_SIZE), help="treat size profile (V3)")
    pset.add_argument("--voice", choices=["on", "off"], help="voice control (V3)")
    pset.add_argument("--tracking", choices=["on", "off"], help="auto pet tracking (V3)")
    pset.add_argument("--zoom", choices=["on", "off"], help="auto zoom (V3)")
    pset.add_argument(
        "--schedule",
        type=lambda v: v == "on",
        choices=[True, False],
        metavar="on|off",
        help="on/off scheduling master switch (V3)",
    )
    pset.add_argument("--pan", choices=list(PAN_DIR), help="rotate the camera left or right (V3)")
    pset.add_argument(
        "--pan-degrees", type=int, default=60, help="degrees to rotate per --pan, default 60"
    )
    pset.add_argument("--treat-sound", action="store_true", help="play the treat tossing sound")
    pset.add_argument("--toss", action="store_true", help="toss a treat (dispenses a real treat)")
    s = sub.add_parser("stream", help="write the H.264 stream to stdout")
    p2p_args(s)
    s.add_argument("--quality", choices=list(QUALITY), default="720p")
    s.add_argument("--duration", type=int, default=0, help="stop after this many seconds")
    s.add_argument("--audio", action="store_true")
    tk = sub.add_parser(
        "talk", help="send G.711 mu-law 16k mono audio from stdin to the camera speaker"
    )
    p2p_args(tk)
    tk.add_argument(
        "--frame", type=int, default=320, help="mu-law bytes per frame (320 = 20ms at 16k)"
    )
    tk.add_argument(
        "--pace", action="store_true", help="pace sending to real time (for file input)"
    )
    sv = sub.add_parser(
        "serve", help="HTTP API over one long-lived P2P session (for Home Assistant)"
    )
    p2p_args(sv)
    import furbo_bridge

    furbo_bridge.add_arguments(sv)
    gc = sub.add_parser(
        "go2rtc-config", help="write the go2rtc config for the cameras this bridge serves"
    )
    gc.add_argument("--device", default=os.environ.get("FURBO_DEVICE") or None)
    gc.add_argument("--template", default="/app/go2rtc.yaml")
    gc.add_argument("--output", default="/data/go2rtc.yaml")
    args = p.parse_args()

    if args.cmd == "login":
        asyncio.run(cloud_login(args.code, args.send_only))
        return 0
    if args.cmd == "status":
        asyncio.run(cloud_status(args.days))
        return 0
    if args.cmd == "p2p-status":
        return cmd_p2p_status(args)
    if args.cmd == "p2p-set":
        return cmd_p2p_set(args)
    if args.cmd == "talk":
        return cmd_talk(args)
    if args.cmd == "go2rtc-config":
        import furbo_bridge

        return furbo_bridge.write_go2rtc_config(args)
    if args.cmd == "serve":
        try:
            asyncio.run(furbo_bridge.serve(args))
        except KeyboardInterrupt:
            pass
        return 0
    return cmd_stream(args)


if __name__ == "__main__":
    sys.exit(main())
