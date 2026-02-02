"""
Memory Store: persistent, memory-first artifact storage for the analysis agent.

Why this matters (and why it makes this an AI agent, not a script):
- Deterministic EDA can be expensive; recomputing repeatedly wastes time and money.
- LLM calls are *token-expensive*; sending raw data or verbose context is costly.
- By storing **compressed schemas** and **compressed analysis signals**, we can:
  - answer many user questions from memory
  - prevent redundant computation
  - feed the LLM only a small, relevant, pre-compressed context
  - deduplicate insights using deterministic semantic hashes (computed elsewhere)

Design constraints:
- No LLM usage here
- No recomputation here
- Dict-backed in-memory store first, with optional JSON persistence to disk
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# -----------------------------
# Dataclasses: stored artifacts
# -----------------------------


@dataclass(frozen=True)
class StoredSchema:
    dataset_id: str
    version: str
    file_hash: str
    compressed_schema_json: str


@dataclass(frozen=True)
class StoredAnalysis:
    dataset_id: str
    version: str
    analysis_result: str  # compressed AnalysisResult JSON
    created_at: str


@dataclass(frozen=True)
class StoredInsight:
    """
    Placeholder for synthesized insights (full insight reasoning lives elsewhere).

    semantic_hash:
      A deterministic fingerprint of the insight's meaning, produced by the
      insight reasoner (e.g., via LLM-assisted normalization + hashing).
      This store uses it for existence checks and semantic deduplication.
    """

    insight_id: str
    dataset_id: str
    version: str
    summary: str
    confidence: float
    semantic_hash: str


@dataclass(frozen=True)
class QueryCacheEntry:
    query_hash: str
    dataset_id: str
    response: str
    created_at: str


# -----------------------------
# MemoryStore
# -----------------------------


class MemoryStore:
    """
    Dict-backed, deterministic memory store with optional JSON persistence.

    Core principle: consumers must check memory BEFORE recomputation.
    This store never performs analysis or reasoning; it only saves/retrieves.
    """

    def __init__(self, persist_path: Optional[Union[str, Path]] = None) -> None:
        """
        Args:
            persist_path: Optional path to a JSON file to persist store contents.
                          If provided and file exists, it is loaded at startup.
        """
        self.persist_path = Path(persist_path) if persist_path else None

        # Primary indexes
        self._schemas: Dict[Tuple[str, str], StoredSchema] = {}
        self._analyses: Dict[Tuple[str, str], StoredAnalysis] = {}

        # Insights: list per dataset, plus semantic index for dedup
        self._insights_by_dataset: Dict[str, List[StoredInsight]] = {}
        self._insight_semantic_index: Dict[Tuple[str, str], str] = {}
        # key: (dataset_id, semantic_hash) -> insight_id

        # Query cache: (dataset_id, query_hash) -> entry
        self._query_cache: Dict[Tuple[str, str], QueryCacheEntry] = {}

        if self.persist_path and self.persist_path.exists():
            self._load()

    # -----------------------------
    # Deterministic hashing helpers
    # -----------------------------

    @staticmethod
    def stable_hash(text: str) -> str:
        """Deterministic SHA256 of a UTF-8 string (useful for query hashing)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # -----------------------------
    # Schemas
    # -----------------------------

    def save_schema(self, schema: StoredSchema) -> None:
        """Persist a compressed schema for a given dataset/version."""
        key = (schema.dataset_id, schema.version)
        self._schemas[key] = schema
        self._persist_if_enabled()

    def get_schema(self, dataset_id: str, version: str) -> Optional[StoredSchema]:
        """Retrieve stored schema, or None if not present."""
        return self._schemas.get((dataset_id, version))

    def list_versions(self, dataset_id: str) -> List[str]:
        """List known versions for a dataset_id (sorted lexicographically)."""
        versions = [v for (ds, v) in self._schemas.keys() if ds == dataset_id]
        versions.sort()
        return versions

    # -----------------------------
    # Analyses (EDA signals)
    # -----------------------------

    def save_analysis(self, analysis: StoredAnalysis) -> None:
        """Persist compressed analysis results for a dataset/version."""
        key = (analysis.dataset_id, analysis.version)
        self._analyses[key] = analysis
        self._persist_if_enabled()

    def get_analysis(self, dataset_id: str, version: str) -> Optional[StoredAnalysis]:
        """Retrieve stored analysis, or None if not present."""
        return self._analyses.get((dataset_id, version))

    def has_analysis(self, dataset_id: str, version: str) -> bool:
        """True if analysis exists; callers should use this to skip recomputation."""
        return (dataset_id, version) in self._analyses

    # -----------------------------
    # Insights (semantic dedup index)
    # -----------------------------

    def insight_exists(self, semantic_hash: str, dataset_id: str) -> bool:
        """Deterministic semantic dedup check for a dataset."""
        return (dataset_id, semantic_hash) in self._insight_semantic_index

    def save_insight(self, insight: StoredInsight) -> None:
        """
        Save an insight if it doesn't already exist for (dataset_id, semantic_hash).

        Note: dedup policy is deterministic and strict; it does not merge.
        Merging/normalization belongs in the insight reasoner.
        """
        key = (insight.dataset_id, insight.semantic_hash)
        if key in self._insight_semantic_index:
            return  # deterministic no-op

        self._insight_semantic_index[key] = insight.insight_id
        self._insights_by_dataset.setdefault(insight.dataset_id, []).append(insight)
        self._persist_if_enabled()

    def list_insights(self, dataset_id: str) -> List[StoredInsight]:
        """Return all stored insights for a dataset (in insertion order)."""
        return list(self._insights_by_dataset.get(dataset_id, []))

    # -----------------------------
    # Query cache (natural language answers)
    # -----------------------------

    def get_cached_query(self, query_hash: str, dataset_id: str) -> Optional[QueryCacheEntry]:
        """Retrieve cached response for a (dataset_id, query_hash) pair."""
        return self._query_cache.get((dataset_id, query_hash))

    def save_cached_query(self, query_hash: str, dataset_id: str, response: str) -> None:
        """Store a cached response deterministically."""
        entry = QueryCacheEntry(
            query_hash=str(query_hash),
            dataset_id=str(dataset_id),
            response=str(response),
            created_at=datetime.now().isoformat(),
        )
        self._query_cache[(dataset_id, query_hash)] = entry
        self._persist_if_enabled()

    # -----------------------------
    # Persistence (optional, minimal)
    # -----------------------------

    def _persist_if_enabled(self) -> None:
        if self.persist_path:
            self._save()

    def _save(self) -> None:
        """
        Save the entire store to a single JSON file.

        This is intentionally minimal (not a DB) and deterministic in structure.
        """
        payload: Dict[str, Any] = {
            "schemas": [asdict(v) for v in self._schemas.values()],
            "analyses": [asdict(v) for v in self._analyses.values()],
            "insights": [asdict(v) for v in self._flatten_insights()],
            "query_cache": [asdict(v) for v in self._query_cache.values()],
        }

        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.persist_path)

    def _load(self) -> None:
        """Load store contents from disk (best-effort, deterministic)."""
        raw = self.persist_path.read_text(encoding="utf-8")
        payload = json.loads(raw) if raw.strip() else {}

        self._schemas.clear()
        self._analyses.clear()
        self._insights_by_dataset.clear()
        self._insight_semantic_index.clear()
        self._query_cache.clear()

        for item in payload.get("schemas", []):
            s = StoredSchema(**item)
            self._schemas[(s.dataset_id, s.version)] = s

        for item in payload.get("analyses", []):
            a = StoredAnalysis(**item)
            self._analyses[(a.dataset_id, a.version)] = a

        for item in payload.get("insights", []):
            ins = StoredInsight(**item)
            self._insights_by_dataset.setdefault(ins.dataset_id, []).append(ins)
            self._insight_semantic_index[(ins.dataset_id, ins.semantic_hash)] = ins.insight_id

        for item in payload.get("query_cache", []):
            q = QueryCacheEntry(**item)
            self._query_cache[(q.dataset_id, q.query_hash)] = q

    def _flatten_insights(self) -> List[StoredInsight]:
        all_insights: List[StoredInsight] = []
        for lst in self._insights_by_dataset.values():
            all_insights.extend(lst)
        return all_insights

