"""mapping_cache_store.py

Local-filesystem cache for approved ReverseMappingPlan objects, keyed by
``template_fingerprint`` (a stable content hash of the customer template).

Once a template has been mapped+approved by an operator we save the approved
plan here.  On subsequent runs of the same template the rehydrate pipeline
loads the cached plan instead of re-running the (non-deterministic) LLM —
the operator no longer has to re-review every mapping.

Storage layout
--------------
::

    artifacts/mapping_cache/<template_fingerprint>.json

Each file holds a single JSON document::

    {
        "template_fingerprint": "<sha256[:16]>",
        "approved_at": "<ISO-8601 UTC>",
        "approved_by": "<operator name or 'unknown'>",
        "cache_schema_version": 1,
        "plan": { ... ReverseMappingPlan.model_dump() ... }
    }

A future migration to blob storage (for multi-user / shared cache) is a
drop-in replacement: ``MappingCacheStore`` is the only consumer of the
filesystem layout and the storage backend is hidden behind the four public
methods (``load`` / ``save`` / ``clear`` / ``list_cached``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ..models.contracts import ReverseMappingPlan

logger = logging.getLogger(__name__)

# Filesystem-safe fingerprint pattern. template_fingerprint is a sha256 hex
# slice so it should only contain [0-9a-f], but we defend against unexpected
# input to avoid path-traversal style bugs.
_SAFE_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

CACHE_SCHEMA_VERSION: int = 1
DEFAULT_CACHE_ROOT_ENV: str = "REHYDRATE_MAPPING_CACHE_ROOT"
DEFAULT_CACHE_ROOT: str = "artifacts/mapping_cache"


@dataclass(frozen=True)
class CachedPlanRecord:
    """A loaded cache entry plus the metadata around the approval event.

    ``approved_at`` is parsed back into a ``datetime``; ``approved_by`` is the
    operator identifier supplied at save time (or ``"unknown"``).
    """

    plan: ReverseMappingPlan
    approved_at: datetime
    approved_by: str
    cache_path: Path


class MappingCacheStore:
    """Filesystem-backed cache for approved ReverseMappingPlan objects.

    Args:
        root: Directory under which cache files are written. Defaults to
            ``$REHYDRATE_MAPPING_CACHE_ROOT`` or ``artifacts/mapping_cache``.
            The directory is created lazily on the first ``save``.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        resolved = (
            str(root)
            if root is not None
            else os.getenv(DEFAULT_CACHE_ROOT_ENV) or DEFAULT_CACHE_ROOT
        )
        self.root = Path(resolved)

    # ─── public API ──────────────────────────────────────────────────────────

    def load(self, template_fingerprint: str) -> CachedPlanRecord | None:
        """Return the cached record for ``template_fingerprint`` or None.

        Returns None when:
          * the cache file does not exist
          * the file cannot be parsed as JSON
          * the embedded plan fails Pydantic validation
          * the embedded fingerprint does not match the requested key
        """
        path = self._path_for(template_fingerprint)
        if path is None or not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Cache file %s is unreadable: %s", path, exc)
            return None
        try:
            plan = ReverseMappingPlan.model_validate(doc.get("plan", {}))
        except ValidationError as exc:
            logger.warning("Cache file %s contains an invalid plan: %s", path, exc)
            return None
        # Defensive: refuse to honour a fingerprint mismatch — could indicate
        # a copy-pasted cache file or fingerprint algorithm change.
        embedded_fp = str(doc.get("template_fingerprint") or "")
        if embedded_fp and embedded_fp != template_fingerprint:
            logger.warning(
                "Cache file %s claims fingerprint %s but was requested under %s; "
                "ignoring",
                path,
                embedded_fp,
                template_fingerprint,
            )
            return None
        if plan.template_fingerprint and plan.template_fingerprint != template_fingerprint:
            logger.warning(
                "Cached plan fingerprint %s does not match requested %s; ignoring",
                plan.template_fingerprint,
                template_fingerprint,
            )
            return None
        approved_at = _parse_iso_utc(doc.get("approved_at")) or datetime.fromtimestamp(
            path.stat().st_mtime, tz=UTC
        )
        approved_by = str(doc.get("approved_by") or "unknown")
        return CachedPlanRecord(
            plan=plan,
            approved_at=approved_at,
            approved_by=approved_by,
            cache_path=path,
        )

    def save(
        self,
        plan: ReverseMappingPlan,
        *,
        approved_by: str = "unknown",
        approved_at: datetime | None = None,
    ) -> Path:
        """Persist ``plan`` to the cache, keyed by ``plan.template_fingerprint``.

        Returns the path the cache entry was written to. Writes are atomic
        (temp file in the same directory then ``os.replace``).
        """
        fingerprint = plan.template_fingerprint
        path = self._path_for(fingerprint)
        if path is None:
            raise ValueError(
                f"Refusing to cache plan: template_fingerprint "
                f"{fingerprint!r} is empty or not filesystem-safe."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        approved_at_dt = approved_at or datetime.now(UTC)
        doc = {
            "template_fingerprint": fingerprint,
            "approved_at": approved_at_dt.astimezone(UTC).isoformat(),
            "approved_by": approved_by,
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "plan": plan.model_dump(mode="json"),
        }
        # Atomic write so a partially-written file never becomes the cached entry.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{fingerprint}.",
            suffix=".json.tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, indent=2)
            os.replace(tmp_name, path)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return path

    def clear(self, template_fingerprint: str) -> bool:
        """Remove the cache entry for ``template_fingerprint``.

        Returns True if a file was removed, False otherwise.
        """
        path = self._path_for(template_fingerprint)
        if path is None or not path.is_file():
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning("Failed to clear cache file %s: %s", path, exc)
            return False

    def list_cached(self) -> list[str]:
        """Return the list of template_fingerprints currently cached."""
        if not self.root.is_dir():
            return []
        out: list[str] = []
        for entry in self.root.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            if entry.name.startswith("."):
                # skip in-flight temp files
                continue
            out.append(entry.stem)
        out.sort()
        return out

    # ─── helpers ─────────────────────────────────────────────────────────────

    def _path_for(self, template_fingerprint: str) -> Path | None:
        """Map a fingerprint to its cache filename, or None if it is unsafe."""
        if not template_fingerprint or not _SAFE_FINGERPRINT_RE.match(
            template_fingerprint
        ):
            return None
        return self.root / f"{template_fingerprint}.json"


def _parse_iso_utc(value: object) -> datetime | None:
    """Parse an ISO-8601 string (possibly ending in ``Z``) into a UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # ``fromisoformat`` accepts the ``+00:00`` offset; normalise trailing ``Z``.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
