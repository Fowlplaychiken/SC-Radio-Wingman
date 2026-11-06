"""Bounded public RSS audio source parsing and resolution."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

MAX_FEED_BYTES = 1_000_000
TIMEOUT_SECONDS = 8


def _public_https_url(url: str) -> bool:
    if not validate_feed_url(url):
        return False
    try:
        host = urlsplit(url).hostname
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (ValueError, OSError, UnicodeError):
        return False


def validate_feed_url(url: str) -> bool:
    """Cheap settings-time validation; DNS/IP safety is rechecked on every fetch."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in url
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
                or parsed.port not in (None, 443)):
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (ValueError, UnicodeError):
        return False


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_https_url(newurl):
            raise urllib.error.URLError("RSS redirect must remain on a public HTTPS host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_feed(url: str) -> bytes:
    if not _public_https_url(url):
        raise ValueError("RSS feed must use HTTPS and a public host.")
    opener = urllib.request.build_opener(_SafeRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "SC-Radio/0.2"})
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        if not _public_https_url(final_url):
            raise ValueError("RSS feed redirected to an unsafe address.")
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("RSS feed is larger than the 1 MB limit.")
    return data


class _FeedTreeBuilder(ET.TreeBuilder):
    def doctype(self, _name, _pubid, _system):
        raise ET.ParseError("RSS document types are unsupported")


def _publication_time(item):
    for child in item:
        if child.tag.rsplit("}", 1)[-1].lower() != "pubdate" or not child.text:
            continue
        try:
            date = parsedate_to_datetime(child.text)
            return (date if date.tzinfo is not None else date.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, OverflowError, OSError):
            return float("-inf")
    return float("-inf")


def latest_audio_enclosure(feed_url: str) -> str:
    """Return the newest RSS audio enclosure after validating public HTTPS targets."""
    try:
        root = ET.fromstring(_read_feed(feed_url), parser=ET.XMLParser(target=_FeedTreeBuilder()))
    except (ET.ParseError, OSError) as exc:
        raise ValueError("Could not read the RSS feed. Check that its public HTTPS URL is reachable.") from exc
    items = (item for item in root.iter() if item.tag.rsplit("}", 1)[-1].lower() == "item")
    for item in sorted(items, key=_publication_time, reverse=True)[:24]:
        for child in item:
            if child.tag.rsplit("}", 1)[-1].lower() != "enclosure":
                continue
            url = child.attrib.get("url", "")
            media_type = child.attrib.get("type", "").lower()
            if media_type.startswith("audio/") and _public_https_url(url):
                return url
    raise ValueError("No valid public HTTPS audio enclosure was found in the RSS feed.")
