"""
Insight Reasoner: the AI reasoning layer.

This module turns deterministic, compressed analytical artifacts into
human-meaningful insights.

What it consumes (compressed only):
- Compressed schema JSON (from SchemaEngine)
- Compressed analysis-result JSON (from AnalysisEngine)
- Existing stored insight summaries (from MemoryStore)

What it must NOT do:
- No dataframe access
- No statistic computation
- No rule-based "importance" ranking

What it does do:
- LLM-based synthesis: merge multiple signals into coherent insights
- Semantic deduplication: avoid duplicates beyond string matching
- Confidence scoring: based on signal strength/support (LLM reasoning)
- Business translation: convert technical findings into business impact

Token/cost discipline:
- Prompts only receive compressed context.
- Existing insights are passed as short summaries only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple


# -----------------------------
# Output dataclass
# -----------------------------


@dataclass(frozen=True)
class Insight:
    insight_id: str
    title: str
    technical_summary: str
    business_impact: str
    confidence: float
    semantic_hash: str


# -----------------------------
# LLM Client Interface (injectable + mockable)
# -----------------------------


class LLMClient(Protocol):
    """
    Minimal interface for an LLM client, designed to be easy to mock.

    Requirements:
    - `complete` returns a string (model output)
    - optionally provide embeddings via `embed` for semantic dedup
    """

    def complete(self, *, prompt: str, temperature: float = 0.2) -> str: ...

    def embed(self, texts: Sequence[str]) -> Optional[List[List[float]]]: ...


# -----------------------------
# Prompt templates
# -----------------------------


SYSTEM_RULES = """You are an expert data analyst.
You must ONLY use the provided context. Do not invent data.
Do NOT compute statistics. Treat all numbers in the context as given.
Return valid JSON only, with no markdown, no commentary."""


def build_synthesis_prompt(
    *,
    dataset_id: str,
    version: str,
    compressed_schema_json: str,
    compressed_analysis_result_json: str,
    existing_insight_summaries: Sequence[str],
    max_new_insights: int,
) -> str:
    """
    One-shot synthesis prompt.

    Notes:
    - Inputs are already compressed. We keep instruction text short.
    - Existing insights are only summaries to reduce tokens.
    """
    existing = list(existing_insight_summaries)[:50]  # hard cap for prompt size
    payload = {
        "dataset_id": dataset_id,
        "version": version,
        "schema": compressed_schema_json,
        "analysis": compressed_analysis_result_json,
        "existing_insights": existing,
        "constraints": {
            "max_new_insights": max_new_insights,
            "no_new_statistics": True,
            "no_dataframe_access": True,
        },
    }

    instruction = f"""{SYSTEM_RULES}

Task:
Synthesize up to {max_new_insights} non-redundant insights by combining multiple signals when appropriate.

Each insight must include:
- title: short, specific
- technical_summary: explain signals and how they connect
- business_impact: why it matters in business terms (no made-up metrics)
- confidence: float 0..1 based on support/consistency/strength in provided signals
- dedup_key: a short normalized phrase capturing the semantic core (used for semantic hashing)

Avoid duplicates vs existing_insights. Deduplicate semantically (not string equality).

Return JSON in this exact shape:
{{"insights":[{{"title":..., "technical_summary":..., "business_impact":..., "confidence":..., "dedup_key":...}}, ...]}}

Context (JSON):
{json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}
"""
    return instruction


def build_dedup_prompt(*, candidate: Insight, existing: Sequence[Dict[str, Any]]) -> str:
    """
    LLM-based semantic dedup check when embeddings are not available.
    Keep it tiny: compare one candidate to a small set of existing summaries.
    """
    payload = {
        "candidate": {
            "title": candidate.title,
            "technical_summary": candidate.technical_summary,
            "business_impact": candidate.business_impact,
        },
        "existing": existing[:20],
    }
    return f"""{SYSTEM_RULES}

Task:
Decide if candidate is semantically redundant with any existing item.
Return JSON: {{"is_duplicate":true/false,"duplicate_of_insight_id":string_or_null,"reason":string}}

Context:
{json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}
"""


def build_query_prompt(
    *,
    dataset_id: str,
    version: str,
    question: str,
    compressed_schema_json: str,
    compressed_analysis_result_json: Optional[str],
    insight_summaries: Sequence[str],
) -> str:
    """
    Query answering prompt.

    The model must answer using only compressed schema + analysis + stored insights.
    No new statistics may be computed.
    """
    payload = {
        "dataset_id": dataset_id,
        "version": version,
        "question": question,
        "schema": compressed_schema_json,
        "analysis": compressed_analysis_result_json,
        "insights": list(insight_summaries)[:80],
    }
    return f"""{SYSTEM_RULES}

Task:
Answer the user's question using ONLY the provided context.
If the context is insufficient, say what is missing and suggest the minimum additional analysis needed.
Do NOT compute new statistics or invent values.

Return JSON in this shape:
{{"answer":string,"used":["schema"|"analysis"|"insights"],"limitations":string}}

Context (JSON):
{json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}
"""


# -----------------------------
# Reasoner
# -----------------------------


class InsightReasoner:
    """
    The AI reasoning layer.

    This class:
    - Optionally compresses prompts using ScaleDown (for token efficiency)
    - Performs a one-shot LLM synthesis call from compressed context via an LLM client (e.g., Ollama)
    - Assigns deterministic IDs + semantic hashes
    - Performs semantic dedup using embeddings (preferred) or a small LLM check
    
    Pipeline:
    1. Build full reasoning prompt
    2. (Optional) Compress prompt using ScaleDown client
    3. Send compressed/full prompt to LLM client
    4. Parse and deduplicate results
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_new_insights: int = 8,
        temperature: float = 0.2,
        embedding_similarity_threshold: float = 0.88,
        compression_client: Optional[Any] = None,
    ) -> None:
        self.llm = llm
        self.max_new_insights = int(max_new_insights)
        self.temperature = float(temperature)
        self.embedding_similarity_threshold = float(embedding_similarity_threshold)
        self.compression_client = compression_client  # Optional ScaledownCompressionClient

    # -----------------------------
    # Deterministic helpers
    # -----------------------------

    def _maybe_compress_prompt(self, prompt: str) -> str:
        """
        Optionally compress prompt using ScaleDown if client is available.
        
        If compression fails, returns original prompt (explicit fallback).
        """
        if not self.compression_client:
            return prompt
        
        try:
            compressed = self.compression_client.compress(prompt)
            return compressed
        except Exception as e:
            # Log and fall back gracefully
            print(f"Warning: Prompt compression failed ({e}), using original prompt")
            return prompt

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        # remove most punctuation to make hashes stable across minor phrasing
        text = re.sub(r"[^\w\s\-]", "", text)
        return text

    @staticmethod
    def semantic_hash(dedup_key: str) -> str:
        """Deterministic semantic hash from a normalized dedup key."""
        norm = InsightReasoner._normalize_text(dedup_key)
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()

    @staticmethod
    def insight_id(dataset_id: str, version: str, semantic_hash: str) -> str:
        """Deterministic ID: stable across re-runs for the same semantic insight."""
        base = f"{dataset_id}|{version}|{semantic_hash}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _clamp01(x: float) -> float:
        return max(0.0, min(1.0, float(x)))

    # -----------------------------
    # Embedding similarity (optional)
    # -----------------------------

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        denom = math.sqrt(na) * math.sqrt(nb)
        return float(dot / denom) if denom > 0 else 0.0

    def _embedding_dedup(
        self,
        candidate: Insight,
        existing_summaries: Sequence[Tuple[str, str]],
    ) -> Tuple[bool, Optional[str]]:
        """
        Dedup candidate vs existing using embeddings if client supports it.

        Args:
            existing_summaries: list of (insight_id, summary_text)

        Returns:
            (is_duplicate, duplicate_of_insight_id)
        """
        texts = [candidate.title + " " + candidate.technical_summary]
        texts.extend([s for _, s in existing_summaries])
        emb = self.llm.embed(texts)
        if not emb or len(emb) != len(texts):
            return False, None

        cand_vec = emb[0]
        best_id = None
        best_sim = -1.0
        for (insight_id, _), vec in zip(existing_summaries, emb[1:]):
            sim = self._cosine_similarity(cand_vec, vec)
            if sim > best_sim:
                best_sim = sim
                best_id = insight_id

        if best_sim >= self.embedding_similarity_threshold:
            return True, best_id
        return False, None

    # -----------------------------
    # Public API
    # -----------------------------

    def synthesize_insights(
        self,
        *,
        dataset_id: str,
        version: str,
        compressed_schema_json: str,
        compressed_analysis_result_json: str,
        existing_insight_summaries: Sequence[str],
        existing_insights_for_dedup: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[Insight]:
        """
        Produce synthesized insights via a single LLM call, then semantically deduplicate.

        Pipeline:
        1. Build full reasoning prompt
        2. Compress prompt using ScaleDown (if available)
        3. Send to LLM (e.g., Ollama)
        4. Parse and deduplicate results

        Inputs are compressed JSON strings; this function does not parse/compute stats.
        """
        prompt = build_synthesis_prompt(
            dataset_id=dataset_id,
            version=version,
            compressed_schema_json=compressed_schema_json,
            compressed_analysis_result_json=compressed_analysis_result_json,
            existing_insight_summaries=existing_insight_summaries,
            max_new_insights=self.max_new_insights,
        )

        # Optional compression: reduce token count before sending to LLM
        prompt = self._maybe_compress_prompt(prompt)

        raw = self.llm.complete(prompt=prompt, temperature=self.temperature)
        parsed = self._parse_llm_json(raw)
        candidates = parsed.get("insights", [])

        insights: List[Insight] = []
        # For embedding dedup we need IDs + summaries; since existing are summaries only,
        # we fall back to hash-based + embedding similarity against summaries.
        existing_pairs: List[Tuple[str, str]] = []
        for idx, s in enumerate(existing_insight_summaries[:50]):
            # We may not have their IDs here; fabricate stable pseudo-IDs based on index+hash
            pseudo_id = hashlib.sha256(f"{dataset_id}|existing|{idx}|{s}".encode("utf-8")).hexdigest()[:16]
            existing_pairs.append((pseudo_id, s))

        for c in candidates[: self.max_new_insights]:
            title = str(c.get("title", "")).strip()
            technical = str(c.get("technical_summary", "")).strip()
            business = str(c.get("business_impact", "")).strip()
            conf = self._clamp01(float(c.get("confidence", 0.5)))
            dedup_key = str(c.get("dedup_key", title)).strip() or title

            sem_hash = self.semantic_hash(dedup_key)
            iid = self.insight_id(dataset_id, version, sem_hash)

            candidate = Insight(
                insight_id=iid,
                title=title,
                technical_summary=technical,
                business_impact=business,
                confidence=conf,
                semantic_hash=sem_hash,
            )

            # First-pass deterministic dedup: semantic_hash collision within this batch
            if any(x.semantic_hash == candidate.semantic_hash for x in insights):
                continue

            # Second-pass: embedding-based semantic dedup vs existing summaries (preferred)
            is_dup, _dup_of = self._embedding_dedup(candidate, existing_pairs)
            if is_dup:
                continue

            # Third-pass: if embeddings not available and caller provided structured existing,
            # do a tiny LLM dedup check (still semantic; not string matching).
            if existing_insights_for_dedup:
                if self._llm_semantic_duplicate(candidate, existing_insights_for_dedup):
                    continue

            insights.append(candidate)

        return insights

    def answer_query(
        self,
        *,
        dataset_id: str,
        version: str,
        question: str,
        compressed_schema_json: str,
        compressed_analysis_result_json: Optional[str],
        insight_summaries: Sequence[str],
    ) -> str:
        """
        Answer a natural-language question using only compressed stored context.

        Pipeline:
        1. Build full query prompt
        2. Compress prompt using ScaleDown (if available)
        3. Send to LLM (e.g., Ollama)
        4. Parse and return answer

        Returns:
            A plain-text answer string (ready for API response / caching).
        """
        prompt = build_query_prompt(
            dataset_id=dataset_id,
            version=version,
            question=question,
            compressed_schema_json=compressed_schema_json,
            compressed_analysis_result_json=compressed_analysis_result_json,
            insight_summaries=insight_summaries,
        )

        # Optional compression: reduce token count before sending to LLM
        prompt = self._maybe_compress_prompt(prompt)

        raw = self.llm.complete(prompt=prompt, temperature=self.temperature)
        parsed = self._parse_llm_json(raw)
        ans = parsed.get("answer")
        if isinstance(ans, str) and ans.strip():
            return ans.strip()
        # Fallback: return raw text to avoid total failure, still cacheable.
        return (raw or "").strip()

    # -----------------------------
    # Internal parsing + dedup
    # -----------------------------

    @staticmethod
    def _parse_llm_json(text: str) -> Dict[str, Any]:
        """
        Parse JSON from an LLM response. Best-effort but deterministic.
        If the model returns extra text, try to extract the first JSON object.
        """
        text = (text or "").strip()
        if not text:
            return {"insights": []}

        # Try direct parse
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        # Extract first JSON object substring
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                obj = json.loads(text[start : end + 1])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

        return {"insights": []}

    def _llm_semantic_duplicate(self, candidate: Insight, existing: Sequence[Dict[str, Any]]) -> bool:
        """
        Semantic dedup via small LLM call when embeddings aren't available.
        Existing items should be compact dicts: {insight_id, summary/title}.
        """
        prompt = build_dedup_prompt(candidate=candidate, existing=list(existing))
        raw = self.llm.complete(prompt=prompt, temperature=0.0)  # deterministic as possible
        parsed = self._parse_llm_json(raw)
        return bool(parsed.get("is_duplicate", False))


# -----------------------------
# Convenience helpers (optional)
# -----------------------------


def insights_to_summaries(insights: Sequence[Insight]) -> List[str]:
    """Compact summaries suitable for passing back into the next synthesis call."""
    out: List[str] = []
    for ins in insights:
        out.append(f"{ins.title} :: {ins.technical_summary[:240]}")
    return out


def insight_to_stored_insight_payload(insight: Insight, dataset_id: str, version: str) -> Dict[str, Any]:
    """
    Produce a dict compatible with memory.StoredInsight without importing memory module
    (keeps this layer decoupled and testable).
    """
    return {
        "insight_id": insight.insight_id,
        "dataset_id": dataset_id,
        "version": version,
        "summary": f"{insight.title}: {insight.technical_summary}",
        "confidence": insight.confidence,
        "semantic_hash": insight.semantic_hash,
    }

