"""
Schema Engine: Extract, compress, version, and detect drift in dataset schemas.

This module handles:
- Loading CSV/Parquet files
- Extracting compressed schema representations
- Versioning datasets
- Detecting schema and distribution drift
"""

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
import pandas as pd
import numpy as np


@dataclass
class ColumnSchema:
    """Compressed representation of a single column's schema."""
    name: str
    dtype: str
    null_percentage: float
    cardinality: int
    unique_ratio: float  # unique_count / total_count
    # Basic stats (compressed - only essential)
    mean: Optional[float] = None
    median: Optional[float] = None
    std: Optional[float] = None
    min: Optional[Union[float, str]] = None
    max: Optional[Union[float, str]] = None
    # For categorical columns
    top_values: Optional[Dict[str, int]] = None  # {value: count} for top 5
    # Distribution signature (compressed histogram)
    histogram_bins: Optional[List[float]] = None  # 10-bin histogram for numeric
    histogram_counts: Optional[List[int]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ColumnSchema':
        """Create from dictionary."""
        return cls(**data)


@dataclass
class DatasetSchema:
    """Compressed schema representation of an entire dataset."""
    dataset_id: str
    version: str
    file_path: str
    file_hash: str  # SHA256 of file content
    row_count: int
    column_count: int
    created_at: str
    columns: Dict[str, ColumnSchema]  # column_name -> ColumnSchema
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = asdict(self)
        result['columns'] = {k: v.to_dict() for k, v in self.columns.items()}
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DatasetSchema':
        """Create from dictionary."""
        data['columns'] = {
            k: ColumnSchema.from_dict(v) 
            for k, v in data['columns'].items()
        }
        return cls(**data)
    
    def to_compressed_json(self) -> str:
        """Serialize to compressed JSON string."""
        return json.dumps(self.to_dict(), separators=(',', ':'))
    
    @classmethod
    def from_compressed_json(cls, json_str: str) -> 'DatasetSchema':
        """Deserialize from compressed JSON string."""
        return cls.from_dict(json.loads(json_str))


@dataclass
class SchemaDrift:
    """Represents schema-level drift between two dataset versions."""
    added_columns: List[str]
    removed_columns: List[str]
    type_changes: Dict[str, tuple]  # column -> (old_type, new_type)
    null_percentage_changes: Dict[str, tuple]  # column -> (old_%, new_%)
    cardinality_changes: Dict[str, tuple]  # column -> (old_card, new_card)
    
    def has_drift(self) -> bool:
        """Check if any drift was detected."""
        return (
            len(self.added_columns) > 0 or
            len(self.removed_columns) > 0 or
            len(self.type_changes) > 0 or
            len(self.null_percentage_changes) > 0 or
            len(self.cardinality_changes) > 0
        )


@dataclass
class DistributionDrift:
    """Represents distribution-level drift for a single column."""
    column_name: str
    # Statistical tests (compressed results)
    ks_statistic: Optional[float] = None  # Kolmogorov-Smirnov test
    chi2_statistic: Optional[float] = None  # Chi-square for categorical
    p_value: Optional[float] = None
    # Summary metrics
    mean_shift: Optional[float] = None
    std_shift: Optional[float] = None
    # Distribution similarity (0-1, higher = more similar)
    similarity_score: float = 1.0
    
    def has_significant_drift(self, p_threshold: float = 0.05) -> bool:
        """Check if drift is statistically significant."""
        if self.p_value is None:
            return False
        return self.p_value < p_threshold and self.similarity_score < 0.8


@dataclass
class DriftReport:
    """Complete drift analysis between two schema versions."""
    base_version: str
    compare_version: str
    schema_drift: SchemaDrift
    distribution_drifts: Dict[str, DistributionDrift]  # column -> drift
    overall_drift_score: float  # 0-1, higher = more drift
    
    def has_drift(self) -> bool:
        """Check if any drift was detected."""
        return (
            self.schema_drift.has_drift() or
            any(d.has_significant_drift() for d in self.distribution_drifts.values())
        )


class SchemaEngine:
    """Engine for extracting, compressing, and comparing dataset schemas."""
    
    def __init__(self, data_dir: Union[str, Path] = "data"):
        """
        Initialize the schema engine.
        
        Args:
            data_dir: Directory where datasets are stored
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
    
    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file content."""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def _extract_column_schema(
        self, 
        df: pd.DataFrame, 
        col_name: str,
        sample_size: Optional[int] = None
    ) -> ColumnSchema:
        """
        Extract compressed schema for a single column.
        
        Args:
            df: DataFrame
            col_name: Name of the column
            sample_size: Optional sample size for large datasets
        """
        col = df[col_name]
        total_count = len(col)
        
        # Handle sampling for very large datasets
        if sample_size and total_count > sample_size:
            col = col.sample(n=sample_size, random_state=42)
            sample_count = len(col)
        else:
            sample_count = total_count
        
        # Basic metadata
        dtype_str = str(col.dtype)
        null_count = col.isnull().sum()
        null_percentage = (null_count / total_count) * 100 if total_count > 0 else 0.0
        unique_count = col.nunique()
        cardinality = unique_count
        unique_ratio = unique_count / total_count if total_count > 0 else 0.0
        
        # Initialize optional fields
        mean = None
        median = None
        std = None
        min_val = None
        max_val = None
        top_values = None
        histogram_bins = None
        histogram_counts = None
        
        # Numeric columns
        if pd.api.types.is_numeric_dtype(col):
            numeric_col = col.dropna()
            if len(numeric_col) > 0:
                mean = float(numeric_col.mean())
                median = float(numeric_col.median())
                std = float(numeric_col.std()) if len(numeric_col) > 1 else 0.0
                min_val = float(numeric_col.min())
                max_val = float(numeric_col.max())
                
                # Compressed histogram (10 bins)
                if len(numeric_col) > 10:
                    counts, bins = np.histogram(numeric_col, bins=10)
                    histogram_bins = bins.tolist()
                    histogram_counts = counts.tolist()
        
        # Categorical/object columns
        elif pd.api.types.is_object_dtype(col) or pd.api.types.is_categorical_dtype(col):
            value_counts = col.value_counts()
            if len(value_counts) > 0:
                # Top 5 values
                top_values = {
                    str(k): int(v) 
                    for k, v in value_counts.head(5).items()
                }
                min_val = str(value_counts.index[0]) if len(value_counts) > 0 else None
                max_val = str(value_counts.index[-1]) if len(value_counts) > 0 else None
        
        # Datetime columns
        elif pd.api.types.is_datetime64_any_dtype(col):
            datetime_col = col.dropna()
            if len(datetime_col) > 0:
                min_val = str(datetime_col.min())
                max_val = str(datetime_col.max())
        
        return ColumnSchema(
            name=col_name,
            dtype=dtype_str,
            null_percentage=null_percentage,
            cardinality=cardinality,
            unique_ratio=unique_ratio,
            mean=mean,
            median=median,
            std=std,
            min=min_val,
            max=max_val,
            top_values=top_values,
            histogram_bins=histogram_bins,
            histogram_counts=histogram_counts
        )
    
    def extract_schema(
        self,
        file_path: Union[str, Path],
        dataset_id: Optional[str] = None,
        version: Optional[str] = None,
        sample_size: Optional[int] = 100000
    ) -> DatasetSchema:
        """
        Extract compressed schema from a dataset file.
        
        Args:
            file_path: Path to CSV or Parquet file
            dataset_id: Optional dataset identifier (defaults to filename stem)
            version: Optional version string (defaults to timestamp)
            sample_size: Sample size for large datasets (None = no sampling)
        
        Returns:
            DatasetSchema object
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Dataset file not found: {file_path}")
        
        # Generate identifiers
        if dataset_id is None:
            dataset_id = file_path.stem
        if version is None:
            version = datetime.now().isoformat()
        
        # Compute file hash
        file_hash = self._compute_file_hash(file_path)
        
        # Load dataset
        if file_path.suffix.lower() == '.parquet':
            df = pd.read_parquet(file_path)
        elif file_path.suffix.lower() == '.csv':
            df = pd.read_csv(file_path, low_memory=False)
        else:
            raise ValueError(f"Unsupported file format: {file_path.suffix}")
        
        # Extract column schemas
        columns = {}
        for col_name in df.columns:
            columns[col_name] = self._extract_column_schema(
                df, col_name, sample_size=sample_size
            )
        
        return DatasetSchema(
            dataset_id=dataset_id,
            version=version,
            file_path=str(file_path),
            file_hash=file_hash,
            row_count=len(df),
            column_count=len(df.columns),
            created_at=datetime.now().isoformat(),
            columns=columns
        )
    
    def detect_schema_drift(
        self,
        base_schema: DatasetSchema,
        compare_schema: DatasetSchema
    ) -> SchemaDrift:
        """
        Detect schema-level drift between two versions.
        
        Args:
            base_schema: Base version schema
            compare_schema: Schema to compare against
        
        Returns:
            SchemaDrift object
        """
        base_cols = set(base_schema.columns.keys())
        compare_cols = set(compare_schema.columns.keys())
        
        added_columns = list(compare_cols - base_cols)
        removed_columns = list(base_cols - compare_cols)
        common_columns = base_cols & compare_cols
        
        type_changes = {}
        null_percentage_changes = {}
        cardinality_changes = {}
        
        for col in common_columns:
            base_col = base_schema.columns[col]
            compare_col = compare_schema.columns[col]
            
            # Type changes
            if base_col.dtype != compare_col.dtype:
                type_changes[col] = (base_col.dtype, compare_col.dtype)
            
            # Null percentage changes (significant if > 5% change)
            null_diff = abs(base_col.null_percentage - compare_col.null_percentage)
            if null_diff > 5.0:
                null_percentage_changes[col] = (
                    base_col.null_percentage,
                    compare_col.null_percentage
                )
            
            # Cardinality changes (significant if > 20% change)
            if base_col.cardinality > 0:
                card_ratio = compare_col.cardinality / base_col.cardinality
                if card_ratio < 0.8 or card_ratio > 1.2:
                    cardinality_changes[col] = (
                        base_col.cardinality,
                        compare_col.cardinality
                    )
        
        return SchemaDrift(
            added_columns=added_columns,
            removed_columns=removed_columns,
            type_changes=type_changes,
            null_percentage_changes=null_percentage_changes,
            cardinality_changes=cardinality_changes
        )
    
    def detect_distribution_drift(
        self,
        base_schema: DatasetSchema,
        compare_schema: DatasetSchema,
        column_name: str,
        base_df: Optional[pd.DataFrame] = None,
        compare_df: Optional[pd.DataFrame] = None
    ) -> DistributionDrift:
        """
        Detect distribution drift for a specific column.
        
        Args:
            base_schema: Base version schema
            compare_schema: Schema to compare against
            column_name: Column to analyze
            base_df: Optional base DataFrame (for detailed analysis)
            compare_df: Optional compare DataFrame (for detailed analysis)
        
        Returns:
            DistributionDrift object
        """
        if column_name not in base_schema.columns or column_name not in compare_schema.columns:
            raise ValueError(f"Column {column_name} not found in both schemas")
        
        base_col = base_schema.columns[column_name]
        compare_col = compare_schema.columns[column_name]
        
        # If DataFrames provided, use statistical tests
        ks_statistic = None
        chi2_statistic = None
        p_value = None
        
        if base_df is not None and compare_df is not None:
            if column_name in base_df.columns and column_name in compare_df.columns:
                base_series = base_df[column_name].dropna()
                compare_series = compare_df[column_name].dropna()
                
                if pd.api.types.is_numeric_dtype(base_series):
                    # Kolmogorov-Smirnov test for numeric
                    from scipy import stats
                    if len(base_series) > 0 and len(compare_series) > 0:
                        ks_statistic, p_value = stats.ks_2samp(base_series, compare_series)
                elif pd.api.types.is_object_dtype(base_series):
                    # Chi-square test for categorical
                    from scipy import stats
                    # Get common categories
                    all_cats = set(base_series.unique()) | set(compare_series.unique())
                    base_counts = [sum(base_series == cat) for cat in all_cats]
                    compare_counts = [sum(compare_series == cat) for cat in all_cats]
                    if sum(base_counts) > 0 and sum(compare_counts) > 0:
                        chi2_statistic, p_value = stats.chisquare(base_counts, compare_counts)
        
        # Compute similarity score from compressed histograms
        similarity_score = 1.0
        if base_col.histogram_bins and compare_col.histogram_bins:
            # Simple histogram overlap metric
            base_counts = np.array(base_col.histogram_counts or [])
            compare_counts = np.array(compare_col.histogram_counts or [])
            if len(base_counts) > 0 and len(compare_counts) > 0:
                # Normalize
                base_norm = base_counts / (base_counts.sum() + 1e-10)
                compare_norm = compare_counts / (compare_counts.sum() + 1e-10)
                # Cosine similarity
                similarity_score = float(
                    np.dot(base_norm, compare_norm) / 
                    (np.linalg.norm(base_norm) * np.linalg.norm(compare_norm) + 1e-10)
                )
        
        # Mean/std shifts for numeric
        mean_shift = None
        std_shift = None
        if base_col.mean is not None and compare_col.mean is not None:
            mean_shift = compare_col.mean - base_col.mean
        if base_col.std is not None and compare_col.std is not None:
            std_shift = compare_col.std - base_col.std
        
        return DistributionDrift(
            column_name=column_name,
            ks_statistic=ks_statistic,
            chi2_statistic=chi2_statistic,
            p_value=p_value,
            mean_shift=mean_shift,
            std_shift=std_shift,
            similarity_score=similarity_score
        )
    
    def generate_drift_report(
        self,
        base_schema: DatasetSchema,
        compare_schema: DatasetSchema,
        base_df: Optional[pd.DataFrame] = None,
        compare_df: Optional[pd.DataFrame] = None
    ) -> DriftReport:
        """
        Generate complete drift report between two schema versions.
        
        Args:
            base_schema: Base version schema
            compare_schema: Schema to compare against
            base_df: Optional base DataFrame
            compare_df: Optional compare DataFrame
        
        Returns:
            DriftReport object
        """
        # Schema drift
        schema_drift = self.detect_schema_drift(base_schema, compare_schema)
        
        # Distribution drift for common columns
        common_columns = set(base_schema.columns.keys()) & set(compare_schema.columns.keys())
        distribution_drifts = {}
        
        for col in common_columns:
            drift = self.detect_distribution_drift(
                base_schema, compare_schema, col, base_df, compare_df
            )
            distribution_drifts[col] = drift
        
        # Compute overall drift score (0-1, higher = more drift)
        drift_components = []
        
        # Schema changes
        if len(base_schema.columns) > 0:
            drift_components.append(len(schema_drift.added_columns) / len(base_schema.columns))
            drift_components.append(len(schema_drift.removed_columns) / len(base_schema.columns))
            drift_components.append(len(schema_drift.type_changes) / len(base_schema.columns))
        
        # Distribution changes
        if distribution_drifts:
            avg_similarity = np.mean([d.similarity_score for d in distribution_drifts.values()])
            drift_components.append(1.0 - avg_similarity)
        
        overall_drift_score = float(np.mean(drift_components)) if drift_components else 0.0
        
        return DriftReport(
            base_version=base_schema.version,
            compare_version=compare_schema.version,
            schema_drift=schema_drift,
            distribution_drifts=distribution_drifts,
            overall_drift_score=overall_drift_score
        )
