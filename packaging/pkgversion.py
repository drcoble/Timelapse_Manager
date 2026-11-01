#!/usr/bin/env python3
"""Map a PEP 440 version to the fields .deb and .rpm sort correctly (#303).

The release tag, tarball and image all carry the PEP 440 string (for example
``0.2.0.dev10``). dpkg and rpm order versions differently: ``0.2.0.dev10`` and
``0.2.0rc6`` would sort *after* ``0.2.0``, so moving from a pre-release to the
final would be a downgrade. Both tools sort ``~`` before everything, including
the end of the string, and ``+`` after it, so:

=================  ===================  ==============  ===========
PEP 440            package version      ``prerelease``  ``metadata``
=================  ===================  ==============  ===========
``0.2.0.dev10``    ``0.2.0~dev10``      ``dev10``
``0.2.0rc6``       ``0.2.0~rc6``        ``rc6``
``0.2.0rc1.dev3``  ``0.2.0~rc1~dev3``   ``rc1~dev3``
``0.2.0``          ``0.2.0``
``0.2.0.post1``    ``0.2.0+post1``                      ``post1``
=================  ===================  ==============  ===========

The three fields feed nfpm with ``version_schema: none``: ``version`` is the
release part, ``prerelease`` is rendered after a ``~`` and ``metadata`` after
a ``+``. No epoch; the package release is ``1``.

Only normalised PEP 440 is accepted (``N(.N)*``, an optional ``rcN``
pre-release, ``.postN``, ``.devN``). Refused rather than guessed at: an epoch or
local version, ``.post`` combined with ``.dev``, and ``aN``/``bN`` pre-releases.
Those last would sort wrongly: PEP 440 puts ``0.2.0.dev7`` below ``0.2.0a1``,
but ``~a1`` sorts below ``~dev7`` (``a`` < ``d``), whereas ``~dev`` < ``~rc``
holds. This project's pre-releases are ``devN`` and ``rcN``.

Standard library only: it runs on a build host with a bare ``python3``.

Usage: ``pkgversion.py <pep440> [--field version|prerelease|metadata|full]``
(no ``--field`` prints ``full``).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

_PEP440 = re.compile(
    r"""^
    (?P<release>\d+(?:\.\d+)*)
    (?:(?P<pre_kind>rc)(?P<pre_num>\d+))?
    (?:\.post(?P<post>\d+))?
    (?:\.dev(?P<dev>\d+))?
    $""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class PackageVersion:
    """The nfpm version fields and the version string they render to."""

    version: str
    prerelease: str = ""
    metadata: str = ""

    @property
    def full(self) -> str:
        """The deb/rpm upstream version (without the package release)."""
        text = self.version
        if self.prerelease:
            text += f"~{self.prerelease}"
        if self.metadata:
            text += f"+{self.metadata}"
        return text


def map_version(pep440: str) -> PackageVersion:
    """Return the package version fields for a normalised PEP 440 ``pep440``.

    :raises ValueError: for anything outside the supported subset.
    """
    match = _PEP440.match(pep440)
    if match is None:
        raise ValueError(f"unsupported version {pep440!r}: not normalised PEP 440")
    parts = match.groupdict()
    if parts["post"] is not None and parts["dev"] is not None:
        raise ValueError(f"unsupported version {pep440!r}: .post with .dev")
    pre: list[str] = []
    if parts["pre_kind"]:
        pre.append(f"{parts['pre_kind']}{parts['pre_num']}")
    if parts["dev"] is not None:
        pre.append(f"dev{parts['dev']}")
    metadata = f"post{parts['post']}" if parts["post"] is not None else ""
    return PackageVersion(parts["release"], "~".join(pre), metadata)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="PEP 440 version, e.g. 0.2.0.dev10")
    parser.add_argument(
        "--field",
        choices=("version", "prerelease", "metadata", "full"),
        default="full",
    )
    args = parser.parse_args(argv)
    try:
        mapped = map_version(args.version)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(mapped.full if args.field == "full" else getattr(mapped, args.field))
    return 0


if __name__ == "__main__":
    sys.exit(main())
