"""HTTP authentication for camera requests: challenge parsing, scheme choice, reuse.

Every camera request that a ``401`` can answer (the snapshot ``GET``, the VAPIX
CGIs and SOAP ``POST``, the VAPIX WebSocket upgrade) negotiates through one
:class:`CameraAuthState` (#69 CAM-S1, CAM-P1):

* **Parsing.** :func:`parse_challenges` reads *every* ``WWW-Authenticate``
  value, whether the camera sends one header per scheme or several challenges
  merged into one header. The grammar is RFC 9110 section 11.6.1. A challenge
  this parser cannot read unambiguously -- a repeated parameter, a control
  character, an unterminated quoted string -- is marked invalid and never used,
  so a parser differential cannot make the client answer a challenge the camera
  did not mean.
* **Choice.** Digest beats Basic. Among usable Digest challenges SHA-256 beats
  MD5. Basic is used only when the camera offered **no** Digest challenge at
  all (a Digest offer this client cannot answer is refused rather than
  "downgraded" to Basic), and only where :class:`BasicPolicy` allows it: over
  ``https://``, or over plain ``http://`` for a camera that opted in. Any other
  scheme (Negotiate, NTLM, ...) is refused with
  :class:`UnsupportedAuthSchemeError`.
* **Reuse.** The state remembers the negotiated scheme per origin. The next
  request to that origin carries an ``Authorization`` header up front: Digest
  with the remembered nonce and the next nonce count, or Basic. One request per
  snapshot instead of three. A ``401`` on that request is answered from its own
  fresh challenge, once (a stale or expired nonce), so reuse never costs a
  capture.

The capture path keeps one state per camera in its session
(:mod:`timelapse_manager.capture.camera_sessions`); probes and adapters built
without one get a fresh state, so nothing is shared between unrelated callers.

Secrets: the password is only ever hashed (Digest) or base64-encoded into the
header (Basic). Neither the header nor the challenge's nonce is logged or put in
an error message; messages name schemes only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from .base import AuthCaptureError

__all__ = [
    "BASIC_OVER_HTTP_REFUSED",
    "BasicPolicy",
    "BasicRefusedError",
    "CameraAuthState",
    "Challenge",
    "DigestChallenge",
    "UnsupportedAuthSchemeError",
    "digest_authorization",
    "parse_challenges",
]

#: Why a plain-HTTP Basic offer was refused for a saved camera (owner decision
#: OD-3). Names the two ways out.
BASIC_OVER_HTTP_REFUSED: Final = (
    "camera offered only Basic authentication over plain HTTP, which would send "
    "the password readable on the network; use an https:// address or allow "
    "Basic over HTTP for this camera"
)
#: The same refusal for a probe (Detect, Query, discovery), which has no
#: per-camera flag to tick.
_BASIC_OVER_HTTP_REFUSED_PROBE: Final = (
    "camera offered only Basic authentication over plain HTTP, which would send "
    "the password readable on the network; use an https:// address (a saved "
    "camera can allow Basic over HTTP)"
)

# RFC 9110 token characters.
_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_SCHEME_RE = re.compile(rf"^({_TOKEN})(?:[ \t]+(.*))?$", re.DOTALL)
_PARAM_RE = re.compile(rf"^({_TOKEN})[ \t]*=[ \t]*(.*)$", re.DOTALL)
_TOKEN_VALUE_RE = re.compile(rf"^{_TOKEN}$")
_TOKEN68_RE = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")
# Control characters other than horizontal tab, and DEL. A value carrying one
# could split a header line when echoed back.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

# The Digest algorithms this client computes, strongest first within a family.
_DIGEST_HASHES: Final[Mapping[str, Callable[[bytes], Any]]] = {
    "MD5": hashlib.md5,
    "MD5-SESS": hashlib.md5,
    "SHA-256": hashlib.sha256,
    "SHA-256-SESS": hashlib.sha256,
}
_DIGEST_STRENGTH: Final[Mapping[str, int]] = {
    "MD5": 1,
    "MD5-SESS": 1,
    "SHA-256": 2,
    "SHA-256-SESS": 2,
}


class BasicRefusedError(AuthCaptureError):
    """Basic authentication was offered where the policy refuses to send it.

    Raised before any credential leaves this server. A probe surfaces its
    message, which says how to fix the camera (owner decision OD-3).
    """


class UnsupportedAuthSchemeError(AuthCaptureError):
    """The camera asked for an authentication scheme this client does not speak.

    An :class:`AuthCaptureError`, so a capture classifies it as an auth failure.
    The VAPIX event stream catches it to fall back to ONVIF PullPoint instead.
    """


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Challenge:
    """One parsed authentication challenge.

    :param scheme: the auth-scheme, lower-cased (``"digest"``, ``"basic"``).
    :param params: the auth-params, names lower-cased, values unquoted.
    :param token68: the token68 form's value (``Negotiate abc==``), if any.
    :param valid: ``False`` when the challenge could not be read unambiguously
        (a repeated parameter, a control character, bad quoting). An invalid
        challenge still counts as *offered* for the scheme choice, but is never
        answered.
    """

    scheme: str
    params: Mapping[str, str] = field(default_factory=dict)
    token68: str | None = None
    valid: bool = True


def _split_elements(value: str) -> list[str] | None:
    """Split a header value at top-level commas, respecting quoted strings.

    Returns ``None`` when a quoted string is unterminated: nothing after the
    open quote can be attributed safely.
    """
    elements: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for char in value:
        if in_quotes:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quotes = False
            continue
        if char == ",":
            elements.append("".join(current).strip())
            current = []
            continue
        if char == '"':
            in_quotes = True
        current.append(char)
    if in_quotes:
        return None
    elements.append("".join(current).strip())
    return elements


def _unquote(raw: str) -> str | None:
    """Return a parameter value, unquoted; ``None`` when it is malformed."""
    if raw.startswith('"'):
        if len(raw) < 2 or not raw.endswith('"'):
            return None
        out: list[str] = []
        escaped = False
        for char in raw[1:-1]:
            if escaped:
                out.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                # An unescaped quote inside: trailing text after the string.
                return None
            else:
                out.append(char)
        if escaped:
            return None
        return "".join(out)
    if _TOKEN_VALUE_RE.match(raw):
        return raw
    return None


class _Builder:
    """Accumulates one challenge while :func:`parse_challenges` walks elements."""

    def __init__(self, scheme: str) -> None:
        self.scheme = scheme.lower()
        self.params: dict[str, str] = {}
        self.token68: str | None = None
        self.valid = True

    def add_param(self, name: str, raw_value: str) -> None:
        key = name.lower()
        value = _unquote(raw_value.strip())
        if value is None or key in self.params or self.token68 is not None:
            self.valid = False
            return
        if _CONTROL_RE.search(value):
            self.valid = False
            return
        self.params[key] = value

    def build(self) -> Challenge:
        return Challenge(
            scheme=self.scheme,
            params=dict(self.params),
            token68=self.token68,
            valid=self.valid,
        )


def _is_param(text: str) -> re.Match[str] | None:
    """Match ``name=value`` unless the text is a token68 (``abc==``)."""
    match = _PARAM_RE.match(text)
    if match is None:
        return None
    if _TOKEN68_RE.match(text) and set(match.group(2)) <= {"="}:
        return None
    return match


def parse_challenges(headers: Iterable[str]) -> list[Challenge]:
    """Parse every challenge in a response's ``WWW-Authenticate`` values.

    Accepts the header's values as a list (``response.headers.get_list(...)``):
    one value per header line, each holding one or more comma-separated
    challenges. Unparseable text never produces a usable challenge.
    """
    challenges: list[Challenge] = []
    for value in headers:
        current: _Builder | None = None
        elements = _split_elements(value)
        if elements is None:
            # An unterminated quoted string: nothing in this value can be
            # attributed safely. Record it as offered but unusable, so it still
            # blocks a Basic fallback (see CameraAuthState.respond).
            head = _SCHEME_RE.match(value.strip().split(",", 1)[0].strip())
            scheme_name = head.group(1).lower() if head is not None else ""
            challenges.append(Challenge(scheme=scheme_name, valid=False))
            continue
        for element in elements:
            if not element:
                continue
            param = _is_param(element)
            if param is not None:
                if current is not None:
                    current.add_param(param.group(1), param.group(2))
                else:
                    # A parameter before any scheme: the value is not a
                    # challenge list this parser can read.
                    current = _Builder("")
                    current.valid = False
                continue
            scheme = _SCHEME_RE.match(element)
            if scheme is None:
                if current is not None:
                    current.valid = False
                continue
            if current is not None:
                challenges.append(current.build())
            current = _Builder(scheme.group(1))
            rest = (scheme.group(2) or "").strip()
            if not rest:
                continue
            first = _is_param(rest)
            if first is not None:
                current.add_param(first.group(1), first.group(2))
            elif _TOKEN68_RE.match(rest):
                current.token68 = rest
            else:
                current.valid = False
        if current is not None:
            challenges.append(current.build())
    return challenges


@dataclass(frozen=True)
class DigestChallenge:
    """A Digest challenge this client can answer.

    :param algorithm: canonical upper-case name, a key of the supported set.
    :param qop_auth: the camera offered ``qop=auth`` (RFC 7616); ``False`` means
        the legacy RFC 2069 form without ``qop``.
    """

    realm: str
    nonce: str
    algorithm: str
    opaque: str | None
    qop_auth: bool

    @property
    def strength(self) -> int:
        """Higher is stronger; used to pick among several Digest offers."""
        return _DIGEST_STRENGTH[self.algorithm]

    @classmethod
    def from_challenge(cls, challenge: Challenge) -> DigestChallenge | None:
        """Return the usable Digest challenge, or ``None`` when unanswerable."""
        if challenge.scheme != "digest" or not challenge.valid:
            return None
        params = challenge.params
        realm = params.get("realm")
        nonce = params.get("nonce")
        if realm is None or not nonce:
            return None
        algorithm = params.get("algorithm", "MD5").upper()
        if algorithm not in _DIGEST_HASHES:
            return None
        qop_raw = params.get("qop")
        qop_auth = False
        if qop_raw is not None:
            offered = {q.strip().lower() for q in re.split(r"[,\s]+", qop_raw) if q}
            if "auth" not in offered:
                return None  # only auth-int, which this client does not compute
            qop_auth = True
        if algorithm.endswith("-SESS") and not qop_auth:
            return None  # a session algorithm needs the cnonce that qop carries
        return cls(
            realm=realm,
            nonce=nonce,
            algorithm=algorithm,
            opaque=params.get("opaque"),
            qop_auth=qop_auth,
        )


# ---------------------------------------------------------------------------
# Header computation
# ---------------------------------------------------------------------------


def _quote(value: str) -> str:
    """Return ``value`` as an RFC 9110 quoted-string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _new_cnonce() -> str:
    """Return a fresh, unpredictable client nonce."""
    return os.urandom(16).hex()


def digest_authorization(
    challenge: DigestChallenge,
    credentials: tuple[str, str],
    *,
    method: str,
    uri: str,
    nonce_count: int,
    cnonce: str | None = None,
) -> str:
    """Compute an RFC 7616 Digest ``Authorization`` header value.

    :param uri: the request-target exactly as sent on the request line.
    :param nonce_count: how many requests (this one included) used the nonce.
    :param cnonce: the client nonce; a fresh random one when omitted.
    :raises AuthCaptureError: the username holds a control character, which a
        quoted header value cannot carry.
    """
    username, password = credentials
    if _CONTROL_RE.search(username):
        raise AuthCaptureError(
            "camera username contains a control character Digest cannot send"
        )
    hash_func = _DIGEST_HASHES[challenge.algorithm]

    def h(text: str) -> str:
        return str(hash_func(text.encode("utf-8")).hexdigest())

    cnonce_value = cnonce if cnonce is not None else _new_cnonce()
    ha1 = h(f"{username}:{challenge.realm}:{password}")
    if challenge.algorithm.endswith("-SESS"):
        ha1 = h(f"{ha1}:{challenge.nonce}:{cnonce_value}")
    ha2 = h(f"{method.upper()}:{uri}")
    nc = f"{nonce_count:08x}"
    if challenge.qop_auth:
        response = h(f"{ha1}:{challenge.nonce}:{nc}:{cnonce_value}:auth:{ha2}")
    else:
        response = h(f"{ha1}:{challenge.nonce}:{ha2}")
    parts = [
        f"username={_quote(username)}",
        f"realm={_quote(challenge.realm)}",
        f"nonce={_quote(challenge.nonce)}",
        f"uri={_quote(uri)}",
        f"algorithm={challenge.algorithm}",
        f"response={_quote(response)}",
    ]
    if challenge.opaque is not None:
        parts.append(f"opaque={_quote(challenge.opaque)}")
    if challenge.qop_auth:
        parts += ["qop=auth", f"nc={nc}", f"cnonce={_quote(cnonce_value)}"]
    return "Digest " + ", ".join(parts)


def basic_authorization(credentials: tuple[str, str]) -> str:
    """Compute an HTTP Basic ``Authorization`` header value (RFC 7617, UTF-8)."""
    username, password = credentials
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


# ---------------------------------------------------------------------------
# Policy and per-camera state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BasicPolicy:
    """Where Basic authentication may be sent (owner decision OD-3).

    Basic carries the password, merely base64-encoded, so over plain HTTP anyone
    on the path reads it. It is always allowed over ``https://``; over
    ``http://`` only for a saved camera whose ``allow_basic_over_http`` flag is
    set. A probe has no flag, so it never sends Basic over plain HTTP.
    """

    allow_over_http: bool = False
    probe: bool = True

    @classmethod
    def for_camera(cls, allow_basic_over_http: bool) -> BasicPolicy:
        """The policy for a saved camera with the given opt-in flag."""
        return cls(allow_over_http=bool(allow_basic_over_http), probe=False)

    @classmethod
    def for_probe(cls) -> BasicPolicy:
        """The policy for an ad-hoc probe: never Basic over plain HTTP."""
        return cls(allow_over_http=False, probe=True)

    def permits(self, url: httpx.URL) -> bool:
        """Whether Basic may be sent to ``url``."""
        return url.scheme == "https" or self.allow_over_http

    def refusal(self) -> str:
        """The message explaining a refused plain-HTTP Basic offer."""
        return _BASIC_OVER_HTTP_REFUSED_PROBE if self.probe else BASIC_OVER_HTTP_REFUSED


_Origin = tuple[str, str, int]


def _origin(url: httpx.URL) -> _Origin:
    port = url.port or (443 if url.scheme == "https" else 80)
    return (url.scheme, url.host.lower(), port)


# Keys the credential fingerprint, so a fingerprint that ever reached a log or a
# dump is not an offline-crackable hash of the password. Per process: nothing
# here outlives it.
_TAG_KEY: Final = os.urandom(32)


def _credential_tag(credentials: tuple[str, str]) -> str:
    """A fingerprint that tells credential pairs apart without storing them."""
    material = "\x00".join(credentials).encode("utf-8")
    return hmac.new(_TAG_KEY, material, hashlib.sha256).hexdigest()


def request_target(url: httpx.URL) -> str:
    """The request-target httpx puts on the request line for ``url``."""
    return url.raw_path.decode("ascii")


@dataclass
class _Remembered:
    """The scheme negotiated with one origin, for one credential pair."""

    credential_tag: str = field(repr=False)
    scheme: str
    digest: DigestChallenge | None = None
    nonce_count: int = 0


class CameraAuthState:
    """One camera's authentication policy and the schemes it negotiated.

    Holds no password: a remembered entry keeps a fingerprint of the credential
    pair it was negotiated with and is ignored for any other pair. Entries are
    per origin (scheme, host, port), so a header negotiated with the camera is
    never sent to another host (an ONVIF snapshot URI may name one).

    All methods are synchronous and run on the event loop, so two concurrent
    captures on one camera never take the same nonce count.
    """

    def __init__(self, policy: BasicPolicy | None = None) -> None:
        """Create an empty state (``policy`` defaults to the probe policy)."""
        self._policy = policy if policy is not None else BasicPolicy.for_probe()
        self._remembered: dict[_Origin, _Remembered] = {}

    @property
    def policy(self) -> BasicPolicy:
        """Where Basic may be sent."""
        return self._policy

    def remembered_scheme(self, url: httpx.URL | str) -> str | None:
        """The scheme negotiated with ``url``'s origin, if any."""
        entry = self._remembered.get(_origin(httpx.URL(url)))
        return entry.scheme if entry is not None else None

    def preemptive(
        self,
        method: str,
        url: httpx.URL | str,
        credentials: tuple[str, str] | None,
    ) -> str | None:
        """Return an ``Authorization`` value to send before any challenge.

        ``None`` when nothing was negotiated with this origin for these
        credentials yet (or Basic may no longer go there).
        """
        if credentials is None:
            return None
        target = httpx.URL(url)
        entry = self._remembered.get(_origin(target))
        if entry is None or entry.credential_tag != _credential_tag(credentials):
            return None
        if entry.scheme == "digest" and entry.digest is not None:
            entry.nonce_count += 1
            return digest_authorization(
                entry.digest,
                credentials,
                method=method,
                uri=request_target(target),
                nonce_count=entry.nonce_count,
            )
        if entry.scheme == "basic" and self._policy.permits(target):
            return basic_authorization(credentials)
        return None

    def respond(
        self,
        www_authenticate: Iterable[str],
        method: str,
        url: httpx.URL | str,
        credentials: tuple[str, str],
    ) -> str:
        """Answer a ``401``: choose a scheme, remember it, return the header.

        :param www_authenticate: every ``WWW-Authenticate`` value of the 401.
        :raises UnsupportedAuthSchemeError: no Basic or Digest challenge offered.
        :raises AuthCaptureError: a Digest offer this client cannot answer, or a
            Basic-only offer the policy refuses.
        """
        target = httpx.URL(url)
        origin = _origin(target)
        # Whatever happens next, the old entry no longer describes the camera.
        self._remembered.pop(origin, None)
        challenges = parse_challenges(www_authenticate)
        digest_offers = [c for c in challenges if c.scheme == "digest"]
        if digest_offers:
            usable = [
                d
                for d in (DigestChallenge.from_challenge(c) for c in digest_offers)
                if d is not None
            ]
            if not usable:
                raise AuthCaptureError(
                    "camera offered Digest authentication with an algorithm or "
                    "qop this client does not support"
                )
            best = max(usable, key=lambda d: d.strength)  # first offer wins a tie
            header = digest_authorization(
                best,
                credentials,
                method=method,
                uri=request_target(target),
                nonce_count=1,
            )
            self._remembered[origin] = _Remembered(
                credential_tag=_credential_tag(credentials),
                scheme="digest",
                digest=best,
                nonce_count=1,
            )
            return header
        if any(c.scheme == "basic" for c in challenges):
            if any(not c.valid for c in challenges):
                # Something this parser could not read may have been a Digest
                # offer; answering with the password-bearing scheme instead
                # would be a downgrade.
                raise AuthCaptureError(
                    "camera sent an authentication challenge this client cannot "
                    "read; refusing to fall back to Basic"
                )
            if not self._policy.permits(target):
                raise BasicRefusedError(self._policy.refusal())
            self._remembered[origin] = _Remembered(
                credential_tag=_credential_tag(credentials), scheme="basic"
            )
            return basic_authorization(credentials)
        offered = sorted({c.scheme for c in challenges if c.scheme})
        if offered:
            raise UnsupportedAuthSchemeError(
                "camera requires an unsupported authentication scheme: "
                + ", ".join(offered)
            )
        raise UnsupportedAuthSchemeError(
            "camera answered 401 without an authentication challenge"
        )

    def forget(self, url: httpx.URL | str) -> None:
        """Drop what was negotiated with ``url``'s origin (it was rejected)."""
        self._remembered.pop(_origin(httpx.URL(url)), None)
