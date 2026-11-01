"""A fake camera that authenticates like an Axis device (Basic + Digest offered).

Shared by the auth-negotiation tests (#69 CAM-S1, CAM-P1). The Digest check is
computed here independently of :mod:`timelapse_manager.cameras.http_auth`
(plain :mod:`hashlib`, RFC 7616), so a passing test means the client's header
is what a real server accepts, not that the client agrees with itself.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable

import httpx

from tests.conftest import make_jpeg

REALM = "AXIS_ACCC8E000000"
_PARAM_RE = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))')


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324


def parse_authorization(header: str) -> dict[str, str]:
    """Split a ``Digest k="v", ...`` header into its parameters."""
    return {
        m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
        for m in _PARAM_RE.finditer(header.partition(" ")[2])
    }


class FakeAxisCamera:
    """Answers like an Axis camera: ``401`` with Basic and Digest challenges.

    :param username, password: the account the camera accepts.
    :param basic_first: order of the two ``WWW-Authenticate`` lines.
    :param merged: send both challenges in one header line instead of two.
    :param offer_digest: ``False`` models a Basic-only device.
    :param body: what a successful request returns.
    """

    def __init__(
        self,
        username: str = "timelapse",
        password: str = "s3cret-pw",
        *,
        basic_first: bool = True,
        merged: bool = False,
        offer_digest: bool = True,
        offer_basic: bool = True,
        body: Callable[[httpx.Request], bytes] | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.basic_first = basic_first
        self.merged = merged
        self.offer_digest = offer_digest
        self.offer_basic = offer_basic
        self._body = body or (lambda _request: make_jpeg(64, 48))
        # Like a real device, every challenge carries a fresh nonce; each one
        # stays valid until rotate_nonce() retires it.
        self.nonce = ""
        self._issued = 0
        self.valid_nonces: set[str] = set()
        self.stale_nonces: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.accepted: list[str] = []  # "digest" / "basic", per success
        self._last_nc: dict[str, int] = {}

    # -- behaviour switches --------------------------------------------------

    def rotate_nonce(self, *, stale: bool = True) -> None:
        """Retire every nonce issued so far (rejected ``stale=true`` if set)."""
        if stale:
            self.stale_nonces |= self.valid_nonces
        self.valid_nonces.clear()

    # -- transport -----------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        """A MockTransport serving this camera."""
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        header = request.headers.get("authorization")
        if header is not None:
            scheme = header.split(" ", 1)[0].lower()
            if scheme == "digest" and self.offer_digest:
                verdict = self._check_digest(request, header)
                if verdict == "ok":
                    self.accepted.append("digest")
                    return httpx.Response(200, content=self._body(request))
                return self._challenge(stale=verdict == "stale")
            if scheme == "basic" and self.offer_basic and self._check_basic(header):
                self.accepted.append("basic")
                return httpx.Response(200, content=self._body(request))
        return self._challenge()

    def _challenge(self, *, stale: bool = False) -> httpx.Response:
        lines: list[str] = []
        self._issued += 1
        self.nonce = f"nonce-{self._issued}"
        self.valid_nonces.add(self.nonce)
        if self.offer_digest:
            digest = (
                f'Digest realm="{REALM}", nonce="{self.nonce}", '
                f'algorithm=MD5, qop="auth"'
            )
            if stale:
                digest += ", stale=true"
            lines.append(digest)
        if self.offer_basic:
            basic = f'Basic realm="{REALM}"'
            if self.basic_first:
                lines.insert(0, basic)
            else:
                lines.append(basic)
        if self.merged:
            lines = [", ".join(lines)]
        return httpx.Response(
            401, headers=[("WWW-Authenticate", line) for line in lines]
        )

    def _check_basic(self, header: str) -> bool:
        expected = base64.b64encode(f"{self.username}:{self.password}".encode()).decode(
            "ascii"
        )
        return header == f"Basic {expected}"

    def _check_digest(self, request: httpx.Request, header: str) -> str:
        params = parse_authorization(header)
        nonce = params.get("nonce", "")
        if nonce in self.stale_nonces:
            return "stale"
        if nonce not in self.valid_nonces or params.get("username") != self.username:
            return "bad"
        uri = params.get("uri", "")
        if uri != request.url.raw_path.decode("ascii"):
            return "bad"
        nc_text = params.get("nc", "")
        nc = int(nc_text, 16) if nc_text else 0
        # A reused or rewound nonce count is a replay.
        if nc <= self._last_nc.get(nonce, 0):
            return "bad"
        ha1 = _md5(f"{self.username}:{REALM}:{self.password}")
        ha2 = _md5(f"{request.method}:{uri}")
        expected = _md5(
            f"{ha1}:{nonce}:{nc_text}:{params.get('cnonce', '')}:auth:{ha2}"
        )
        if params.get("response") != expected or params.get("qop") != "auth":
            return "bad"
        self._last_nc[nonce] = nc
        return "ok"
