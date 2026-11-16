"""Outbound HTTP for connectors: retries with backoff, a response size cap,
robots.txt, and SSRF protection (no requests to private or local addresses,
checked on every redirect hop)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchError(Exception):
    pass


class BlockedUrl(FetchError):
    pass


class RobotsDisallowed(FetchError):
    pass


@dataclass
class Response:
    status: int
    headers: httpx.Headers
    content: bytes
    url: str


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_url(url: str, resolver: Resolver = system_resolver) -> None:
    """Raise BlockedUrl unless the URL is http(s) to public addresses only."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedUrl(f"Only http and https URLs are allowed: {url}")
    host = parts.hostname
    if not host:
        raise BlockedUrl(f"URL has no host: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = [host] if _is_ip(host) else await resolver(host, port)
    except OSError as e:
        raise FetchError(f"Cannot resolve {host}: {e}") from e
    if not addresses:
        raise FetchError(f"Cannot resolve {host}")
    for addr in addresses:
        if not ipaddress.ip_address(addr.split("%")[0]).is_global:
            raise BlockedUrl(f"{host} resolves to a non-public address")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class RobotsCache:
    """robots.txt per host, cached for a day. Per RFC 9309: a missing file
    (4xx) allows everything; an unreachable one (5xx, network) disallows
    until the next try, ten minutes later."""

    def __init__(self, fetch: Callable[[str], Awaitable[Response]], agent: str) -> None:
        self._fetch = fetch
        self._agent = agent
        self._cache: dict[str, tuple[float, RobotFileParser | bool]] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        hit = self._cache.get(base)
        if hit is None or hit[0] < time.monotonic():
            hit = await self._load(base)
            self._cache[base] = hit
        rules = hit[1]
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self._agent, url)

    async def _load(self, base: str) -> tuple[float, RobotFileParser | bool]:
        now = time.monotonic()
        try:
            resp = await self._fetch(base + "/robots.txt")
        except BlockedUrl:
            raise
        except FetchError:
            return now + 600, False
        if 400 <= resp.status < 500:
            return now + 86400, True
        if resp.status != 200:
            return now + 600, False
        parser = RobotFileParser()
        parser.parse(resp.content.decode("utf-8", errors="replace").splitlines())
        return now + 86400, parser


class SafeHttp:
    def __init__(self, client: httpx.AsyncClient, user_agent: str,
                 resolver: Resolver = system_resolver, retries: int = 3) -> None:
        self._client = client
        self.user_agent = user_agent
        self._resolver = resolver
        self._retries = retries
        agent_token = user_agent.split("/")[0]
        self.robots = RobotsCache(lambda u: self.get(u, check_robots=False), agent_token)

    async def get(self, url: str, headers: dict[str, str] | None = None, check_robots: bool = True) -> Response:
        if check_robots and not await self.robots.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        last: str = ""
        for attempt in range(self._retries):
            if attempt:
                await asyncio.sleep(min(2 ** attempt, 30))
            try:
                resp = await self._get_following(url, headers or {})
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if resp.status == 429 or resp.status >= 500:
                last = f"HTTP {resp.status}"
                continue
            return resp
        raise FetchError(f"{url}: {last}")

    async def _get_following(self, url: str, headers: dict[str, str]) -> Response:
        h = {"User-Agent": self.user_agent, **headers}
        for _ in range(MAX_REDIRECTS + 1):
            await check_url(url, self._resolver)
            async with self._client.stream("GET", url, headers=h, follow_redirects=False, timeout=TIMEOUT) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    continue
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise FetchError(f"{url}: response larger than {MAX_BYTES} bytes")
                return Response(status=r.status_code, headers=r.headers, content=bytes(body), url=url)
        raise FetchError(f"{url}: too many redirects")
