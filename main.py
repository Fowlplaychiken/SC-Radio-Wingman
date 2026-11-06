"""Optional Wingman v3 skill for named radio stations and personal music."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from hashlib import file_digest
from itertools import islice
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit
from uuid import uuid4

from sc_radio_pulsenet import PulseNetCatalog
from sc_radio_sources import TIMEOUT_SECONDS, latest_audio_enclosure, validate_feed_url
from sc_radio_tinytag import TinyTag, TinyTagException
from services.file import get_audio_library_dir
from skills.skill_base import Skill


class PlaybackError(RuntimeError):
    pass


def player_executable(configured: str) -> Path:
    expected_name = "mpv.exe" if os.name == "nt" else "mpv"
    if configured.strip():
        path = Path(configured.strip().strip('"'))
        if path.is_absolute() and path.name.lower() == expected_name and path.is_file():
            return path
        raise PlaybackError("Set an absolute path to your installed mpv player in the skill settings.")

    if os.name == "nt":
        manifest = json.loads(Path(__file__).with_name("player-windows.json").read_text(encoding="utf-8"))
        for variable, relative in manifest["existing_locations"]:
            base = Path(os.environ.get(variable, ""))
            path = base / relative
            if path.is_absolute() and path.is_file():
                return path

        managed = Path(os.environ.get("LOCALAPPDATA", "")) / manifest["managed_location"]
        if managed.is_absolute() and managed.is_file():
            try:
                with managed.open("rb") as stream:
                    if file_digest(stream, "sha256").hexdigest() == manifest["executable_sha256"]:
                        return managed
            except OSError:
                pass
            raise PlaybackError("SC Radio's downloaded mpv failed verification. See the README for repair steps.")
        raise PlaybackError("mpv was not found. Run setup-player.ps1 from the SC Radio folder or set the mpv executable path.")

    for directory in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin", "/Applications/mpv.app/Contents/MacOS"):
        path = Path(directory) / expected_name
        if path.is_file():
            return path
    raise PlaybackError("mpv was not found. Install mpv or set the mpv executable path in the skill settings.")


class SkillLoopClosed(RuntimeError):
    pass


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_ulong),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_ulong),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_ulong),
        ("scheduling_class", ctypes.c_ulong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


def _windows_job_for_pid(pid: int):
    """Bind one child to a kill-on-close Job Object, or raise without leaking handles."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
    )
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process_handle = None
    try:
        limits = _JobExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = kernel32.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return job
    except BaseException:
        kernel32.CloseHandle(job)
        raise
    finally:
        if process_handle:
            kernel32.CloseHandle(process_handle)


def _close_windows_handle(handle):
    if handle:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        if not kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class SkillLoopPortal:
    """Own one asyncio loop so skill resources survive Wingman's per-turn loops."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop = None
        self._thread = None
        self._startup_error = None
        self._closing = False
        self._closed = False
        self._close_complete = threading.Event()

    def _start_locked(self):
        if self._closing or self._closed:
            raise SkillLoopClosed("SC Radio is disabled.")
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run_loop, name="sc-radio-owner", daemon=True
            )
            self._thread.start()
        thread = self._thread
        ready = self._ready
        # The loop thread does not acquire _lock while publishing startup state.
        if not ready.wait(timeout=5):
            raise RuntimeError("SC Radio owner loop did not start.")
        if self._startup_error:
            raise RuntimeError("SC Radio owner loop did not start.") from self._startup_error
        if self._closed or self._closing or thread is not self._thread:
            raise SkillLoopClosed("SC Radio is disabled.")
        return self._loop

    def _run_loop(self):
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
            asyncio.set_event_loop(None)

    def submit(self, async_callable):
        with self._lock:
            loop = self._start_locked()

            async def invoke():
                return await async_callable()

            return asyncio.run_coroutine_threadsafe(invoke(), loop)

    async def call(self, async_callable):
        return await asyncio.wrap_future(self.submit(async_callable))

    async def aclose(self, async_callable):
        with self._lock:
            if self._closed:
                return
            if self._closing:
                already_closing = True
                thread = loop = close_future = None
            else:
                already_closing = False
                self._closing = True
                thread = self._thread
                loop = self._loop
                close_future = None
                if loop is not None:
                    async def invoke_close():
                        return await async_callable()

                    close_future = asyncio.run_coroutine_threadsafe(invoke_close(), loop)

        if already_closing:
            await asyncio.to_thread(self._close_complete.wait)
            return
        close_error = None
        if close_future is not None:
            try:
                cleanup = asyncio.wrap_future(close_future)
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
            except BaseException as exc:
                close_error = exc
            finally:
                if loop is not None:
                    loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            await asyncio.to_thread(thread.join)
        with self._lock:
            self._closed = True
            self._closing = False
            self._close_complete.set()
        if close_error is not None:
            raise close_error


class Player:
    def __init__(self):
        self.process = None
        self.job_handle = None
        self.reader = self.writer = None
        self.socket_dir = None
        self.request_id = 0

    @property
    def running(self):
        return self.process is not None and self.process.returncode is None

    async def start(
        self, executable: str, sources: list[str], volume: int, *, shuffle: bool = False,
        youtube_resolver: str | None = None,
    ):
        await self.stop()
        path = player_executable(executable)
        if os.name == "nt":
            address = rf"\\.\pipe\wingman-sc-radio-{uuid4().hex}"
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            self.socket_dir = tempfile.TemporaryDirectory(prefix="wingman-radio-")
            address = str(Path(self.socket_dir.name) / "mpv")
            kwargs = {}
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(path),
                "--no-config",
                "--load-scripts=no",
                *((
                    "--ytdl=yes",
                    f"--script-opt=ytdl_hook-ytdl_path={youtube_resolver}",
                    "--ytdl-format=bestaudio/best",
                    "--ytdl-raw-options=ignore-config=,no-plugin-dirs=,js-runtimes=node,socket-timeout=8",
                ) if youtube_resolver else ("--ytdl=no",)),
                "--no-video",
                "--force-window=no",
                "--audio-file-auto=no",
                "--audio-display=no",
                "--no-terminal",
                "--load-unsafe-playlists=no",
                "--tls-verify=yes",
                "--network-timeout=5",
                f"--volume={volume}",
                f"--input-ipc-server={address}",
                *(("--shuffle",) if shuffle else ()),
                "--",
                *sources,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            if os.name == "nt":
                # A short spawn-to-assignment window remains; if binding fails, stop and reap mpv.
                self.job_handle = _windows_job_for_pid(self.process.pid)
            await asyncio.wait_for(self._wait_for_audio(address), 25 if youtube_resolver else 8)
        except (OSError, TimeoutError, PlaybackError):
            await self.stop()
            if youtube_resolver:
                raise PlaybackError(
                    "Online audio could not start. The source may be offline, "
                    "or yt-dlp may need an update or its JavaScript runtime."
                ) from None
            raise PlaybackError(
                "Audio could not start. Check the player, source and output device."
            ) from None
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def _wait_for_audio(self, address):
        while self.running:
            try:
                self.reader, self.writer = await self._connect(address)
                break
            except OSError:
                await asyncio.sleep(0.05)
        while self.running and self.writer:
            try:
                if await self.command("get_property", "audio-params"):
                    return
            except PlaybackError:
                pass  # mpv has not opened its audio track yet.
            await asyncio.sleep(0.15)
        raise PlaybackError("The player exited before opening audio.")

    @staticmethod
    async def _connect(address):
        if os.name != "nt":
            return await asyncio.open_unix_connection(address)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.create_pipe_connection(lambda: protocol, address)
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)

    async def command(self, *args):
        if not self.running or not self.writer:
            raise PlaybackError("The radio player has stopped.")
        self.request_id += 1
        request_id = self.request_id
        message = json.dumps({"command": args, "request_id": request_id}).encode()
        try:
            async with asyncio.timeout(2):
                self.writer.write(message + b"\n")
                await self.writer.drain()
                while line := await self.reader.readline():
                    reply = json.loads(line)
                    if reply.get("request_id") == request_id:
                        if reply.get("error") != "success":
                            raise PlaybackError("The radio player could not apply that command.")
                        return reply.get("data")
        except (OSError, ValueError, TimeoutError):
            pass
        raise PlaybackError("The radio player did not respond.")

    async def stop(self):
        if self.writer:
            self.writer.close()
            self.writer = self.reader = None
        if self.process:
            if self.running:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            await self.process.wait()
            self.process = None
        if self.job_handle:
            _close_windows_handle(self.job_handle)
            self.job_handle = None
        if self.socket_dir:
            self.socket_dir.cleanup()
            self.socket_dir = None


def stations_from(setting: str) -> dict[str, str]:
    stations = {}
    names = set()
    for line in setting.splitlines():
        if not line.strip():
            continue
        name, separator, url = (part.strip() for part in line.partition("="))
        source_type = "stream"
        if url.lower().startswith("rss:"):
            source_type, url = "rss", url[4:].strip()
        parsed = urlsplit(url)
        if (
            not separator
            or not name
            or len(name) > 60
            or len(url) > 2048
            or name.casefold() in names
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "\\" in url
            or any(c.isspace() for c in url)
            or any(ord(c) < 32 or ord(c) == 127 for c in url)
        ):
            raise ValueError("Use uniquely named stations with direct HTTPS audio feeds.")
        # Force URL port validation before handing a source to the player.
        _ = parsed.port
        if source_type == "rss":
            if not validate_feed_url(url):
                raise ValueError("RSS sources must use HTTPS on a public host.")
            url = "rss:" + url
        elif (parsed.hostname or "").lower().rstrip(".") in {
            "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
            "twitch.tv", "www.twitch.tv", "m.twitch.tv", "clips.twitch.tv",
        }:
            if parsed.port not in (None, 443):
                raise ValueError("YouTube and Twitch sources must use the standard HTTPS port.")
            source_type = "resolver"
        names.add(name.casefold())
        stations[name] = ("resolver:" if source_type == "resolver" else "") + url
    return stations


def library_tracks(root: Path, config) -> list[Path]:
    root = root.resolve()
    tracks = []
    for file in config.files[:100]:
        name = Path(file.name)
        if name.name != file.name or Path(file.path).is_absolute():
            continue
        try:
            path = (root / file.path / name).resolve()
            if (
                path.is_relative_to(root)
                and path.is_file()
                and path.suffix.casefold() in {".mp3", ".wav"}
            ):
                tracks.append(path)
        except (OSError, RuntimeError, ValueError):
            continue
    return tracks


class Track(NamedTuple):
    path: Path
    title: str
    artist: str
    album: str
    album_artist: str
    genre: str
    disc: int | None
    number: int | None

    def description(self):
        label = self.title or self.path.name
        if self.artist:
            label += f" — {self.artist}"
        if self.album:
            label += f" ({self.album})"
        return label


def tag_text(value):
    return " ".join(value.split())[:120] if isinstance(value, str) else ""


def music_key(value):
    if not isinstance(value, str):
        return ""
    folded = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", plain))


def read_track(path):
    try:
        tag = TinyTag.get(path, duration=False)
    except (TinyTagException, OSError):
        return Track(path, "", "", "", "", "", None, None)
    return Track(
        path, tag_text(tag.title), tag_text(tag.artist), tag_text(tag.album),
        tag_text(tag.albumartist), tag_text(tag.genre), tag.disc, tag.track,
    )


def tool_definition(name, description, properties=None, required=()):
    return name, {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


class ScRadio(Skill):
    def __init__(self, config, settings, wingman):
        super().__init__(config=config, settings=settings, wingman=wingman)
        self.portal = SkillLoopPortal()
        self.player = Player()
        self.pulsenet = PulseNetCatalog()
        self.lock = asyncio.Lock()
        self.source = None
        self.track_cache = {}
        self.volume = None
        self.closed = False

    def get_tools(self):
        return [
            tool_definition(
                "sc_radio_list",
                "List configured broadcast stations or selected local music by songs, artists, albums or genres. "
                "Use for ordinary catalogue questions or to look up names before playing.",
                {
                    "kind": {"type": "string", "enum": ["stations", "songs", "artists", "albums", "genres"]},
                    "station_offset": {
                        "type": "integer", "minimum": 0,
                        "description": "For more stations, use the next station offset from the previous result.",
                    },
                },
            ),
            tool_definition(
                "sc_radio_play",
                "Play a broadcast station, combine PulseNet genre stations, or play personal music. "
                "Use station_mix with an ordered list of PulseNet station names. "
                "Use shuffle=true to play a personal music collection at random. For a song or album by "
                "a specific artist, pass both names.",
                {
                    "kind": {
                        "type": "string",
                        "enum": ["station", "station_mix", "song", "artist", "album", "genre", "playlist"],
                    },
                    "name": {
                        "type": "string",
                        "description": "Station, song title, artist, album, or genre name, according to kind.",
                    },
                    "stations": {
                        "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 24,
                        "description": "PulseNet genre station names in play order, for station_mix.",
                    },
                    "artist": {"type": "string", "description": "Artist to qualify a song or album."},
                    "album": {"type": "string", "description": "Album to qualify a song or artist."},
                    "shuffle": {
                        "type": "boolean",
                        "description": "Play a music collection in random order.",
                    },
                },
                ("kind",),
            ),
            tool_definition(
                "sc_radio_control",
                "Control SC Radio music. Skip means next track. Restart means start the current song "
                "over. Repeat one repeats the current song, and repeat all repeats the playlist.",
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "pause", "resume", "skip", "previous", "restart", "stop", "status",
                            "mute", "unmute", "repeat_one", "repeat_all", "repeat_off",
                        ],
                    }
                },
                ("action",),
            ),
            tool_definition(
                "sc_radio_queue",
                "Show the current music queue, add a selected song to the end, or play it next.",
                {
                    "action": {"type": "string", "enum": ["show", "append", "next"]},
                    "name": {"type": "string", "description": "Song title for append or next."},
                    "artist": {"type": "string", "description": "Optional artist qualifier."},
                    "album": {"type": "string", "description": "Optional album qualifier."},
                },
                ("action",),
            ),
            tool_definition(
                "sc_radio_volume",
                "Set music volume with percent, or make it louder/quieter with a signed change "
                "in percentage points. Use one argument.",
                {
                    "percent": {"type": "integer", "minimum": 0, "maximum": 100},
                    "change": {"type": "integer", "minimum": -100, "maximum": 100},
                },
            ),
        ]

    def _setting(self, name):
        return self.retrieve_custom_property_value(name, [])

    def _sources(self):
        config = self._setting("audio_config")
        tracks = library_tracks(Path(get_audio_library_dir()), config) if config else []
        return stations_from(self._setting("stations") or ""), tracks

    def _pulsenet_enabled(self):
        return self._setting("pulsenet_enabled") is True

    def _tracks(self, paths):
        previous = self.track_cache
        self.track_cache = {}
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            cached = previous.get(path)
            track = cached[1] if cached and cached[0] == signature else read_track(path)
            self.track_cache[path] = (signature, track)
        return [track for _, track in self.track_cache.values()]

    def _volume(self):
        config = self._setting("audio_config")
        return self.volume if self.volume is not None else round(100 * (config.volume if config else 0.5))

    async def _unload_on_owner_loop(self):
        async with self.lock:
            await self.player.stop()

    async def unload(self):
        self.closed = True
        try:
            await self.portal.aclose(self._unload_on_owner_loop)
        finally:
            await super().unload()

    async def execute_tool(self, tool_name, parameters, benchmark):
        async def execute_on_owner_loop():
            async with self.lock:
                if self.closed:
                    return "SC Radio is disabled.", ""
                try:
                    return await self._execute(tool_name, parameters), ""
                except (OSError, ValueError, PlaybackError) as error:
                    # Keep local paths, URLs and process logs out of tool replies.
                    if isinstance(error, PlaybackError):
                        return str(error), ""
                    return (
                        "Check the station list and Audio Library selections in SC Radio settings.",
                        "",
                    )

        try:
            return await self.portal.call(execute_on_owner_loop)
        except SkillLoopClosed:
            return "SC Radio is disabled.", ""

    async def _execute(self, tool_name, parameters):
        if tool_name == "sc_radio_list":
            return await self._list(parameters.get("kind"), parameters.get("station_offset", 0))
        if tool_name == "sc_radio_play":
            return await self._play(
                parameters.get("kind"), parameters.get("name"), parameters.get("shuffle", False),
                parameters.get("artist"), parameters.get("album"), parameters.get("stations"),
            )
        if tool_name == "sc_radio_queue":
            return await self._queue(
                parameters.get("action"), parameters.get("name"),
                parameters.get("artist"), parameters.get("album"),
            )
        if tool_name == "sc_radio_volume":
            percent, change = parameters.get("percent"), parameters.get("change")
            if percent is None and type(change) is int and -100 <= change <= 100:
                percent = min(100, max(0, self._volume() + change))
                change = None
            if type(percent) is not int or not 0 <= percent <= 100 or change is not None:
                return "Choose an integer volume from 0 to 100 or give one relative change."
            if self.player.running:
                await self.player.command("set_property", "volume", percent)
            self.volume = percent
            return f"SC Radio volume set to {percent}%."
        if tool_name == "sc_radio_control":
            return await self._control(parameters.get("action"))
        return "Unknown SC Radio command."

    async def _list(self, kind, station_offset=0):
        stations, paths = self._sources()
        if type(station_offset) is not int or station_offset < 0:
            return "Choose a nonnegative station offset."
        if kind in (None, "stations") and self._pulsenet_enabled():
            configured = {name.casefold() for name in stations}
            stations.update(
                (name, url) for name, url in (await self.pulsenet.stations()).items()
                if name.casefold() not in configured
            )
        if station_offset and station_offset >= len(stations):
            return "No more stations."
        end = station_offset + 32
        page = islice(stations, station_offset, end)
        station_list = f"Stations: {', '.join(page) or 'none'}."
        if end < len(stations):
            station_list += f" Showing {station_offset + 1}–{end} of {len(stations)}. Next station offset: {end}."
        if kind == "stations":
            return station_list
        tracks = self._tracks(paths)
        tag_fields = {"artists": "artist", "albums": "album", "genres": "genre"}
        if kind in tag_fields:
            values = {getattr(track, tag_fields[kind]) for track in tracks} - {""}
            return f"{kind.title()}: {', '.join(sorted(values, key=str.casefold)) or 'none tagged'}."
        songs = f"Selected songs: {', '.join(track.description() for track in tracks) or 'none'}."
        return songs if kind == "songs" else f"{station_list} {songs}"

    async def _play(self, kind=None, name=None, shuffle=False, artist=None, album=None, station_names=None):
        if type(shuffle) is not bool:
            return "Shuffle must be true or false."
        if shuffle and kind not in ("playlist", "artist", "album", "genre"):
            return "Shuffle is only available for a music collection."
        stations, paths = self._sources()
        player_options = {"shuffle": shuffle}
        use_youtube = False
        if kind == "station":
            key = name.strip().casefold() if isinstance(name, str) else ""
            matches = [(label, url) for label, url in stations.items() if label.casefold() == key]
            if not matches and self._pulsenet_enabled():
                matches = [
                    (label, url) for label, url in (await self.pulsenet.stations()).items()
                    if label.casefold() == key
                ]
                use_youtube = bool(matches)
            if not matches:
                return "Source not found. Use the SC Radio list to choose a configured name."
            label, source = matches[0]
            if source.startswith("rss:"):
                try:
                    source = await asyncio.wait_for(
                        asyncio.to_thread(latest_audio_enclosure, source[4:]), timeout=TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    return "The podcast feed timed out. Try again later."
                except ValueError as exc:
                    return f"{exc}"
            elif source.startswith("resolver:"):
                source = source[len("resolver:"):]
                use_youtube = True
            sources = [source]
            if use_youtube and urlsplit(source).path == "/playlist":
                kind = "online_playlist"
        elif kind == "station_mix":
            if not self._pulsenet_enabled():
                return "Enable PulseNet in SC Radio settings to combine its genre stations."
            if not isinstance(station_names, list) or not 2 <= len(station_names) <= 24:
                return "Give two or more PulseNet genre station names in play order."
            keys = [music_key(item) for item in station_names]
            if not all(keys) or len(set(keys)) != len(keys):
                return "Give distinct PulseNet genre station names."
            available = {
                music_key(label): (label, url)
                for label, url in (await self.pulsenet.stations()).items()
                if url.startswith("https://www.youtube.com/playlist?")
            }
            if any(key not in available for key in keys):
                return "Use the SC Radio station list to choose available PulseNet genre stations."
            label = "PulseNet mix: " + ", ".join(available[key][0] for key in keys)
            sources = [available[key][1] for key in keys]
            use_youtube = True
        elif kind == "playlist":
            label = "Shuffled personal playlist" if shuffle else "Personal playlist"
            sources = [str(track.path) for track in self._tracks(paths)]
        elif kind in ("song", "artist", "album", "genre"):
            matches = self._find_music(kind, name, artist, album, paths)
            if not matches:
                return "No selected music matches. Check the title, artist or album in the SC Radio list."
            if kind == "song" and len(matches) > 1:
                return "Several songs match. Give the artist, album, or exact filename."
            if kind == "album":
                album_artists = {music_key(track.album_artist or track.artist) for track in matches}
                if len(album_artists - {""}) > 1 and not artist:
                    return "Several artists have that album. Which artist did you mean?"
                matches.sort(key=lambda track: (track.disc or 1, track.number or float("inf")))
            label = {
                "song": matches[0].description(),
                "artist": matches[0].artist or matches[0].album_artist,
                "album": matches[0].album,
                "genre": matches[0].genre,
            }[kind]
            sources = [str(track.path) for track in matches]
        else:
            return "Choose a station, song, artist, album, genre or your personal playlist."
        if not sources:
            return "No songs are selected. Choose tracks from Wingman's Audio Library."
        if use_youtube:
            resolver = shutil.which("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
            if resolver is None:
                return "Install yt-dlp and its JavaScript runtime to play this public YouTube or Twitch source. See the SC Radio README."
            player_options["youtube_resolver"] = str(Path(resolver).resolve())
        if shuffle and kind != "playlist":
            label = f"Shuffled {label}"
        self.source = None
        await self.player.start(
            self._setting("player_path") or "", sources, min(100, max(0, self._volume())), **player_options,
        )
        self.source = (kind, label)
        return f"Playing {label}."

    def _find_music(self, kind, name, artist, album, paths):
        key = music_key(name)
        if not key:
            return []
        artist_key, album_key = music_key(artist), music_key(album)
        found = []
        for track in self._tracks(paths):
            artist_names = {music_key(track.artist), music_key(track.album_artist)} - {""}
            album_name = music_key(track.album)
            if (artist_key and artist_key not in artist_names) or (album_key and album_key != album_name):
                continue
            names = {
                "song": {music_key(track.title), music_key(track.path.stem), music_key(track.path.name)},
                "artist": artist_names,
                "album": {album_name},
                "genre": {music_key(track.genre)},
            }[kind]
            if key in names:
                found.append(track)
        return found

    def _playing_track_label(self, value):
        path = Path(value)
        cached = self.track_cache.get(path)
        return cached[1].description() if cached else path.name

    async def _queue(self, action, name, artist, album):
        if not self.player.running or not self.source or self.source[0] in ("station", "station_mix", "online_playlist"):
            return "Start playing personal music to use the queue."
        if action == "show":
            entries = await self.player.command("get_property", "playlist")
            tracks = [
                self._playing_track_label(entry["filename"])
                for entry in entries[:100] if entry.get("filename")
            ]
            return f"Music queue: {', '.join(tracks) or 'empty'}."
        if action not in ("append", "next"):
            return "Choose to list the queue, add a song to the end, or play it next."
        _, paths = self._sources()
        matches = self._find_music("song", name, artist, album, paths)
        if len(matches) != 1:
            return "Give one selected song title and its artist or album if needed."
        track = matches[0]
        await self.player.command(
            "loadfile", str(track.path), "insert-next" if action == "next" else "append",
        )
        self.source = ("playlist", "Personal queue")
        return f"Queued {track.description()} {'next' if action == 'next' else 'at the end'}."

    async def _control(self, action):
        if action == "stop":
            await self.player.stop()
            self.source = None
            return "SC Radio stopped."
        if not self.player.running or not self.source:
            return "SC Radio is idle."
        kind, label = self.source
        online_playlist = kind in ("station_mix", "online_playlist")
        collection = online_playlist or kind in ("playlist", "album", "artist", "genre")
        if action == "status":
            paused = await self.player.command("get_property", "pause")
            state = "Paused" if paused else "Playing"
            if online_playlist:
                current = tag_text(await self.player.command("get_property", "media-title"))
                if current and "://" not in current:
                    return f"{state}: {label}, {current}."
            elif collection:
                current = await self.player.command("get_property", "path")
                if current:
                    return f"{state}: {label}, {self._playing_track_label(current)}."
            return f"{state}: {label}."
        if action in ("skip", "previous"):
            if not collection:
                return "Next and previous are available for music collections."
            position = await self.player.command("get_property", "playlist-pos")
            if action == "previous":
                if position == 0:
                    return "This is the first track in your playlist."
                await self.player.command("playlist-prev", "weak")
                return "Returned to the previous track."
            total = await self.player.command("get_property", "playlist-count")
            if position + 1 >= total:
                return "This is the last track in your playlist."
            await self.player.command("playlist-next", "weak")
            return "Skipped to the next track."
        if action in ("pause", "resume"):
            await self.player.command("set_property", "pause", action == "pause")
            return "SC Radio paused." if action == "pause" else "SC Radio resumed."
        if action in ("mute", "unmute"):
            await self.player.command("set_property", "mute", action == "mute")
            return "SC Radio muted." if action == "mute" else "SC Radio unmuted."
        if action == "restart":
            if kind == "station":
                return "A live station cannot restart from the beginning."
            await self.player.command("seek", 0, "absolute")
            return "Restarted the current song."
        if action in ("repeat_one", "repeat_all", "repeat_off"):
            if kind == "station" or (action == "repeat_all" and not collection):
                return "Repeat a selected song or a music collection."
            await self.player.command("set", "loop-file", "inf" if action == "repeat_one" else "no")
            await self.player.command("set", "loop-playlist", "inf" if action == "repeat_all" else "no")
            return {
                "repeat_one": "Repeating the current song.",
                "repeat_all": "Repeating this playlist.",
                "repeat_off": "Repeat is off.",
            }[action]
        return "Unknown playback control."
