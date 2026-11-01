"""Application services: transactional write operations shared across surfaces.

A service is a plain function that performs one write (create a project, edit a
camera, trigger a render) against a caller-supplied session, returning a
:class:`~timelapse_manager.services._base.Result` that pairs the produced value
with the post-commit side effects the caller must run. This lets the web router
and the JSON API share one implementation of each mutation instead of two.

Concrete services live in per-domain modules (``services.projects``,
``services.cameras``, ...) and are imported directly from there. They are not
re-exported here on purpose: keeping this package's ``__init__`` free of
per-domain imports means adding a new domain never edits this file and never
risks an import cycle through it.
"""

from __future__ import annotations
