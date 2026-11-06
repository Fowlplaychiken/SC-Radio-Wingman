import asyncio
import importlib.util
import os
import subprocess
import struct
import sys
import threading
import wave
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT


@pytest.fixture
def radio(monkeypatch, tmp_path):
    # Stock ModuleManager loads main.py by filename with a top-level module name.
    file_service = ModuleType("services.file")
    file_service.get_audio_library_dir = lambda: str(tmp_path)
    skill_module = ModuleType("skills.skill_base")

    class Skill:
        def __init__(self, config, settings, wingman):
            self.config = config
            self.unloaded = False

        def retrieve_custom_property_value(self, name, errors):
            return self.config.get(name)

        async def unload(self):
            self.unloaded = True

    skill_module.Skill = Skill
    monkeypatch.setitem(sys.modules, file_service.__name__, file_service)
    monkeypatch.setitem(sys.modules, skill_module.__name__, skill_module)
    monkeypatch.syspath_prepend(str(SKILL_DIR / "dependencies"))
    spec = importlib.util.spec_from_file_location("sc_radio", SKILL_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_station_config_and_validation(radio):
    config = yaml.safe_load((SKILL_DIR / "default_config.yaml").read_text(encoding="utf-8"))
    assert config["module"] == "skills.sc_radio.main"
    assert config["name"] == radio.ScRadio.__name__
    assert config["api_version"] == 3
    stations = next(
        item["value"] for item in config["custom_properties"] if item["id"] == "stations"
    )
    audio_config = next(
        item["value"] for item in config["custom_properties"] if item["id"] == "audio_config"
    )
    assert audio_config["volume"] == 0.5
    assert radio.stations_from(stations) == {
        "The Base": "https://listen.thebase.sc",
        "HCN Radio": "https://hcnradio.ddns.me/stream/1/",
    }
    assert radio.stations_from("Friend = https://example.com/listen?codec=mp3") == {
        "Friend": "https://example.com/listen?codec=mp3"
    }
    defaulted = radio.ScRadio({"audio_config": None}, None, None)
    assert defaulted._volume() == 50
    saved_muted = radio.ScRadio({"audio_config": SimpleNamespace(volume=0)}, None, None)
    assert saved_muted._volume() == 0
    for invalid in (
        "Bad = file:///etc/passwd",
        "Bad = http://example.com",
        "Bad = https://user:pass@example.com",
        "Bad = https://example.com:abcd",
        "A = https://example.com\na = https://example.net",
    ):
        with pytest.raises(ValueError):
            radio.stations_from(invalid)


def test_pulsenet_is_opt_in_and_uses_existing_player(radio, monkeypatch, tmp_path):
    live = "https://www.youtube.com/channel/UCIMaIJsfJEMi5yJIe5nAb0g/live"
    playlist = "https://www.youtube.com/playlist?list=PLExample"
    config = {"stations": "The Base = https://listen.thebase.sc", "audio_config": None}
    skill = radio.ScRadio(config, None, None)
    starts = []
    resolver = tmp_path / "yt-dlp.exe"

    async def catalogue():
        return {"PulseNet": live, "CrossWind": playlist}

    async def start(_executable, sources, volume, **options):
        starts.append((sources, volume, options))

    monkeypatch.setattr(skill.pulsenet, "stations", catalogue)
    monkeypatch.setattr(skill.player, "start", start)
    monkeypatch.setattr(radio.shutil, "which", lambda _name: None)

    async def run():
        assert "PulseNet" not in await skill._list("stations")
        config["pulsenet_enabled"] = True
        assert await skill._list("stations") == "Stations: The Base, PulseNet, CrossWind."
        assert "Install yt-dlp" in await skill._play("station", "PulseNet")
        assert not starts
        monkeypatch.setattr(radio.shutil, "which", lambda _name: str(resolver))
        assert await skill._play("station", "pulsenet") == "Playing PulseNet."
        assert await skill._play("station", "CrossWind") == "Playing CrossWind."
        assert await skill._play("station", "The Base") == "Playing The Base."
        assert starts == [
            ([live], 50, {"shuffle": False, "youtube_resolver": str(resolver)}),
            ([playlist], 50, {"shuffle": False, "youtube_resolver": str(resolver)}),
            (["https://listen.thebase.sc"], 50, {"shuffle": False}),
        ]

    asyncio.run(run())


def test_pulsenet_catalog_refresh_and_outage(radio, monkeypatch):
    import sc_radio_pulsenet as catalog

    placeholder = "UUIMaIJsfJEMi5yJIe5nAb0g"
    source = f"""window.STATIONS = [
        {{label: 'Placeholder', playlistId: '{placeholder}'}},
        {{label: 'CrossWind', playlistId: 'PLdistinct123', live: false}},
        {{label: 'Duplicate', playlistId: 'PLdistinct123'}},
        {{label: 'crosswind', playlistId: 'PLdifferent456'}},
    ];"""
    live = {"PulseNet": catalog.PULSENET_LIVE_URL}
    expected = live | {"CrossWind": "https://www.youtube.com/playlist?list=PLdistinct123"}
    assert catalog.parse_catalog(source) == expected
    with pytest.raises(ValueError):
        catalog.parse_catalog(source + "fetch('https://example.com');")

    responses = iter((expected, OSError("offline"), live))

    def fetch():
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(catalog, "_fetch_catalog", fetch)
    monkeypatch.setattr(catalog, "_REFRESH_SECONDS", 0)
    monkeypatch.setattr(catalog, "_RETRY_SECONDS", 0)

    async def run():
        stations = catalog.PulseNetCatalog()
        assert await stations.stations() == expected
        assert await stations.stations() == expected
        assert await stations.stations() == live

    asyncio.run(run())


def test_large_station_catalogue_lists_in_pages(radio):
    configured = "\n".join(f"Station {index:03} = https://example.com/{index}" for index in range(101))
    assert len(radio.stations_from(configured)) == 101
    skill = radio.ScRadio({"stations": configured, "audio_config": None}, None, None)
    try:
        result, _ = asyncio.run(skill.execute_tool("sc_radio_list", {"kind": "stations"}, None))
        assert "Station 031" in result and "Station 032" not in result
        assert "Next station offset: 32" in result
        result, _ = asyncio.run(skill.execute_tool("sc_radio_list", {"kind": "stations", "station_offset": 96}, None))
        assert "Station 100" in result and "Next station offset" not in result
    finally:
        asyncio.run(skill.unload())


@pytest.mark.skipif(os.name != "nt", reason="Windows player discovery")
def test_windows_player_prefers_existing_and_checks_managed_hash(radio, tmp_path, monkeypatch):
    for name in ("ProgramFiles", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / name))
    managed = tmp_path / "LOCALAPPDATA" / "SC Radio/mpv-0.42.0-x64/mpv.exe"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"not the pinned player")
    with pytest.raises(radio.PlaybackError, match="failed verification"):
        radio.player_executable("")

    existing = tmp_path / "ProgramFiles" / "mpv/mpv.exe"
    existing.parent.mkdir(parents=True)
    existing.touch()
    assert radio.player_executable("") == existing
    assert radio.player_executable(f'"{existing}"') == existing


def test_library_files_stay_inside_selection_and_library(radio, tmp_path):
    directory = tmp_path / "music"
    directory.mkdir()
    song = directory / "song.wav"
    song.touch()
    config = SimpleNamespace(
        files=[
            SimpleNamespace(path="music", name="song.wav"),
            SimpleNamespace(path=str(directory), name="song.wav"),
            SimpleNamespace(path="music", name="../song.wav"),
            SimpleNamespace(path="../music", name="song.wav"),
            SimpleNamespace(path="music", name="not-present.mp3"),
        ]
    )
    assert radio.library_tracks(tmp_path, config) == [song]


def test_skill_controls_one_owned_session_and_unloads(radio, tmp_path):
    class Player:
        running = False

        def __init__(self):
            self.starts, self.commands = [], []
            self.stops = 0
            self.loop_ids = []

        def record_loop(self):
            self.loop_ids.append(id(asyncio.get_running_loop()))

        async def start(self, executable, sources, volume, *, shuffle=False):
            self.record_loop()
            if self.running:
                await self.stop()
            self.starts.append((executable, sources, volume))
            self.shuffle = shuffle
            if sources[0] == "https://offline.example/":
                raise radio.PlaybackError("Audio could not start.")
            self.running = True

        async def stop(self):
            self.record_loop()
            self.running = False
            self.stops += 1

        async def command(self, *args):
            self.record_loop()
            self.commands.append(args)
            if args == ("get_property", "playlist-pos"):
                return int(("playlist-next", "weak") in self.commands)
            if args == ("get_property", "playlist-count"):
                return 2
            if args == ("get_property", "playlist"):
                return [{"filename": str(first)}, {"filename": str(second)}]

    first, second = tmp_path / "one.wav", tmp_path / "two.mp3"
    first.touch()
    second.touch()
    skill = radio.ScRadio(
        {
            "stations": "The Base = https://listen.thebase.sc\nOffline = https://offline.example/",
            "player_path": "owner path",
            "audio_config": SimpleNamespace(
                volume=0.25,
                files=[SimpleNamespace(path="", name=track.name) for track in (first, second)],
            ),
        },
        None,
        None,
    )
    skill.player = Player()

    def call(tool, **args):
        result, speech = asyncio.run(
            skill.execute_tool(f"sc_radio_{tool}", args, None)
        )
        assert not speech
        return result

    assert "one.wav" in call("list")
    assert "not found" in call("play", kind="station", name="https://example.net/")
    assert "Playing The Base" in call("play", kind="station", name="the base")
    assert skill.player.starts[-1][1:] == (["https://listen.thebase.sc"], 25)
    assert "playing: the base" in call("control", action="status").lower()
    assert "paused" in call("control", action="pause")
    assert "resumed" in call("control", action="resume")
    assert "15%" in call("volume", percent=15)
    assert "integer" in call("volume", percent=True)
    assert "Personal playlist" in call("play", kind="playlist")
    assert skill.player.starts[-1][1:] == ([str(first), str(second)], 15)
    assert skill.player.stops == 1
    assert "Skipped" in call("control", action="skip")
    assert skill.player.commands[-1] == ("playlist-next", "weak")
    assert "previous" in call("control", action="previous")
    assert skill.player.commands[-1] == ("playlist-prev", "weak")
    assert "playlist" in call("control", action="repeat_all")
    assert skill.player.commands[-1] == ("set", "loop-playlist", "inf")
    assert "current song" in call("control", action="repeat_one")
    assert skill.player.commands[-2:] == [("set", "loop-file", "inf"), ("set", "loop-playlist", "no")]
    assert "Repeat is off" in call("control", action="repeat_off")
    assert "Restarted" in call("control", action="restart")
    assert skill.player.commands[-1] == ("seek", 0, "absolute")
    assert "muted" in call("control", action="mute")
    assert skill.player.commands[-1] == ("set_property", "mute", True)
    assert "unmuted" in call("control", action="unmute")
    assert skill.player.commands[-1] == ("set_property", "mute", False)
    assert "25%" in call("volume", change=10)
    assert "one relative change" in call("volume", percent=20, change=10)
    assert "Shuffled personal playlist" in call("play", kind="playlist", shuffle=True)
    assert skill.player.shuffle is True
    assert skill.player.starts[-1][1:] == ([str(first), str(second)], 25)
    assert "only" in call("play", kind="station", name="The Base", shuffle=True).lower()
    assert "Playing two.mp3" in call("play", kind="song", name="two")
    assert skill.player.shuffle is False
    assert "Queued one.wav next" in call("queue", action="next", name="one")
    assert skill.player.commands[-1] == ("loadfile", str(first), "insert-next")
    assert "Music queue: one.wav, two.mp3" in call("queue", action="show")
    assert "could not start" in call("play", kind="station", name="Offline")
    assert "idle" in call("control", action="status")
    assert "Playing The Base" in call("play", kind="station", name="The Base")
    owner_loop_ids = set(skill.player.loop_ids)
    assert len(owner_loop_ids) == 1
    asyncio.run(skill.unload())
    assert skill.unloaded and not skill.player.running
    assert not any(
        thread.name == "sc-radio-owner" and thread.is_alive()
        for thread in threading.enumerate()
    )
    assert "disabled" in call("play", kind="station", name="The Base")


def tagged_wav(path, *, title, artist, album, number, genre="Space Folk"):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0" * 160)
    raw = path.read_bytes()

    def chunk(kind, data):
        return kind + struct.pack("<I", len(data)) + data + b"\0" * (len(data) % 2)

    tags = (b"INAM", title), (b"IART", artist), (b"IPRD", album), (b"ITRK", str(number)), (b"IGNR", genre)
    info = chunk(b"LIST", b"INFO" + b"".join(
        chunk(key, value.encode("utf-8") + b"\0") for key, value in tags
    ))
    path.write_bytes(raw[:4] + struct.pack("<I", len(raw) + len(info) - 8) + raw[8:] + info)


def test_tagged_album_artist_and_duplicate_titles(radio, tmp_path):
    first, second, other = (tmp_path / name for name in ("a.wav", "b.wav", "c.wav"))
    tagged_wav(first, title="Far Horizon", artist="The Crew", album="Homebound", number=2)
    tagged_wav(second, title="First Watch", artist="The Crew", album="Homebound", number=1)
    tagged_wav(other, title="Far Horizon", artist="Another Artist", album="Homebound", number=1)
    skill = radio.ScRadio({
        "stations": "The Base = https://listen.thebase.sc",
        "player_path": "owner path",
        "audio_config": SimpleNamespace(
            volume=0.25,
            files=[SimpleNamespace(path="", name=p.name) for p in (first, other, second)],
        ),
    }, None, None)
    starts = []

    async def start(executable, sources, volume, *, shuffle=False):
        starts.append((sources, shuffle))

    async def stop():
        pass

    skill.player = SimpleNamespace(start=start, stop=stop)
    def call(tool, **args):
        return asyncio.run(skill.execute_tool(f"sc_radio_{tool}", args, None))[0]

    try:
        assert "The Crew" in call("list", kind="artists")
        assert "Homebound" in call("list", kind="albums")
        assert "Space Folk" in call("list", kind="genres")
        assert "Far Horizon — The Crew (Homebound)" in call("list", kind="songs")
        assert "Several songs" in call("play", kind="song", name="Far Horizon")
        assert "Several artists" in call("play", kind="album", name="Homebound")
        assert not starts
        assert "Far Horizon" in call("play", kind="song", name="far horizon", artist="The Crew")
        assert starts[-1] == ([str(first)], False)
        assert "Homebound" in call("play", kind="album", name="homebound", artist="The Crew")
        assert starts[-1] == ([str(second), str(first)], False)
        assert "Shuffled The Crew" in call("play", kind="artist", name="the crew", shuffle=True)
        assert starts[-1] == ([str(first), str(second)], True)
        assert "Space Folk" in call("play", kind="genre", name="Space Folk")
        assert starts[-1] == ([str(first), str(other), str(second)], False)
    finally:
        asyncio.run(skill.unload())


def test_player_only_runs_selected_executable_and_kills_failed_start(radio, tmp_path, monkeypatch):
    executable = tmp_path / ("mpv.exe" if sys.platform == "win32" else "mpv")
    executable.touch()
    spawned = []

    class Process:
        returncode = None
        pid = 1234

        def kill(self):
            self.returncode = -1

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        process = Process()
        spawned.append((args, kwargs, process))
        return process

    async def failed_start(_address):
        raise radio.PlaybackError("No audio opened.")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(radio, "_windows_job_for_pid", lambda _pid: None)
    player = radio.Player()
    monkeypatch.setattr(player, "_wait_for_audio", failed_start)

    async def run():
        with pytest.raises(radio.PlaybackError, match="absolute path"):
            await player.start("mpv", ["https://example.com"], 25)
        assert not spawned
        with pytest.raises(radio.PlaybackError, match="could not start"):
            await player.start(str(executable), ["https://example.com"], 25)
        args, kwargs, process = spawned[0]
        assert args[0] == str(executable) and args[-2:] == ("--", "https://example.com")
        assert "--ytdl=no" in args and "--tls-verify=yes" in args and "shell" not in kwargs
        assert "--shuffle" not in args
        assert process.returncode == -1 and not player.running
        with pytest.raises(radio.PlaybackError, match="could not start"):
            await player.start(str(executable), ["one.wav", "two.wav"], 25, shuffle=True)
        shuffled_args = spawned[1][0]
        assert "--shuffle" in shuffled_args[:-3]
        assert shuffled_args[-3:] == ("--", "one.wav", "two.wav")
        with pytest.raises(radio.PlaybackError, match="PulseNet audio could not start"):
            await player.start(str(executable), ["https://www.youtube.com/"], 25, youtube_resolver="yt-dlp")
        youtube_args, _, youtube_process = spawned[2]
        assert "--ytdl=yes" in youtube_args and "--ytdl=no" not in youtube_args
        assert "--no-video" in youtube_args and "--force-window=no" in youtube_args
        assert "--script-opt=ytdl_hook-ytdl_path=yt-dlp" in youtube_args
        assert youtube_process.returncode == -1 and not player.running

    asyncio.run(run())


def test_windows_job_binding_failure_kills_and_reaps_child(radio, monkeypatch):
    monkeypatch.setattr(radio.os, "name", "nt")
    monkeypatch.setattr(radio.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(radio, "player_executable", lambda _configured: "mpv.exe")
    spawned = []

    class Process:
        returncode = None
        pid = 1234

        def kill(self):
            self.returncode = -1

        async def wait(self):
            return self.returncode

    async def spawn(*_args, **_kwargs):
        process = Process()
        spawned.append(process)
        return process

    def fail_binding(_pid):
        raise OSError("job assignment denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(radio, "_windows_job_for_pid", fail_binding)
    player = radio.Player()

    async def run():
        with pytest.raises(radio.PlaybackError, match="could not start"):
            await player.start("", ["https://example.com"], 50)
        assert spawned[0].returncode == -1
        assert player.process is None and player.job_handle is None

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_windows_job_handle_close_terminates_benign_child(radio):
    async def run():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        job = radio._windows_job_for_pid(process.pid)
        radio._close_windows_handle(job)
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.returncode is not None

    asyncio.run(run())


def test_player_correlates_ipc_response_and_reports_disconnect(radio):
    class Writer:
        message = None

        def write(self, message):
            self.message = message

        async def drain(self):
            pass

    async def run():
        player = radio.Player()
        player.process = SimpleNamespace(returncode=None)
        player.writer = Writer()
        player.reader = asyncio.StreamReader()
        player.reader.feed_data(
            b'{"event":"start-file"}\n'
            b'{"request_id":0,"error":"success"}\n'
            b'{"request_id":1,"error":"success","data":{"samplerate":44100}}\n'
        )
        assert await player.command("get_property", "audio-params") == {"samplerate": 44100}
        assert player.writer.message == (
            b'{"command": ["get_property", "audio-params"], "request_id": 1}\n'
        )
        player.reader.feed_eof()
        with pytest.raises(radio.PlaybackError, match="did not respond"):
            await player.command("get_property", "pause")

    asyncio.run(run())
