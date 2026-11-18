#!/usr/bin/env python3
"""Tidal Connect bridge manager.

Runs one Tidal Connect device per Music Assistant player the user switched on in the
add-on's sidebar page. For each such player it:

* starts a copy of the 32-bit Tidal Connect program, advertised under the player's name,
  whose audio goes into its own named pipe,
* streams that pipe in real time into Music Assistant, by one of two routes:
  - "airplay" (default): to the player's own AirPlay receiver in MA's AirPlay Receiver
    plugin, together with song title, artist, album and cover art. The plugin starts and
    stops the player by itself.
  - "vban": to a dedicated VBAN Receiver that the bridge creates in MA, and the bridge
    starts/stops the player through MA's API. No song info (MA can't take it for VBAN).
* acts as the Tidal program's "speaker controller" over its control socket: volume from
  the Tidal app goes to the MA player, the MA player's volume goes back to the app, and
  stopping the player in MA pauses Tidal.

Only the Python standard library is used.
"""

from __future__ import annotations

import array
import base64
import hashlib
import json
import random
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.environ.get("BRIDGE_DATA", "/data")
OPTIONS_FILE = os.path.join(DATA_DIR, "options.json")
TARGETS_FILE = os.path.join(DATA_DIR, "targets.json")
RUN_DIR = os.environ.get("BRIDGE_RUN", "/tmp/tidal-bridge")
PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")

RATE, CHANNELS, SAMPLE_BYTES = 44100, 2, 2
FRAME_BYTES = CHANNELS * SAMPLE_BYTES
FRAMES_PER_PACKET = 256
PACKET_BYTES = FRAMES_PER_PACKET * FRAME_BYTES
VBAN_SR_44100, VBAN_INT16 = 16, 0x01
PREFILL_SECONDS = 0.3
START_SILENCE_SECONDS = 1.0

WEBSOCKET_PORT_BASE = 2019  # Tidal app -> this device (open these on your firewall)
VBAN_PORT_BASE = 6990  # bridge -> Music Assistant, on 127.0.0.1 only
MAX_TARGETS = 10
CONTROL_POLL_SECONDS = 2.0
VOLUME_ECHO_GUARD_SECONDS = 3.0  # ignore MA's volume right after we set it from Tidal

AIRPLAY_FRAMES_PER_PACKET = 352
AIRPLAY_LATENCY_FRAMES = 22050 + RATE  # what classic AirPlay senders use (1.5 s)
NTP_EPOCH_OFFSET = 0x83AA7E80  # seconds from 1900 (NTP) to 1970 (Unix)
AIRPLAY_RETRY_SECONDS = 5  # reconnect after MA's AirPlay receiver restarted
RELEASE_GRACE_SECONDS = 10  # MA may take a while to start a group; don't mistake that for a stop
RESUME_AFTER_SECONDS = 8  # Tidal still playing this long after MA stopped: start MA again
PAUSE_HOLD_SECONDS = 600  # after a pause from HA, a play from HA within this time resumes Tidal
HA_WEBSOCKET = os.environ.get("BRIDGE_HA_WS", "ws://supervisor/core/websocket")
HA_MEDIA_SERVICES = {"media_next_track", "media_previous_track", "media_pause", "media_play",
                     "media_play_pause"}

ARMHF = "/armhf"
LOADER = f"{ARMHF}/lib/ld-linux-armhf.so.3"
LIBS = ":".join(
    f"{ARMHF}{p}" for p in ("/lib/arm-linux-gnueabihf", "/usr/lib/arm-linux-gnueabihf", "/lib", "/usr/lib")
)
APPDIR = f"{ARMHF}/app/ifi-tidal-release"
APP = f"{APPDIR}/bin/tidal_connect_application"
NOISE = re.compile(r"^(ALSA lib|Cannot connect to server|jack server is not running|JackShmReadWritePtr)")


def log(msg: str) -> None:
    print(f"[bridge] {msg}", flush=True)


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def lan_interface() -> str:
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        pass
    return "eth0"


def listening_ports() -> dict[int, set[str]]:
    """TCP ports the add-on's own processes listen on, with the program names (diagnostics)."""
    inodes: dict[str, int] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 9 and parts[3] == "0A":  # LISTEN
                        inodes[parts[9]] = int(parts[1].rsplit(":", 1)[1], 16)
        except OSError:
            pass
    found: dict[int, set[str]] = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        prog = next((os.path.basename(a) for a in args if "tidal_connect" in a or a.endswith(".py")),
                    os.path.basename(args[0]) if args else pid)
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link.startswith("socket:[") and link[8:-1] in inodes:
                found.setdefault(inodes[link[8:-1]], set()).add(prog)
    return found


def log_listening_ports(delay: float) -> None:
    def work() -> None:
        time.sleep(delay)
        ports = listening_ports()
        if ports:
            log("listening on TCP ports: " + ", ".join(
                f"{port} ({'/'.join(sorted(progs))})" for port, progs in sorted(ports.items())))
    threading.Thread(target=work, daemon=True, name="port-report").start()


# --------------------------------------------------------------------------- MA API
class MusicAssistant:
    """Minimal client for the Music Assistant JSON API (POST /api)."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/") + "/api"
        self.token = token

    def call(self, command: str, **args):
        body = json.dumps({"message_id": uuid.uuid4().hex, "command": command, "args": args})
        req = urllib.request.Request(
            self.url,
            data=body.encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{command}: HTTP {err.code} {detail}") from err
        except (urllib.error.URLError, OSError) as err:
            raise RuntimeError(f"{command}: cannot reach Music Assistant at {self.url} ({err})") from err
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data, dict) and "error_code" in data:
            raise RuntimeError(f"{command}: {data.get('details') or data}")
        return data

    def players(self) -> list[dict]:
        """Players and groups a person would pick (no hidden protocol sub-players)."""
        result = []
        for p in self.call("players/all") or []:
            if p.get("type") == "protocol" or p.get("hidden") or p.get("enabled") is False:
                continue
            name = str(p.get("display_name") or p.get("name") or "")
            if str(p.get("provider", "")).startswith("airplay") and "Music Assistant" in name:
                continue  # MA's own AirPlay receivers seen as speakers: picking one would loop
            result.append(
                {
                    "player_id": p.get("player_id"),
                    "name": p.get("display_name") or p.get("name") or p.get("player_id"),
                    "type": p.get("type") or "player",
                    "available": p.get("available", True),
                }
            )
        result.sort(key=lambda x: (x["type"] != "group", str(x["name"]).lower()))
        return result

    def vban_instance_ids(self) -> set[str]:
        return {p["instance_id"] for p in (self.call("providers") or []) if p.get("domain") == "vban_receiver"}

    def create_vban_receiver(self, stream_name: str, port: int) -> str:
        """Add a VBAN Receiver instance through MA's setup flow and return its instance id."""
        before = self.vban_instance_ids()
        step = self.call("config/providers/setup", provider_domain="vban_receiver") or {}
        values = {
            "bind_port": port,
            "vban_stream_name": stream_name,
            "sender_host": "127.0.0.1",
            "audio_format": "S16LE",
            "sample_rate": RATE,
            "audio_channels": CHANNELS,
            "bind_ip": "0.0.0.0",
        }
        for _ in range(4):
            stype = str(step.get("type", "")).lower()
            if stype == "form":
                step = self.call("config/flows/submit", flow_id=step["flow_id"], values=values) or {}
                if step.get("errors"):
                    raise RuntimeError(f"Music Assistant rejected the VBAN Receiver settings: {step['errors']}")
                continue
            if stype in ("progress", "working"):
                time.sleep(1)
                step = self.call("config/flows/get", flow_id=step["flow_id"]) or {}
                continue
            break
        result = step.get("result") or {}
        if result.get("instance_id"):
            return result["instance_id"]
        if str(step.get("type", "")).lower() == "abort":
            raise RuntimeError(f"Music Assistant aborted adding a VBAN Receiver: {step.get('reason')}")
        for _ in range(10):  # older MA: find the new instance in the provider list
            new = self.vban_instance_ids() - before
            if new:
                return new.pop()
            time.sleep(1)
        raise RuntimeError(f"could not create a VBAN Receiver in Music Assistant (last step: {step})")

    def airplay_receiver_id(self) -> str | None:
        for p in self.call("providers") or []:
            if p.get("domain") == "airplay_receiver":
                return p.get("instance_id")
        return None

    def airplay_connected_players(self, instance_id: str) -> list[str]:
        try:
            value = self.call("config/providers/get_value", instance_id=instance_id, key="connected_players")
        except RuntimeError:
            return []
        return [str(v) for v in value] if isinstance(value, list) else []

    def set_airplay_connected_players(self, instance_id: str, players: list[str]) -> None:
        self.call(
            "config/providers/save", provider_domain="airplay_receiver", instance_id=instance_id,
            values={"connected_players": players},
        )

    def airplay_publish_name(self, instance_id: str, player_name: str) -> str:
        try:
            template = self.call("config/providers/get_value", instance_id=instance_id, key="publish_name_template")
        except RuntimeError:
            template = None
        if template == "player":
            return player_name
        if template == "mass_player":
            return f"Music Assistant | {player_name}"
        return f"{player_name} | Music Assistant"

    def remove_provider(self, instance_id: str) -> None:
        self.call("config/providers/remove", instance_id=instance_id)


# --------------------------------------------------------------------------- AirPlay (classic RAOP) sender
def ntp_now() -> int:
    now_ns = time.time_ns()
    sec = now_ns // 1_000_000_000
    frac = ((now_ns - sec * 1_000_000_000) << 32) // 1_000_000_000
    return ((sec + NTP_EPOCH_OFFSET) << 32) | frac


def ntp_to_frames(ntp: int) -> int:
    return ((ntp >> 16) * RATE) >> 16


def frames_to_ntp(frames: int) -> int:
    return ((frames << 16) // RATE) << 16


def airplay_receiver_ports(instance_id: str, player_ids) -> dict[str, int]:
    """Same port choice as MA's AirPlay Receiver plugin (one port per connected player)."""
    ports: dict[str, int] = {}
    claimed: set[int] = set()
    for pid in sorted(set(player_ids)):
        digest = hashlib.md5(f"{instance_id}_{pid}".encode(), usedforsecurity=False).hexdigest()
        port = 7000 + int(digest, 16) % 1000
        while port in claimed:
            port = 7000 + (port - 7000 + 1) % 1000
        claimed.add(port)
        ports[pid] = port
    return ports


def browse_airplay_port(name: str) -> int | None:
    """Look the receiver up by its advertised name over mDNS (fallback if the port rule differs)."""
    try:
        out = subprocess.run(
            ["avahi-browse", "-rpt", "_raop._tcp"], capture_output=True, text=True, timeout=8
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        f = line.split(";")
        if len(f) > 8 and f[0] == "=" and unescape_avahi(f[3]).split("@", 1)[-1] == name:
            try:
                return int(f[8])
            except ValueError:
                pass
    return None


def unescape_avahi(text: str) -> str:
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), text)


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def dmap_tag(tag: str, value: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(value)) + value


class AirPlaySender:
    """Minimal classic AirPlay (RAOP) sender: 44.1 kHz 16-bit stereo ALAC (stored uncompressed), no encryption.

    Speaks just enough RTSP/RTP for shairport-sync (which MA's AirPlay Receiver runs):
    ANNOUNCE (ALAC), SETUP, RECORD, SET_PARAMETER (song info and cover art), TEARDOWN,
    answers timing requests and sends a sync packet every second.
    """

    def __init__(self, host: str, port: int, label: str) -> None:
        self.host, self.port, self.label = host, port, label
        self.lock = threading.Lock()
        self.rtsp: socket.socket | None = None
        self.cseq = 0
        self.session_id = random.getrandbits(32)
        self.dacp_id = "%016X" % random.getrandbits(64)
        self.active_remote = str(random.getrandbits(32))
        self.seq = random.getrandbits(16)
        self.start_ts = 0
        self.head_ts = 0
        self.closed = threading.Event()
        self.failed = False  # the receiver went away (e.g. MA restarted its AirPlay Receiver)
        self.socks: list[socket.socket] = []
        self.uri = ""
        self.first_packet = True

    # -- RTSP
    def _request(self, method: str, uri: str | None = None, headers: dict | None = None,
                 body: bytes = b"", content_type: str | None = None) -> tuple[int, dict]:
        assert self.rtsp
        self.cseq += 1
        lines = [f"{method} {uri or self.uri} RTSP/1.0", f"CSeq: {self.cseq}", "User-Agent: AirPlay/550.10",
                 f"DACP-ID: {self.dacp_id}", f"Active-Remote: {self.active_remote}",
                 f"Client-Instance: {self.dacp_id}"]
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        lines.append(f"Content-Length: {len(body)}")
        self.rtsp.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.rtsp.recv(4096)
            if not chunk:
                raise ConnectionError(f"AirPlay receiver closed the connection during {method}")
            raw += chunk
        head, rest = raw.split(b"\r\n\r\n", 1)
        hlines = head.decode(errors="replace").split("\r\n")
        try:
            code = int(hlines[0].split()[1])
        except (IndexError, ValueError):
            raise ConnectionError(f"bad AirPlay reply to {method}: {hlines[0]!r}") from None
        resp = {}
        for line in hlines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp[k.strip().lower()] = v.strip()
        need = int(resp.get("content-length", "0") or 0) - len(rest)
        while need > 0:
            chunk = self.rtsp.recv(need)
            if not chunk:
                break
            need -= len(chunk)
        if code != 200:
            raise ConnectionError(f"AirPlay receiver answered {method} with {code}")
        return code, resp

    def _udp(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        self.socks.append(s)
        return s

    def start(self) -> None:
        self.rtsp = socket.create_connection((self.host, self.port), timeout=5)
        local_ip = self.rtsp.getsockname()[0]
        self.uri = f"rtsp://{local_ip}/{self.session_id}"
        self.timing, self.control, self.audio = self._udp(), self._udp(), self._udp()
        with self.lock:
            self._request("OPTIONS", "*")
            sdp = (
                f"v=0\r\no=iTunes {self.session_id} 0 IN IP4 {local_ip}\r\ns=iTunes\r\n"
                f"c=IN IP4 {self.host}\r\nt=0 0\r\nm=audio 0 RTP/AVP 96\r\n"
                f"a=rtpmap:96 AppleLossless\r\n"
                f"a=fmtp:96 {AIRPLAY_FRAMES_PER_PACKET} 0 16 40 10 14 2 255 0 0 44100\r\n"
            )
            self._request("ANNOUNCE", body=sdp.encode(), content_type="application/sdp")
            _, resp = self._request("SETUP", headers={"Transport": (
                "RTP/AVP/UDP;unicast;interleaved=0-1;mode=record;"
                f"control_port={self.control.getsockname()[1]};timing_port={self.timing.getsockname()[1]}"
            )})
            opts = dict(o.split("=", 1) for o in resp.get("transport", "").split(";") if "=" in o)
            self.server_audio = (self.host, int(opts["server_port"]))
            self.server_control = (self.host, int(opts.get("control_port", 0)))
            self.session = resp.get("session", "1").split(";")[0]
            self.start_ts = self.head_ts = ntp_to_frames(ntp_now())
            threading.Thread(target=self._timing_loop, daemon=True, name=f"airplay-timing-{self.label}").start()
            self._request("RECORD", headers={
                "Session": self.session, "Range": "npt=0-",
                "RTP-Info": f"seq={self.seq};rtptime={self.rtptime}",
            })
        threading.Thread(target=self._sync_loop, daemon=True, name=f"airplay-sync-{self.label}").start()

    def alive(self) -> bool:
        """False once the receiver closed the session or stopped taking audio."""
        if self.failed or self.closed.is_set() or not self.rtsp:
            return False
        if not self.lock.acquire(blocking=False):
            return True  # a request is running right now
        try:
            readable, _, _ = select.select([self.rtsp], [], [], 0)
            if readable and not self.rtsp.recv(1, socket.MSG_PEEK):
                self.failed = True
        except OSError:
            self.failed = True
        finally:
            self.lock.release()
        return not self.failed

    @property
    def rtptime(self) -> int:
        return (self.head_ts - self.start_ts + AIRPLAY_LATENCY_FRAMES) & 0xFFFFFFFF

    # -- UDP side
    def _timing_loop(self) -> None:
        self.timing.settimeout(1)
        while not self.closed.is_set():
            try:
                data, addr = self.timing.recvfrom(128)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) >= 32 and data[1] & 0x7F == 0x52:  # timing request -> reply
                recv = ntp_now()
                reply = struct.pack(">BBHI", 0x80, 0xD3, struct.unpack(">H", data[2:4])[0], 0) + data[24:32]
                reply += struct.pack(">QQ", recv, ntp_now())
                try:
                    self.timing.sendto(reply, addr)
                except OSError:
                    return

    def _sync_loop(self) -> None:
        first = True
        while not self.closed.wait(0 if first else 1.0):
            if not self.server_control[1]:
                return
            now = frames_to_ntp(self.head_ts)
            pkt = struct.pack(">BBHIQI", 0x90 if first else 0x80, 0xD4, 0x0007,
                              (self.rtptime - AIRPLAY_LATENCY_FRAMES) & 0xFFFFFFFF, now, self.rtptime)
            first = False
            try:
                self.control.sendto(pkt, self.server_control)
            except OSError:
                return

    @staticmethod
    def alac_frame(pcm_le: bytes) -> bytes:
        """Wrap 16-bit stereo PCM in an ALAC frame that stores it uncompressed (as iPhones may do).

        Bits: channel tag 1 (stereo, 3), 4 + 12 unused, has-size 0, wasted bytes 0 (2),
        not-compressed 1, then L/R samples big-endian, then the end tag 7 (3), padded to a byte.
        """
        samples = array.array("h", pcm_le)
        if sys.byteorder == "little":
            samples.byteswap()
        body = samples.tobytes()
        nbits = 23 + len(body) * 8 + 3
        pad = -nbits % 8
        value = (((0x100001 << (len(body) * 8)) | int.from_bytes(body, "big")) << 3 | 7) << pad
        return value.to_bytes((nbits + pad) // 8, "big")

    def send_audio(self, pcm_le: bytes) -> None:
        header = struct.pack(">BBHII", 0x80, 0xE0 if self.first_packet else 0x60, self.seq,
                             self.rtptime, self.session_id)
        self.first_packet = False
        try:
            self.audio.sendto(header + self.alac_frame(pcm_le), self.server_audio)
        except ConnectionRefusedError:  # nobody listens on the audio port any more
            self.failed = True
        self.seq = (self.seq + 1) & 0xFFFF
        self.head_ts += len(pcm_le) // FRAME_BYTES

    # -- song info
    def set_metadata(self, title: str, artist: str, album: str, artwork: bytes | None) -> None:
        items = b""
        if title:
            items += dmap_tag("minm", title.encode())
        if artist:
            items += dmap_tag("asar", artist.encode())
        if album:
            items += dmap_tag("asal", album.encode())
        with self.lock:
            if self.closed.is_set() or not self.rtsp:
                return
            info = {"Session": self.session, "RTP-Info": f"seq={self.seq};rtptime={self.rtptime}"}
            if items:
                self._request("SET_PARAMETER", headers=info, body=dmap_tag("mlit", items),
                              content_type="application/x-dmap-tagged")
            if artwork:
                kind = "image/png" if artwork[:4] == b"\x89PNG" else "image/jpeg"
                self._request("SET_PARAMETER", headers=info, body=artwork, content_type=kind)

    def close(self) -> None:
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            try:
                if self.rtsp:
                    self.rtsp.settimeout(2)
                    self._request("TEARDOWN", headers={"Session": getattr(self, "session", "1")})
            except (OSError, ConnectionError):
                pass
            for s in [self.rtsp, *self.socks]:
                if s:
                    s.close()
            self.rtsp = None


# --------------------------------------------------------------------------- one target
class Target:
    """One Tidal Connect device bound to one Music Assistant player."""

    def __init__(self, mgr: "Manager", player_id: str, name: str, slot: int, vban_id: str | None,
                 route: str = "vban", airplay_added: bool = False) -> None:
        self.mgr = mgr
        self.route = route
        self.airplay_added = airplay_added  # we put this player into the AirPlay Receiver's list
        self.player_id = player_id
        self.name = name
        self.slot = slot
        self.vban_id = vban_id
        self.ws_port = WEBSOCKET_PORT_BASE + slot
        self.vban_port = VBAN_PORT_BASE + slot
        self.stream_name = f"tidal-{slot}"
        self.home = os.path.join(RUN_DIR, f"t{slot}")
        self.fifo = os.path.join(self.home, "audio.fifo")
        self.state = "starting"
        self.error = ""
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self.control = TidalControl(self)
        self.streaming = False  # Tidal audio is flowing
        self.started_at = 0.0  # when we last asked MA to play this device
        self.released = False  # MA moved on (stopped / other source): don't stop it again
        self.released_at = 0.0
        self.last_loud = 0.0  # last time real (non-silent) audio came from Tidal
        self.restart_requested = threading.Event()  # start MA playback again (e.g. after a takeover)
        self.ha_paused_at = 0.0  # last pause pressed in Home Assistant and sent to Tidal
        self.sender: AirPlaySender | None = None
        self.song: dict = {}  # latest song info from Tidal
        self.song_lock = threading.Lock()
        self.song_seq = 0

    def to_dict(self) -> dict:
        return {"player_id": self.player_id, "name": self.name, "slot": self.slot, "vban_id": self.vban_id,
                "route": self.route, "airplay_added": self.airplay_added}

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "port": self.ws_port}

    # -- lifecycle
    def start(self) -> None:
        os.makedirs(self.home, exist_ok=True)
        for stale in (self.fifo, self.control.path):
            if os.path.exists(stale):
                os.remove(stale)
        os.mkfifo(self.fifo)
        # each copy reads its own ~/.asoundrc, which sends "default" into its own pipe
        with open(os.path.join(self.home, ".asoundrc"), "w", encoding="ascii") as f:
            f.write(
                "pcm.!default {\n type plug\n slave { pcm \"tidalpipe\"\n format S16_LE\n rate 44100\n channels 2 }\n}\n"
                f"pcm.tidalpipe {{\n type file\n slave.pcm {{ type null }}\n file \"{self.fifo}\"\n format \"raw\"\n}}\n"
            )
        workers = [self._run_program, self._run_stream]
        if self.mgr.options.get("tidal_controls", True):
            workers.append(self.control.run)
        for fn in workers:
            t = threading.Thread(target=fn, daemon=True, name=f"{fn.__name__}-{self.slot}")
            t.start()
            self._threads.append(t)
        via = "MA's AirPlay Receiver" if self.route == "airplay" else f"VBAN port {self.vban_port}"
        log(f"[{self.name}] Tidal Connect device on port {self.ws_port}, audio via {via}")

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self.state == "playing" and not self.released:
            self._ma(lambda ma: ma.call("players/cmd/stop", player_id=self.player_id))
        self.state = "off"

    # -- Tidal program
    def _command(self) -> list[str]:
        opts = self.mgr.options
        cert = f"{APPDIR}/id_certificate/IfiAudio_ZenStream.dat"  # comes with the Tidal Connect image
        cmd = [
            LOADER, "--library-path", LIBS, APP,
            "--tc-certificate-path", cert,
            "--playback-device", "default",
            "--friendly-name", self.name,
            "--model-name", "Music Assistant",
            "--codec-mpegh", "true",
            "--codec-mqa", "false",
            "--disable-app-security", "false",
            "--disable-web-security", "false",
            "--enable-mqa-passthrough", "false",
            "--log-level", str(opts.get("log_level", 3)),
            "--enable-websocket-log", "0",
            "--websocket-port", str(self.ws_port),
            "--netif-for-deviceid", self.mgr.lan_if,
            "--additional-seed", self.player_id,
        ]
        if self.mgr.options.get("tidal_controls", True):
            cmd += ["--controller-unix-socket-path", self.control.path]
        return cmd

    def _run_program(self) -> None:
        env = {**os.environ, "HOME": self.home}
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    self._command(), cwd=APPDIR, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                )
            except OSError as err:
                self.state, self.error = "error", f"cannot start Tidal program: {err}"
                log(f"[{self.name}] {self.error}")
                return
            if self.state in ("starting", "error") and not self.streaming:
                self.state, self.error = "ready", ""
            assert self._proc.stdout
            for line in self._proc.stdout:
                line = line.rstrip()
                if line and not NOISE.match(line):
                    print(f"[{self.name}] {line}", flush=True)
            code = self._proc.wait()
            if self._stop.is_set():
                return
            self.state, self.error = "error", f"Tidal program exited (code {code}), restarting"
            log(f"[{self.name}] {self.error}")
            self._stop.wait(10)

    # -- audio streaming
    def _ma(self, fn) -> None:
        try:
            if self.mgr.ma.token:
                fn(self.mgr.ma)
        except RuntimeError as err:
            self.error = str(err)
            log(f"[{self.name}] {err}")

    def _start_player(self) -> None:
        self.started_at, self.released = time.monotonic(), False
        if self.route == "vban":
            if not self.vban_id:
                self.error = "no VBAN Receiver in Music Assistant for this device"
                log(f"[{self.name}] {self.error}")
                return
            self._ma(lambda ma: ma.call("players/cmd/select_source", player_id=self.player_id, source=self.vban_id))
        log(f"[{self.name}] Tidal started: playing on {self.name}")
        if self.route == "airplay":
            self._nudge_airplay()
        self.control.on_player_started()

    def _nudge_airplay(self) -> None:
        """MA's AirPlay Receiver starts the player itself, but only when its receiver goes from idle
        to active. When Tidal resumes within ~10 s of a stop (or MA stopped the player while the
        session stayed open), that never happens, so check and start the player ourselves."""
        apid = self.mgr.find_airplay()
        if not apid or not self.mgr.ma.token:
            return
        self._stop.wait(4)
        if self._stop.is_set() or not self.streaming:
            return
        try:
            p = self.mgr.ma.call("players/get", player_id=self.player_id) or {}
            state = str(p.get("playback_state") or p.get("state") or "")
            if state == "playing" and apid in str(p.get("active_source") or ""):
                return
            log(f"[{self.name}] MA didn't start {self.name} by itself: starting it")
            self.mgr.ma.call("player_queues/play_media", queue_id=self.player_id,
                             media=f"{apid}://audio_source/{self.player_id}")
        except RuntimeError as err:
            log(f"[{self.name}] could not start {self.name}: {err}")

    # -- song info (AirPlay route only)
    def set_song(self, title: str, artist: str, album: str, image_url: str | None) -> None:
        """Called from the control thread; the cover download runs in the background."""
        with self.song_lock:
            self.song_seq += 1
            seq = self.song_seq

        def work() -> None:
            song = {"title": title, "artist": artist, "album": album, "image_url": image_url, "artwork": None}
            previous = self.song
            if previous and previous.get("image_url") == image_url:
                song["artwork"] = previous.get("artwork")
            elif image_url:
                song["artwork"] = fetch_image(image_url)
            with self.song_lock:
                if seq != self.song_seq:  # a newer song arrived while downloading
                    return
                self.song = song
            self._push_song()

        threading.Thread(target=work, daemon=True, name=f"song-{self.slot}").start()

    def _push_song(self) -> None:
        sender, song = self.sender, self.song
        if not sender or not song:
            return
        try:
            sender.set_metadata(song["title"], song["artist"], song["album"], song["artwork"])
        except (OSError, ConnectionError) as err:
            log(f"[{self.name}] could not send song info: {err}")

    def _airplay_port(self) -> int:
        ma, apid = self.mgr.ma, self.mgr.find_airplay()
        if not apid:
            raise RuntimeError("no AirPlay Receiver plugin in Music Assistant")
        connected = ma.airplay_connected_players(apid)
        port = airplay_receiver_ports(apid, connected).get(self.player_id)
        if port and port_open("127.0.0.1", port):
            return port
        name = ma.airplay_publish_name(apid, self.name)
        port = browse_airplay_port(name)
        if port:
            return port
        raise RuntimeError(f"can't find the AirPlay receiver \"{name}\" of Music Assistant")

    # -- audio streaming
    def _run_stream(self) -> None:
        stop_after = float(self.mgr.options.get("stop_after", 5))
        airplay = self.route == "airplay"
        frames_per_packet = AIRPLAY_FRAMES_PER_PACKET if airplay else FRAMES_PER_PACKET
        packet_bytes = frames_per_packet * FRAME_BYTES
        rfd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        keep_wfd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)  # never see EOF between tracks
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dest = ("127.0.0.1", self.vban_port)
        name = self.stream_name.encode("ascii")[:16].ljust(16, b"\x00")
        silence = bytes(packet_bytes)
        st = {"counter": 0, "clock": 0.0, "frames": 0}
        buf = bytearray()
        playing, last_data = False, 0.0

        def send(payload: bytes) -> None:
            if airplay:
                if self.sender:
                    try:
                        self.sender.send_audio(payload)
                    except OSError:
                        pass
            else:
                header = struct.pack(
                    "<4sBBBB16sL", b"VBAN", VBAN_SR_44100, FRAMES_PER_PACKET - 1, CHANNELS - 1, VBAN_INT16,
                    name, st["counter"] & 0xFFFFFFFF,
                )
                sock.sendto(header + payload, dest)
            st["counter"] += 1
            st["frames"] += frames_per_packet
            ahead = (st["clock"] + st["frames"] / RATE - PREFILL_SECONDS) - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)

        def end_airplay() -> None:
            if self.sender:
                self.sender.close()
                self.sender = None

        next_try = [0.0]

        def ensure_airplay() -> None:
            """Open the AirPlay session, or reopen it if MA's receiver went away (e.g. it restarted)."""
            if self.sender and self.sender.alive():
                return
            now = time.monotonic()
            if now < next_try[0]:
                return
            next_try[0] = now + AIRPLAY_RETRY_SECONDS
            reconnect = self.sender is not None
            end_airplay()
            try:
                sender = AirPlaySender("127.0.0.1", self._airplay_port(), str(self.slot))
                sender.start()
            except (OSError, ConnectionError, RuntimeError, KeyError, ValueError) as err:
                self.state, self.error = "error", f"AirPlay: {err}"
                log(f"[{self.name}] {self.error}")
                return
            self.sender = sender
            if self.state == "error":
                self.state, self.error = "playing", ""
            what = "reconnected" if reconnect else "streaming"
            log(f"[{self.name}] {what} to MA's AirPlay receiver on port {sender.port}")
            if reconnect:
                self.started_at, self.released = time.monotonic(), False
            threading.Thread(target=self._push_song, daemon=True).start()

        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([rfd], [], [], 0.05)
                if playing and self.restart_requested.is_set():
                    self.restart_requested.clear()
                    log(f"[{self.name}] Tidal is playing but {self.name} isn't: starting it again")
                    threading.Thread(target=self._start_player, daemon=True).start()
                if playing and airplay:
                    ensure_airplay()
                now = time.monotonic()
                if not ready:
                    if playing:
                        held = now - self.ha_paused_at < PAUSE_HOLD_SECONDS and not self.released
                        if now - last_data > stop_after and not held:
                            playing = self.streaming = False
                            self.state = "ready"
                            end_airplay()
                            if self.released:
                                log(f"[{self.name}] Tidal silent for {stop_after:.0f}s")
                            else:
                                log(f"[{self.name}] Tidal silent for {stop_after:.0f}s: stopping {self.name}")
                                self._ma(lambda ma: ma.call("players/cmd/stop", player_id=self.player_id))
                        else:
                            due = (now - st["clock"] + PREFILL_SECONDS) * RATE
                            while st["frames"] + frames_per_packet <= due:
                                send(silence)
                    continue
                try:
                    chunk = os.read(rfd, 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    continue
                if chunk.count(0) != len(chunk):
                    self.last_loud = time.monotonic()
                if not playing:
                    playing = self.streaming = True
                    self.state, self.error = "playing", ""
                    st["clock"], st["frames"] = time.monotonic(), 0
                    if airplay:
                        next_try[0] = 0.0
                        ensure_airplay()
                        st["clock"] = time.monotonic()
                        threading.Thread(target=self._start_player, daemon=True).start()
                    else:
                        starter = threading.Thread(target=self._start_player, daemon=True)
                        starter.start()
                        deadline = time.monotonic() + START_SILENCE_SECONDS
                        while starter.is_alive() and time.monotonic() < deadline:
                            send(silence)
                buf += chunk
                while len(buf) >= packet_bytes:
                    send(bytes(buf[:packet_bytes]))
                    del buf[:packet_bytes]
                last_data = time.monotonic()
        finally:
            end_airplay()
            os.close(rfd)
            os.close(keep_wfd)
            sock.close()


def pick_image_url(images) -> str | None:
    """Tidal sends cover art in a few sizes; prefer a medium one (MA scales it anyway)."""
    if not isinstance(images, dict):
        return None
    for size in ("medium", "high", "low"):
        img = images.get(size)
        url = img.get("url") if isinstance(img, dict) else img if isinstance(img, str) else None
        if url:
            return str(url)
    return None


def fetch_image(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tidal-connect-bridge"}),
                                    timeout=10) as resp:
            data = resp.read(4_000_000)
        return data if data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG" else None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- Tidal control socket
class TidalControl:
    """Speaker-controller side of the Tidal program's control socket ("spkcon").

    The Tidal program listens on a Unix socket. Each message is JSON framed as
    FF 02 <length, 2 bytes big-endian> <JSON> FF 03 and carries a "command" key.
    The program asks the controller to apply volume (setVolumeMute, level 0..1), to grant
    the audio output (requestResources), and reports playback state and track info.
    The controller can send play / pause / next / previous / setVolumeMute back.
    """

    def __init__(self, target: "Target") -> None:
        self.t = target
        self.path = os.path.join(target.home, "control.sock")
        self.sock: socket.socket | None = None
        self.send_lock = threading.Lock()
        self.request_id = 0
        self.tidal_state = ""  # IDLE / BUFFERING / PLAYING / PAUSED
        self.level: int | None = None  # last volume (0..100) both sides agree on
        self.muted: bool | None = None
        self.from_tidal_at = 0.0
        self.connected = False
        self.away_polls = 0  # polls in a row that saw MA play something else / nothing
        self.last_media: tuple | None = None

    # -- framing
    @staticmethod
    def frame(msg: dict) -> bytes:
        body = json.dumps(msg, separators=(",", ":")).encode()
        return b"\xff\x02" + struct.pack(">H", len(body)) + body + b"\xff\x03"

    @staticmethod
    def unframe(buf: bytearray) -> list[dict]:
        """Take every complete packet off the front of buf."""
        out = []
        while True:
            start = buf.find(b"\xff\x02")
            if start < 0:
                buf.clear()
                return out
            if start:
                del buf[:start]
            if len(buf) < 4:
                return out
            size = struct.unpack(">H", bytes(buf[2:4]))[0]
            if len(buf) < 4 + size + 2:
                return out
            body, tail = bytes(buf[4:4 + size]), bytes(buf[4 + size:6 + size])
            if tail != b"\xff\x03":  # out of step: skip this marker, look for the next one
                del buf[:2]
                continue
            del buf[:6 + size]
            try:
                msg = json.loads(body)
            except ValueError:
                continue
            if isinstance(msg, dict):
                out.append(msg)

    def send(self, command: str, with_id: bool = True, **fields) -> None:
        msg: dict = {"command": command, **fields}
        with self.send_lock:
            if with_id:
                self.request_id += 1
                msg["requestId"] = self.request_id
            if self.sock:
                try:
                    self.sock.sendall(self.frame(msg))
                except OSError:
                    pass

    # -- main loop
    def run(self) -> None:
        t = self.t
        next_poll = 0.0
        while not t._stop.is_set():
            if not self.sock:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.path)
                except OSError:
                    s.close()
                    t._stop.wait(1)
                    continue
                s.settimeout(0.5)
                self.sock, self.connected, buf = s, True, bytearray()
                log(f"[{t.name}] connected to the Tidal program's controls")
                self.send("startService", with_id=False)
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("closed")
                buf += chunk
                for msg in self.unframe(buf):
                    self.handle(msg)
            except socket.timeout:
                pass
            except OSError:
                with self.send_lock:
                    self.sock.close()
                    self.sock = None
                self.connected = False
                if not t._stop.is_set():
                    log(f"[{t.name}] lost the Tidal program's controls, reconnecting")
                t._stop.wait(1)
                continue
            if time.monotonic() >= next_poll:
                next_poll = time.monotonic() + CONTROL_POLL_SECONDS
                self.poll_ma()

    # -- Tidal -> bridge
    def handle(self, msg: dict) -> None:
        t, cmd = self.t, msg.get("command")
        if cmd == "requestResources":
            self.send("grantResources")
        elif cmd == "setVolumeMute":
            vol = msg.get("volume") if isinstance(msg.get("volume"), dict) else msg
            level, mute = vol.get("level"), vol.get("mute")
            if not t.streaming:
                # not playing on MA yet: MA's own volume wins when playback starts
                return
            if isinstance(level, (int, float)):
                pct = int(round(level * 100 if level <= 1 else level))
                pct = max(0, min(100, pct))
                if pct != self.level:
                    self.level, self.from_tidal_at = pct, time.monotonic()
                    log(f"[{t.name}] Tidal volume {pct}%")
                    t._ma(lambda ma: ma.call("players/cmd/volume_set", player_id=t.player_id, volume_level=pct))
            if isinstance(mute, bool) and mute != self.muted:
                first = self.muted is None
                self.muted, self.from_tidal_at = mute, time.monotonic()
                if mute or not first:  # a first "not muted" report changes nothing
                    t._ma(lambda ma: ma.call("players/cmd/volume_mute", player_id=t.player_id, muted=mute))
        elif cmd == "notifyPlayerStatusChanged":
            state = msg.get("playerState") or msg.get("state")
            if isinstance(state, str) and state != self.tidal_state:
                self.tidal_state = state
                if state == "PLAYING":
                    t.ha_paused_at = 0.0
                if state == "PLAYING" and t.streaming and t.released:
                    t.restart_requested.set()
        elif cmd == "notifySessionState":
            state = msg.get("state") or msg.get("sessionState")
            if isinstance(state, str):
                log(f"[{t.name}] Tidal session: {state}")
        elif cmd == "notifyMediaChanged":
            info = msg.get("mediaInfo") or msg
            meta = info.get("metadata", info) if isinstance(info, dict) else {}
            if isinstance(meta, dict) and meta.get("title"):
                artists = meta.get("artists")
                if isinstance(artists, list):
                    artists = ", ".join(str(a.get("name", a) if isinstance(a, dict) else a) for a in artists)
                image_url = pick_image_url(meta.get("images"))
                key = (meta.get("title"), artists, meta.get("albumTitle"), image_url)
                if key == self.last_media:  # Tidal reports each song twice
                    return
                self.last_media = key
                log(f"[{t.name}] now playing: {meta.get('title')}" + (f" - {artists}" if artists else ""))
                t.set_song(str(meta.get("title") or ""), str(artists or ""), str(meta.get("albumTitle") or ""),
                           image_url)

    # -- Home Assistant buttons -> Tidal
    def on_ha_button(self, service: str) -> bool:
        """A media button was pressed in HA for this player. Returns True when Tidal took it."""
        t = self.t
        if not self.connected:
            return False
        now = time.monotonic()
        active = t.streaming and not t.released  # Tidal is what this player is playing right now
        resumable = self.tidal_state in ("PAUSED", "IDLE", "STOPPED", "") and \
            now - t.ha_paused_at < PAUSE_HOLD_SECONDS
        if service == "media_play_pause":
            service = "media_pause" if active and self.tidal_state == "PLAYING" else "media_play"
        if service in ("media_next_track", "media_previous_track") and (active or resumable):
            command = "next" if service == "media_next_track" else "previous"
        elif service == "media_pause" and active:
            command = "pause"
            t.ha_paused_at = now
        elif service == "media_play" and (resumable or (active and self.tidal_state == "PAUSED")):
            command = "play"
            t.ha_paused_at = 0.0
            if t.released and t.streaming:  # (a stopped stream starts MA anyway when audio comes)
                t.restart_requested.set()
        else:
            return False
        log(f"[{t.name}] {service.replace('media_', '').replace('_', ' ')} pressed in Home Assistant: "
            f"sending {command} to Tidal")
        self.send(command)
        return True

    # -- MA -> Tidal
    def on_player_started(self) -> None:
        """Right after MA starts playing: tell the Tidal app the player's current volume."""
        self.poll_ma(force_volume=True)

    def poll_ma(self, force_volume: bool = False) -> None:
        t = self.t
        if not self.connected or not t.mgr.ma.token or not (t.streaming or force_volume):
            return
        try:
            p = t.mgr.ma.call("players/get", player_id=t.player_id) or {}
        except RuntimeError:
            return
        now = time.monotonic()
        level = p.get("group_volume") if p.get("type") == "group" and p.get("group_volume") is not None else p.get("volume_level")
        muted = p.get("volume_muted")
        if isinstance(level, (int, float)) and now - self.from_tidal_at > VOLUME_ECHO_GUARD_SECONDS:
            level = int(round(level))
            if force_volume or level != self.level or (isinstance(muted, bool) and muted != self.muted):
                self.level = level
                if isinstance(muted, bool):
                    self.muted = muted
                self.send("setVolumeMute", volume={"level": level / 100.0, "mute": bool(self.muted)})
        # MA no longer playing this device (stopped in MA/HA, or switched to other music): pause Tidal
        if t.streaming and not t.released and now - t.started_at > RELEASE_GRACE_SECONDS:
            state = str(p.get("playback_state") or p.get("state") or "")
            source = str(p.get("active_source") or "")
            mine = t.mgr.airplay_id if t.route == "airplay" else t.vban_id
            moved_on = bool(p) and bool((mine and source and mine not in source) or state in ("idle", "off"))
            self.away_polls = self.away_polls + 1 if moved_on else 0
            if self.away_polls >= 2:
                self.away_polls = 0
                t.released, t.released_at = True, now
                log(f"[{t.name}] stopped in Music Assistant: pausing Tidal")
                self.send("pause")
        # Tidal kept (or started) playing after MA stopped, e.g. someone took the device over or
        # pressed play again in the app: start MA again instead of playing into the void
        elif t.streaming and t.released and now - t.released_at > RESUME_AFTER_SECONDS \
                and now - t.last_loud < 1.0:
            t.restart_requested.set()


# --------------------------------------------------------------------------- Home Assistant
class HAWebSocket:
    """Just enough of a websocket client (RFC 6455) to talk to Home Assistant's API."""

    def __init__(self, url: str) -> None:
        m = re.match(r"ws://([^/:]+)(?::(\d+))?(/.*)?$", url)
        if not m:
            raise ValueError(f"bad websocket url {url}")
        host, port, path = m.group(1), int(m.group(2) or 80), m.group(3) or "/"
        self.sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise ConnectionError(f"handshake refused: {status}")
        self.sock.settimeout(None)
        self.lock = threading.Lock()

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.lock:
            self.sock.sendall(head + mask + masked)

    def send(self, msg: dict) -> None:
        self._send_frame(0x1, json.dumps(msg).encode())

    def recv(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("closed by Home Assistant")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if fin:
                    return json.loads(message)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class HAButtons:
    """Watches the media buttons pressed in Home Assistant (dashboards, automations, voice) for the
    Music Assistant players that are Tidal devices, and sends them to Tidal while Tidal plays there.
    MA itself can't skip an AirPlay / VBAN source, so without this those buttons do nothing."""

    def __init__(self, mgr: "Manager") -> None:
        self.mgr = mgr
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.entities: dict[str, str] = {}  # media_player entity_id -> MA player_id

    def start(self) -> None:
        if not self.token:
            log("Home Assistant API not available: dashboard buttons won't reach Tidal")
            return
        threading.Thread(target=self.run, daemon=True, name="ha-buttons").start()

    def run(self) -> None:
        delay = 5
        while True:
            try:
                self.session()
                delay = 5
            except (OSError, ConnectionError, ValueError, RuntimeError) as err:
                log(f"Home Assistant connection: {err}; retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 120)

    def session(self) -> None:
        ws = HAWebSocket(HA_WEBSOCKET)
        try:
            if ws.recv().get("type") != "auth_required":
                raise RuntimeError("unexpected greeting")
            ws.send({"type": "auth", "access_token": self.token})
            reply = ws.recv()
            if reply.get("type") != "auth_ok":
                raise RuntimeError(f"login refused ({reply.get('message') or reply.get('type')})")
            ws.send({"id": 1, "type": "config/entity_registry/list"})
            ws.send({"id": 2, "type": "subscribe_events", "event_type": "call_service"})
            ws.send({"id": 3, "type": "subscribe_events", "event_type": "entity_registry_updated"})
            next_id = 4
            log("watching Home Assistant's media buttons for Tidal devices")
            while True:
                msg = ws.recv()
                if msg.get("type") == "result" and isinstance(msg.get("result"), list):
                    self.load_entities(msg.get("result") or [])
                elif msg.get("type") == "event":
                    event = msg.get("event") or {}
                    if event.get("event_type") == "entity_registry_updated":
                        ws.send({"id": next_id, "type": "config/entity_registry/list"})
                        next_id += 1
                    elif event.get("event_type") == "call_service":
                        self.on_call(event.get("data") or {})
        finally:
            ws.close()

    def load_entities(self, entries: list) -> None:
        found = {}
        for e in entries:
            if isinstance(e, dict) and e.get("platform") == "music_assistant" \
                    and str(e.get("entity_id", "")).startswith("media_player.") and e.get("unique_id"):
                found[e["entity_id"]] = str(e["unique_id"])
        self.entities = found

    def on_call(self, data: dict) -> None:
        if data.get("domain") != "media_player" or data.get("service") not in HA_MEDIA_SERVICES:
            return
        sd = data.get("service_data") or {}
        ids = sd.get("entity_id") or []
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",")]
        for entity_id in ids:
            player_id = self.entities.get(entity_id)
            t = self.mgr.targets.get(player_id) if player_id else None
            if t:
                try:
                    t.control.on_ha_button(str(data["service"]))
                except Exception as err:  # noqa: BLE001 - never let one press kill the watcher
                    log(f"[{t.name}] could not forward {data['service']}: {err}")


# --------------------------------------------------------------------------- manager
class Manager:
    def __init__(self) -> None:
        self.options = load_json(OPTIONS_FILE, {})
        self.ma = MusicAssistant(str(self.options.get("ma_url") or "http://127.0.0.1:8095"), str(self.options.get("ma_token") or ""))
        self.lan_if = lan_interface()
        self.lock = threading.Lock()
        self.targets: dict[str, Target] = {}
        self.route = "vban" if str(self.options.get("audio_route", "airplay")) == "vban" else "airplay"
        self.airplay_id: str | None = None

    def find_airplay(self) -> str | None:
        """The AirPlay Receiver plugin's instance id (looked up again if it was missing)."""
        if not self.airplay_id and self.ma.token:
            try:
                self.airplay_id = self.ma.airplay_receiver_id()
            except RuntimeError as err:
                log(f"Music Assistant not reachable ({err})")
        return self.airplay_id

    def connect_airplay(self, t: "Target") -> None:
        """Make sure MA's AirPlay Receiver runs a receiver for this player."""
        apid = self.find_airplay()
        if not apid:
            raise RuntimeError(
                "the AirPlay Receiver plugin isn't set up in Music Assistant. Add it under Settings > Plugins, "
                "or set audio_route to vban on the add-on's Configuration tab"
            )
        players = self.ma.airplay_connected_players(apid)
        if t.player_id not in players:
            self.ma.set_airplay_connected_players(apid, [*players, t.player_id])
            t.airplay_added = True
            log(f"[{t.name}] added to MA's AirPlay Receiver")

    def save(self) -> None:
        save_json(TARGETS_FILE, [t.to_dict() for t in self.targets.values()])

    def restore(self) -> None:
        saved = load_json(TARGETS_FILE, [])
        if not saved:
            log("no Tidal Connect devices switched on yet: open the add-on's sidebar page to add some")
        existing = set()
        try:
            existing = self.ma.vban_instance_ids()
        except RuntimeError as err:
            log(f"Music Assistant not reachable yet ({err}); starting devices anyway")
        for item in saved:
            t = Target(self, item["player_id"], item["name"], int(item["slot"]), item.get("vban_id"),
                       item.get("route", "vban"), bool(item.get("airplay_added")))
            if t.route != self.route:
                log(f"[{t.name}] switching audio route from {t.route} to {self.route}")
                self._switch_route(t, existing)
            elif t.route == "airplay":
                try:
                    self.connect_airplay(t)
                except RuntimeError as err:
                    t.error = str(err)
                    log(f"[{t.name}] {err}")
            elif existing and t.vban_id not in existing:
                log(f"[{t.name}] its VBAN Receiver is gone from Music Assistant, creating it again")
                try:
                    t.vban_id = self.ma.create_vban_receiver(t.stream_name, t.vban_port)
                except RuntimeError as err:
                    t.error = str(err)
                    log(f"[{t.name}] {err}")
            self.targets[t.player_id] = t
            t.start()
        self.save()

    def _switch_route(self, t: "Target", existing: set) -> None:
        try:
            if self.route == "airplay":
                self.connect_airplay(t)
                if t.vban_id and t.vban_id in existing:
                    self.ma.remove_provider(t.vban_id)
                t.vban_id = None
            else:
                self._disconnect_airplay(t)
                t.vban_id = self.ma.create_vban_receiver(t.stream_name, t.vban_port)
            t.route = self.route
        except RuntimeError as err:
            t.error = str(err)
            t.route = self.route
            log(f"[{t.name}] {err}")

    def _disconnect_airplay(self, t: "Target") -> None:
        if not t.airplay_added or not self.find_airplay():
            return
        players = self.ma.airplay_connected_players(self.airplay_id)
        if t.player_id in players:
            self.ma.set_airplay_connected_players(self.airplay_id, [p for p in players if p != t.player_id])
            log(f"[{t.name}] removed from MA's AirPlay Receiver")
        t.airplay_added = False

    def enable(self, player_id: str, name: str) -> Target:
        with self.lock:
            if player_id in self.targets:
                return self.targets[player_id]
            used = {t.slot for t in self.targets.values()}
            slot = next((i for i in range(MAX_TARGETS) if i not in used), None)
            if slot is None:
                raise RuntimeError(f"at most {MAX_TARGETS} Tidal Connect devices are supported")
            t = Target(self, player_id, name, slot, None, self.route)
            if self.route == "airplay":
                self.connect_airplay(t)
            else:
                t.vban_id = self.ma.create_vban_receiver(t.stream_name, t.vban_port)
                log(f"[{name}] created VBAN Receiver {t.vban_id} in Music Assistant")
            self.targets[player_id] = t
            self.save()
            t.start()
            log_listening_ports(15)
            return t

    def disable(self, player_id: str) -> None:
        with self.lock:
            t = self.targets.pop(player_id, None)
            if not t:
                return
            self.save()
        t.stop()
        log(f"[{t.name}] Tidal Connect device removed")
        try:
            if t.vban_id:
                self.ma.remove_provider(t.vban_id)
            self._disconnect_airplay(t)
        except RuntimeError as err:
            log(f"[{t.name}] could not clean up in Music Assistant: {err}")

    def overview(self) -> dict:
        info: dict = {"token": bool(self.ma.token), "error": "", "players": []}
        try:
            players = self.ma.players() if self.ma.token else []
        except RuntimeError as err:
            players, info["error"] = [], str(err)
        if not self.ma.token:
            info["error"] = "No Music Assistant token set. Add one on the add-on's Configuration tab."
        seen = set()
        for p in players:
            t = self.targets.get(p["player_id"])
            seen.add(p["player_id"])
            info["players"].append({**p, "enabled": t is not None, **(t.status() if t else {})})
        for pid, t in self.targets.items():  # enabled, but MA does not list it right now
            if pid not in seen:
                info["players"].append(
                    {"player_id": pid, "name": t.name, "type": "player", "available": False, "enabled": True, **t.status()}
                )
        info["ports"] = f"{WEBSOCKET_PORT_BASE}-{WEBSOCKET_PORT_BASE + MAX_TARGETS - 1}"
        return info


# --------------------------------------------------------------------------- web page
def make_handler(mgr: Manager):
    allowed = set(filter(None, os.environ.get("BRIDGE_ALLOWED_CLIENTS", "172.30.32.2,127.0.0.1").split(",")))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _allowed(self) -> bool:
            if self.client_address[0] in allowed:
                return True
            self.send_error(403, "Only reachable through Home Assistant")
            return False

        def _json(self, code: int, data) -> None:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._allowed():
                return
            path = self.path.split("?")[0]
            if path.endswith("/api/players"):
                self._json(200, mgr.overview())
                return
            try:
                with open(PAGE_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self._allowed():
                return
            if not self.path.split("?")[0].endswith("/api/toggle"):
                self.send_error(404)
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if data.get("enabled"):
                    mgr.enable(str(data["player_id"]), str(data.get("name") or data["player_id"]))
                else:
                    mgr.disable(str(data["player_id"]))
                self._json(200, {"ok": True})
            except (RuntimeError, KeyError, ValueError) as err:
                log(f"toggle failed: {err}")
                self._json(400, {"ok": False, "error": str(err)})

    return Handler


def main() -> None:
    os.makedirs(RUN_DIR, exist_ok=True)
    mgr = Manager()
    port = int(os.environ.get("BRIDGE_WEB_PORT", "8099"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(mgr))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"sidebar page ready (port {port}); announcing devices on {mgr.lan_if}")
    mgr.restore()
    HAButtons(mgr).start()
    log_listening_ports(20)

    def shutdown(*_):
        for t in list(mgr.targets.values()):
            t._stop.set()
            if t._proc and t._proc.poll() is None:
                t._proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
