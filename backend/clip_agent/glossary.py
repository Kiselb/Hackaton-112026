from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


_WORD_CHAR = "0-9A-Za-zА-Яа-яЁё"
_ABBREVIATION = re.compile(r"^[0-9A-Za-zА-Яа-яЁё]+(?:-[0-9A-Za-zА-Яа-яЁё]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_GLOSSARY_BYTES = 5 * 1024 * 1024
MAX_QUERY_CHARS = 4000
MAX_EXPANDED_QUERY_CHARS = 6000


class GlossaryConfigError(RuntimeError):
    """A fail-closed, user-safe glossary configuration error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GlossaryTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    abbreviation: str = Field(min_length=1, max_length=64)
    expansion: str = Field(min_length=1, max_length=500)
    synonyms: list[str] = Field(default_factory=list, max_length=20)
    ambiguous: bool = False
    context_terms: list[str] = Field(default_factory=list, max_length=30)
    case_sensitive: bool = False

    @field_validator("abbreviation")
    @classmethod
    def validate_abbreviation(cls, value: str) -> str:
        cleaned = value.strip()
        if not _ABBREVIATION.fullmatch(cleaned):
            raise ValueError("abbreviation must contain only letters, digits, and inner hyphens")
        return cleaned

    @field_validator("expansion")
    @classmethod
    def validate_expansion(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("expansion must not be empty")
        return cleaned

    @field_validator("synonyms")
    @classmethod
    def validate_synonyms(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            alias = value.strip()
            if not _ABBREVIATION.fullmatch(alias):
                raise ValueError("synonyms must contain only letters, digits, and inner hyphens")
            if alias not in cleaned:
                cleaned.append(alias)
        return cleaned

    @field_validator("context_terms")
    @classmethod
    def validate_context_terms(cls, values: list[str]) -> list[str]:
        cleaned = [" ".join(value.split()).casefold() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))


class GlossaryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=100)
    source: Literal["customer_provided", "synthetic_test"]
    content_sha256: str
    terms: list[GlossaryTerm] = Field(min_length=1, max_length=10_000)

    @field_validator("content_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("content_sha256 must be a lowercase SHA-256 hex digest")
        return value


def canonical_glossary_payload(document: GlossaryDocument | dict[str, Any]) -> bytes:
    if isinstance(document, GlossaryDocument):
        payload = document.model_dump(mode="json", exclude={"content_sha256"})
    else:
        payload = {key: value for key, value in document.items() if key != "content_sha256"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def glossary_checksum(document: GlossaryDocument | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_glossary_payload(document)).hexdigest()


@dataclass(frozen=True)
class GlossaryExpansion:
    original_query: str
    search_query: str
    version: str | None
    content_sha256: str | None
    enabled: bool
    found_count: int
    applied_count: int
    skipped_count: int
    skip_reasons: dict[str, int]
    matches: tuple[tuple[str, str, str], ...]
    latency_ms: float

    @property
    def used(self) -> bool:
        return self.applied_count > 0

    @property
    def changed(self) -> bool:
        return self.search_query != self.original_query

    def trace_details(self) -> dict[str, Any]:
        return {
            "glossary_enabled": self.enabled,
            "glossary_used": self.used,
            "glossary_version": self.version,
            "glossary_sha256": self.content_sha256,
            "glossary_found_terms": self.found_count,
            "glossary_applied_expansions": self.applied_count,
            "glossary_skipped_expansions": self.skipped_count,
            "glossary_skip_reasons": self.skip_reasons,
            "glossary_matches": [
                {"abbreviation": abbreviation, "expansion": expansion, "status": status}
                for abbreviation, expansion, status in self.matches
            ],
            "glossary_latency_ms": self.latency_ms,
            "glossary_query_changed": self.changed,
            "glossary_search_query": self.search_query,
            "glossary_additional_retrieval_calls": 0,
        }


def disabled_expansion(query: str) -> GlossaryExpansion:
    return GlossaryExpansion(
        original_query=query,
        search_query=query,
        version=None,
        content_sha256=None,
        enabled=False,
        found_count=0,
        applied_count=0,
        skipped_count=0,
        skip_reasons={},
        matches=(),
        latency_ms=0.0,
    )


class GlossaryExpander:
    def __init__(self, document: GlossaryDocument) -> None:
        aliases: dict[str, tuple[str, GlossaryTerm]] = {}
        for term in document.terms:
            for alias in (term.abbreviation, *term.synonyms):
                key = alias.casefold()
                existing = aliases.get(key)
                if existing is not None:
                    raise GlossaryConfigError(
                        "conflicting_alias",
                        f"glossary alias {alias!r} is defined more than once",
                    )
                aliases[key] = (alias, term)
        self.document = document
        self.aliases = aliases
        alternatives = sorted(
            (re.escape(alias) for alias, _ in aliases.values()),
            key=len,
            reverse=True,
        )
        self.pattern = re.compile(
            rf"(?<![{_WORD_CHAR}])(?:{'|'.join(alternatives)})(?![{_WORD_CHAR}])",
            re.IGNORECASE,
        )

    def expand(self, query: str) -> GlossaryExpansion:
        started = time.perf_counter()
        if len(query) > MAX_QUERY_CHARS:
            return self._result(
                query,
                query,
                found=0,
                applied=0,
                reasons=Counter({"query_too_long": 1}),
                started=started,
            )
        matches = list(self.pattern.finditer(query))
        if not matches:
            return self._result(query, query, 0, 0, Counter(), started)

        normalized_query = " ".join(query.split()).casefold()
        applied_terms: set[str] = set()
        reasons: Counter[str] = Counter()
        glossary_matches: list[tuple[str, str, str]] = []
        replacements: list[tuple[int, int, str]] = []
        for match in matches:
            matched = match.group(0)
            declared_alias, term = self.aliases[matched.casefold()]
            term_key = term.abbreviation.casefold()
            if term.case_sensitive and matched != declared_alias:
                reasons["case_sensitive_term"] += 1
                glossary_matches.append(
                    (term.abbreviation, term.expansion, "case_sensitive_term")
                )
                continue
            if term.expansion.casefold() in normalized_query:
                reasons["already_expanded"] += 1
                glossary_matches.append(
                    (term.abbreviation, term.expansion, "already_expanded")
                )
                continue
            if term_key in applied_terms:
                reasons["repeated_term"] += 1
                glossary_matches.append(
                    (term.abbreviation, term.expansion, "repeated_term")
                )
                continue
            if term.ambiguous and not any(
                marker in normalized_query for marker in term.context_terms
            ):
                reasons["ambiguous_without_context"] += 1
                glossary_matches.append(
                    (term.abbreviation, term.expansion, "ambiguous_without_context")
                )
                continue
            replacement = f"{term.expansion} ({term.abbreviation})"
            replacements.append((match.start(), match.end(), replacement))
            applied_terms.add(term_key)
            glossary_matches.append((term.abbreviation, term.expansion, "applied"))

        expanded = query
        for start, end, replacement in reversed(replacements):
            expanded = f"{expanded[:start]}{replacement}{expanded[end:]}"
        if len(expanded) > MAX_EXPANDED_QUERY_CHARS:
            reasons["expanded_query_too_long"] += len(replacements)
            glossary_matches = [
                (
                    abbreviation,
                    expansion,
                    "expanded_query_too_long" if status == "applied" else status,
                )
                for abbreviation, expansion, status in glossary_matches
            ]
            replacements = []
            expanded = query
        return self._result(
            query,
            expanded,
            found=len(matches),
            applied=len(replacements),
            reasons=reasons,
            matches=tuple(glossary_matches),
            started=started,
        )

    def _result(
        self,
        original: str,
        expanded: str,
        found: int,
        applied: int,
        reasons: Counter[str],
        started: float,
        matches: tuple[tuple[str, str, str], ...] = (),
    ) -> GlossaryExpansion:
        return GlossaryExpansion(
            original_query=original,
            search_query=expanded,
            version=self.document.version,
            content_sha256=self.document.content_sha256,
            enabled=True,
            found_count=found,
            applied_count=applied,
            skipped_count=sum(reasons.values()),
            skip_reasons=dict(sorted(reasons.items())),
            matches=matches,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )


def load_glossary(path: Path) -> GlossaryExpander:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise GlossaryConfigError("file_unavailable", "glossary file is unavailable") from exc
    if size <= 0 or size > MAX_GLOSSARY_BYTES:
        raise GlossaryConfigError("invalid_file_size", "glossary file size is invalid")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GlossaryConfigError("invalid_json", "glossary is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise GlossaryConfigError("invalid_structure", "glossary root must be an object")
    try:
        document = GlossaryDocument.model_validate(raw)
    except ValidationError as exc:
        raise GlossaryConfigError("invalid_structure", "glossary schema validation failed") from exc
    actual_checksum = glossary_checksum(raw)
    if actual_checksum != document.content_sha256:
        raise GlossaryConfigError("checksum_mismatch", "glossary checksum validation failed")
    return GlossaryExpander(document)


@lru_cache(maxsize=8)
def get_glossary_expander(path: str) -> GlossaryExpander:
    return load_glossary(Path(path))
