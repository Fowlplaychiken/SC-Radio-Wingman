"""Read the broadcaster's public station definitions as data."""

from __future__ import annotations

import asyncio
import re
import time
from http.client import HTTPException
from urllib import request


PULSENET_LIVE_URL = "https://www.youtube.com/channel/UCIMaIJsfJEMi5yJIe5nAb0g/live"
CATALOG_URL = "https://raw.githubusercontent.com/Diftic/PulseNet-Player/master/src/Renderer/stations.js"
_PLACEHOLDER_PLAYLIST = "UUIMaIJsfJEMi5yJIe5nAb0g"
_MAX_BYTES = 64 * 1024
_TIMEOUT_SECONDS = 4
_REFRESH_SECONDS = 15 * 60
_RETRY_SECONDS = 60

_ROW = re.compile(r"\s*\{([^{}]*)\}\s*(?:,|$)")
_FIELD = re.compile(r"\s*(\w+)\s*:\s*('(?:[^'\\\r\n]|\\['\\])*'|\"(?:[^\"\\\r\n]|\\[\"\\])*\"|\w+)\s*(?:,|$)")
_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,128}")


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
        stations[label] = f"https://www.youtube.com/playlist?list={playlist}"
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


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_catalog() -> dict[str, str]:
    opener = request.build_opener(_NoRedirect())
    with opener.open(CATALOG_URL, timeout=_TIMEOUT_SECONDS) as response:
        raw = response.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("PulseNet station list is too large")
    return parse_catalog(raw.decode("utf-8-sig"))


class PulseNetCatalog:
    def __init__(self) -> None:
        self._stations = {"PulseNet": PULSENET_LIVE_URL}
        self._refresh_after = 0.0
        self._lock = asyncio.Lock()

    async def stations(self) -> dict[str, str]:
        async with self._lock:
            if time.monotonic() >= self._refresh_after:
                try:
                    self._stations = await asyncio.wait_for(
                        asyncio.to_thread(_fetch_catalog), timeout=_TIMEOUT_SECONDS
                    )
                except (OSError, ValueError, HTTPException, asyncio.TimeoutError):
                    self._refresh_after = time.monotonic() + _RETRY_SECONDS
                else:
                    self._refresh_after = time.monotonic() + _REFRESH_SECONDS
            return self._stations.copy()
