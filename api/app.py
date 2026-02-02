"""
FastAPI app wiring for the Advanced Data Analysis Agent.

Deterministic Agent Router:
- POST /ingest
- POST /analyze
- POST /query
- POST /compare

Strict separation of concerns:
- api layer: orchestration + routing only
- analysis layer: deterministic computation (pandas/numpy)
- reasoning layer: LLM synthesis / explanation (no stats)
- memory layer: persistence + cache (memory-first)

LLM Pipeline:
1. ScaleDown for prompt compression (free, token-efficient)
2. Ollama for local LLM generation (fully free, llama3.1:8b)

Environment:
- `.env` is loaded at startup via python-dotenv
- SCALEDOWN_API_KEY for compression (optional)
- OLLAMA_BASE_URL for local LLM (default: http://localhost:11434)
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from analysis.analysis_engine import AnalysisEngine
from analysis.schema_engine import DatasetSchema, SchemaEngine
from llm.scaledown_client import ScaledownClientError, ScaledownCompressionClient
from llm.ollama_client import OllamaClientError, OllamaLLMClient
from memory.store import MemoryStore, StoredAnalysis, StoredInsight, StoredSchema
from reasoning.insight_reasoner import InsightReasoner, insights_to_summaries, insight_to_stored_insight_payload


DATA_DIR = Path("data")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_vnum(v: str) -> int:
    if not v:
        return 0
    v = v.strip().lower()
    if v.startswith("v") and v[1:].isdigit():
        return int(v[1:])
    return 0


def _next_version(existing_versions: List[str]) -> str:
    if not existing_versions:
        return "v1"
    mx = max((_parse_vnum(v) for v in existing_versions), default=0)
    return f"v{mx + 1}"


def _latest_version(existing_versions: List[str]) -> Optional[str]:
    if not existing_versions:
        return None
    # Prefer vN semantics; fall back to lexicographic if unknown
    with_nums = [(v, _parse_vnum(v)) for v in existing_versions]
    if any(n > 0 for _, n in with_nums):
        return max(with_nums, key=lambda t: t[1])[0]
    existing_versions.sort()
    return existing_versions[-1]


def _find_version_by_hash(store: MemoryStore, dataset_id: str, file_hash: str) -> Optional[str]:
    for v in store.list_versions(dataset_id):
        s = store.get_schema(dataset_id, v)
        if s and s.file_hash == file_hash:
            return v
    return None


# -----------------------------
# Request / Response models
# -----------------------------


class IngestRequest(BaseModel):
    filename: str = Field(..., description="CSV/Parquet file located under data/")
    dataset_id: Optional[str] = Field(None, description="Defaults to filename stem")


class IngestResponse(BaseModel):
    dataset_id: str
    version: str
    file_hash: str
    cached: bool


class AnalyzeRequest(BaseModel):
    dataset_id: str
    version: Optional[str] = None


class AnalyzeResponse(BaseModel):
    dataset_id: str
    version: str
    schema_cached: bool
    analysis_cached: bool
    insights_cached: bool
    insights_created: int


class QueryRequest(BaseModel):
    dataset_id: str
    version: Optional[str] = None
    question: str


class QueryResponse(BaseModel):
    dataset_id: str
    version: str
    query_hash: str
    cached: bool
    answer: str


class CompareRequest(BaseModel):
    dataset_id: str
    base_version: str
    compare_version: str


class CompareResponse(BaseModel):
    dataset_id: str
    base_version: str
    compare_version: str
    drift_report: Dict[str, Any]


def create_app() -> FastAPI:
    # Load environment variables at app startup (no-op if .env is missing).
    load_dotenv()

    app = FastAPI(title="Advanced Data Analysis Agent")

    # Core singletons (thin orchestration layer)
    store = MemoryStore(persist_path=os.getenv("MEMORY_STORE_PATH"))
    schema_engine = SchemaEngine(data_dir=DATA_DIR)
    analysis_engine = AnalysisEngine()
    
    # LLM Pipeline:
    # 1. Ollama for local LLM generation (free, fully local)
    llm = OllamaLLMClient()
    
    # 2. Optional: ScaleDown for prompt compression (reduces token count)
    compression_client = None
    if os.getenv("SCALEDOWN_API_KEY"):
        try:
            compression_client = ScaledownCompressionClient()
        except ScaledownClientError as e:
            print(f"Warning: ScaleDown compression not available ({e}), proceeding without compression")
    
    # Reasoner with optional compression
    reasoner = InsightReasoner(llm, compression_client=compression_client)

    @app.post("/ingest", response_model=IngestResponse)
    def ingest(req: IngestRequest) -> IngestResponse:
        DATA_DIR.mkdir(exist_ok=True)
        path = DATA_DIR / req.filename
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"File not found under data/: {req.filename}")

        dataset_id = req.dataset_id or path.stem
        file_hash = _sha256_file(path)

        # Memory-first: if this file_hash is already ingested, do not recompute schema.
        existing_v = _find_version_by_hash(store, dataset_id, file_hash)
        if existing_v:
            return IngestResponse(dataset_id=dataset_id, version=existing_v, file_hash=file_hash, cached=True)

        version = _next_version(store.list_versions(dataset_id))
        ds_schema = schema_engine.extract_schema(file_path=path, dataset_id=dataset_id, version=version)
        store.save_schema(
            StoredSchema(
                dataset_id=dataset_id,
                version=version,
                file_hash=ds_schema.file_hash,
                compressed_schema_json=ds_schema.to_compressed_json(),
            )
        )
        return IngestResponse(dataset_id=dataset_id, version=version, file_hash=ds_schema.file_hash, cached=False)

    @app.post("/analyze", response_model=AnalyzeResponse)
    def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
        dataset_id = req.dataset_id
        version = req.version or _latest_version(store.list_versions(dataset_id))
        if not version:
            raise HTTPException(status_code=404, detail="No ingested versions found. Call /ingest first.")

        # Memory-first schema
        stored_schema = store.get_schema(dataset_id, version)
        if not stored_schema:
            raise HTTPException(status_code=404, detail="Schema not found in memory. Call /ingest first.")
        schema_cached = True

        # Memory-first analysis
        analysis_cached = store.has_analysis(dataset_id, version)
        if not analysis_cached:
            ds_schema = DatasetSchema.from_compressed_json(stored_schema.compressed_schema_json)
            file_path = Path(ds_schema.file_path)
            if not file_path.exists():
                raise HTTPException(status_code=404, detail=f"Underlying data file missing: {ds_schema.file_path}")

            result = analysis_engine.analyze_file(file_path=file_path, dataset_id=dataset_id, version=version)
            store.save_analysis(
                StoredAnalysis(
                    dataset_id=dataset_id,
                    version=version,
                    analysis_result=result.to_compressed_json(),
                    created_at=result.created_at,
                )
            )

        stored_analysis = store.get_analysis(dataset_id, version)
        if not stored_analysis:
            raise HTTPException(status_code=500, detail="Analysis missing after save (unexpected).")

        # Memory-first insights (version-scoped)
        existing = store.list_insights(dataset_id)
        existing_for_version = [i for i in existing if i.version == version]
        insights_cached = len(existing_for_version) > 0
        insights_created = 0

        if not insights_cached:
            existing_summaries = [i.summary for i in existing]  # dataset-level (helps dedup across versions)
            try:
                new_insights = reasoner.synthesize_insights(
                    dataset_id=dataset_id,
                    version=version,
                    compressed_schema_json=stored_schema.compressed_schema_json,
                    compressed_analysis_result_json=stored_analysis.analysis_result,
                    existing_insight_summaries=existing_summaries,
                )
            except (ScaledownClientError, OllamaClientError) as e:
                # Deterministic behavior: do not retry here, and do not hide the failure.
                # Analysis is already stored at this point; insights are simply unavailable.
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM unavailable for insight synthesis. Check Ollama is running at http://localhost:11434. Error: {e}",
                )
            for ins in new_insights:
                payload = insight_to_stored_insight_payload(ins, dataset_id=dataset_id, version=version)
                store.save_insight(StoredInsight(**payload))
                insights_created += 1

        return AnalyzeResponse(
            dataset_id=dataset_id,
            version=version,
            schema_cached=schema_cached,
            analysis_cached=analysis_cached,
            insights_cached=insights_cached,
            insights_created=insights_created,
        )

    @app.post("/query", response_model=QueryResponse)
    def query(req: QueryRequest) -> QueryResponse:
        dataset_id = req.dataset_id
        version = req.version or _latest_version(store.list_versions(dataset_id))
        if not version:
            raise HTTPException(status_code=404, detail="No ingested versions found. Call /ingest first.")

        stored_schema = store.get_schema(dataset_id, version)
        if not stored_schema:
            raise HTTPException(status_code=404, detail="Schema not found in memory. Call /ingest first.")

        stored_analysis = store.get_analysis(dataset_id, version)  # analysis is optional for some queries
        insights = [i for i in store.list_insights(dataset_id) if i.version == version]
        summaries = [i.summary for i in insights]

        # Query cache (include version to avoid stale answers)
        query_hash = MemoryStore.stable_hash(f"{dataset_id}|{version}|{req.question}")
        cached = store.get_cached_query(query_hash, dataset_id)
        if cached:
            return QueryResponse(
                dataset_id=dataset_id,
                version=version,
                query_hash=query_hash,
                cached=True,
                answer=cached.response,
            )

        # LLM reasoning lives in reasoning layer (not in API)
        try:
            answer = reasoner.answer_query(
                dataset_id=dataset_id,
                version=version,
                question=req.question,
                compressed_schema_json=stored_schema.compressed_schema_json,
                compressed_analysis_result_json=stored_analysis.analysis_result if stored_analysis else None,
                insight_summaries=summaries,
            )
        except (ScaledownClientError, OllamaClientError) as e:
            raise HTTPException(
                status_code=502,
                detail=f"LLM unavailable for query answering. Check Ollama is running at http://localhost:11434. Error: {e}",
            )

        store.save_cached_query(query_hash=query_hash, dataset_id=dataset_id, response=answer)
        return QueryResponse(
            dataset_id=dataset_id,
            version=version,
            query_hash=query_hash,
            cached=False,
            answer=answer,
        )

    @app.post("/compare", response_model=CompareResponse)
    def compare(req: CompareRequest) -> CompareResponse:
        dataset_id = req.dataset_id
        base = store.get_schema(dataset_id, req.base_version)
        comp = store.get_schema(dataset_id, req.compare_version)
        if not base or not comp:
            raise HTTPException(status_code=404, detail="Both schema versions must exist in memory.")

        base_schema = DatasetSchema.from_compressed_json(base.compressed_schema_json)
        comp_schema = DatasetSchema.from_compressed_json(comp.compressed_schema_json)

        report = schema_engine.generate_drift_report(base_schema, comp_schema, base_df=None, compare_df=None)
        return CompareResponse(
            dataset_id=dataset_id,
            base_version=req.base_version,
            compare_version=req.compare_version,
            drift_report=asdict(report),
        )

    return app


app = create_app()

