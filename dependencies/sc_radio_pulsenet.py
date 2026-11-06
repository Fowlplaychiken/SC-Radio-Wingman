"""Read the broadcaster's public station definitions as data."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from http.client import HTTPException
from urllib import request


_CHANNEL_ID = "UCIMaIJsfJEMi5yJIe5nAb0g"
_CHANNEL_URL = f"https://www.youtube.com/channel/{_CHANNEL_ID}"
PULSENET_LIVE_URL = f"{_CHANNEL_URL}/live"
CATALOG_URL = "https://raw.githubusercontent.com/Diftic/PulseNet-Player/master/src/Renderer/stations.js"
CHANNEL_PLAYLISTS_URL = f"{_CHANNEL_URL}/playlists"
_PLACEHOLDER_PLAYLIST = "UUIMaIJsfJEMi5yJIe5nAb0g"
_TIMEOUT_SECONDS = 4
_REFRESH_SECONDS = 15 * 60
_RETRY_SECONDS = 60

_ROW = re.compile(r"\s*\{([^{}]*)\}\s*(?:,|$)")
_FIELD = re.compile(r"\s*(\w+)\s*:\s*('(?:[^'\\\r\n]|\\['\\])*'|\"(?:[^\"\\\r\n]|\\[\"\\])*\"|\w+)\s*(?:,|$)")
_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,128}")
_PLAYLIST_TITLE_SEPARATOR = re.compile(r"\s+[-–—]\s+")


def _playlist_url(playlist: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist}"


def parse_catalog(source: str) -> dict[str, str]:
    source = re.sub(r"^\s*//[^\r\n]*", "", source, flags=re.MULTILINE)
    array = re.fullmatch(r"\s*window\.STATIONS\s*=\s*\[(.*)\]\s*;?\s*", source, re.DOTALL)
    if array is None:
        raise ValueError("Unrecognized PulseNet station list")

    stations = {"PulseNet": PULSENET_LIVE_URL}
    names = {"pulsenet"}
    playlists = {_PLACEHOLDER_PLAYLIST}
    rows = array[1].strip()
    row_end = 0
    for row in _ROW.finditer(rows):
        if row.start() != row_end:
            raise ValueError("Unrecognized PulseNet station definition")
        row_end = row.end()
        fields = _station_fields(row[1].strip())
        label = fields.get("label", "").strip()
        playlist = fields.get("playlistId", "")
        if not 0 < len(label) <= 80 or label.casefold() in names:
            continue
        if not _PLAYLIST_ID.fullmatch(playlist) or playlist in playlists:
            continue
        stations[label] = _playlist_url(playlist)
        names.add(label.casefold())
        playlists.add(playlist)
    if row_end != len(rows):
        raise ValueError("Unrecognized PulseNet station definition")
    return stations


def _station_fields(source: str) -> dict[str, str]:
    fields = {}
    field_end = 0
    for field in _FIELD.finditer(source):
        key, value = field.groups()
        if field.start() != field_end or key in fields:
            raise ValueError("Unrecognized PulseNet station fields")
        field_end = field.end()
        fields[key] = re.sub(r"\\(['\"\\])", r"\1", value[1:-1]) if value[0] in "'\"" else value
    if field_end != len(source):
        raise ValueError("Unrecognized PulseNet station fields")
    return fields


def parse_channel_catalog(page: str) -> dict[str, str]:
    marker = re.search(r"\bytInitialData\s*=\s*(?=\{)", page)
    if marker is None:
        raise ValueError("Missing PulseNet channel data")

    try:
        data, _ = json.JSONDecoder().raw_decode(page, marker.end())
        channel = data["metadata"]["channelMetadataRenderer"]
        tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
        if channel["externalId"] != _CHANNEL_ID:
            raise ValueError("Unexpected PulseNet channel identity")
        playlist_tab = next(
            tab["tabRenderer"]["content"]
            for tab in tabs
            if tab.get("tabRenderer", {}).get("selected") is True
            and tab["tabRenderer"].get("title") == "Playlists"
        )
        if not isinstance(playlist_tab, dict):
            raise ValueError("Unrecognized PulseNet channel playlist content")
    except (KeyError, TypeError, AttributeError, StopIteration, RecursionError) as exc:
        raise ValueError("Unrecognized PulseNet channel playlist page") from exc

    candidates: dict[str, tuple[str, str]] = {}
    ambiguous = set()
    pending = [playlist_tab]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(reversed(node))
        elif isinstance(node, dict):
            lockup = node.get("lockupViewModel")
            if not isinstance(lockup, dict):
                pending.extend(node.values())
                continue
            if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_PLAYLIST":
                continue
            try:
                playlist = lockup["contentId"]
                title = lockup["metadata"]["lockupMetadataViewModel"]["title"]["content"]
            except (KeyError, TypeError):
                continue
            if not isinstance(playlist, str) or not _PLAYLIST_ID.fullmatch(playlist):
                continue
            if not isinstance(title, str) or playlist == _PLACEHOLDER_PLAYLIST:
                continue

            label, *description = _PLAYLIST_TITLE_SEPARATOR.split(title.strip(), maxsplit=1)
            name = label.casefold()
            if not 0 < len(label) <= 80 or not label.isprintable() or name == "pulsenet":
                continue
            if not description and not name.endswith(" station"):
                continue
            if name in candidates and candidates[name][1] != playlist:
                ambiguous.add(name)
            candidates[name] = (label, playlist)

    return {label: _playlist_url(playlist) for name, (label, playlist) in candidates.items() if name not in ambiguous}


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_text(url: str, max_bytes: int) -> str:
    opener = request.build_opener(_NoRedirect())
    req = request.Request(url, headers={"Accept-Language": "en-US,en;q=0.9"})
    with opener.open(req, timeout=_TIMEOUT_SECONDS) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("PulseNet station response is too large")
    return raw.decode("utf-8-sig")


def _fetch_catalog() -> dict[str, str]:
    return parse_catalog(_fetch_text(CATALOG_URL, 64 * 1024))


def _fetch_channel_catalog() -> dict[str, str]:
    return parse_channel_catalog(_fetch_text(CHANNEL_PLAYLISTS_URL, 2 * 1024 * 1024))


class PulseNetCatalog:
    def __init__(self) -> None:
        self._catalogs: dict[str, dict[str, str]] = {}
        self._refresh_after: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def stations(self) -> dict[str, str]:
        async with self._lock:
            await asyncio.gather(
                self._refresh("publisher", _fetch_catalog),
                self._refresh("channel", _fetch_channel_catalog),
            )
            stations = {"PulseNet": PULSENET_LIVE_URL} | self._catalogs.get("publisher", {})
            names = {label.casefold() for label in stations}
            urls = set(stations.values())
            for label, url in self._catalogs.get("channel", {}).items():
                if label.casefold() in names or url in urls:
                    continue
                stations[label] = url
                names.add(label.casefold())
                urls.add(url)
            return stations

    async def _refresh(self, source: str, fetch: Callable[[], dict[str, str]]) -> None:
        if time.monotonic() < self._refresh_after.get(source, 0.0):
            return
        try:
            self._catalogs[source] = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=_TIMEOUT_SECONDS)
        except (OSError, ValueError, HTTPException, asyncio.TimeoutError):
            delay = _RETRY_SECONDS
        else:
            delay = _REFRESH_SECONDS
        self._refresh_after[source] = time.monotonic() + delay
