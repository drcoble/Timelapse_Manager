#!/usr/bin/env bash
# Exit 0 when a PEP 440 version is a final release (digits and dots only, for
# example 0.2.0), 1 for dev, rc, post and anything else. Only final versions
# move the container image's `latest` tag.
set -euo pipefail

version="${1:?usage: $0 <pep440-version>}"
[[ "$version" =~ ^[0-9]+(\.[0-9]+)*$ ]]
