"""
Analysis Engine: Deterministic EDA signal generation.

This module produces *raw analytical signals* (not business reasoning):
- Univariate distributions (numeric + categorical + datetime summaries)
- Correlations (numeric-numeric)
- Outlier signals (IQR + robust z-score)

Design goals:
- Deterministic computation (pandas / numpy / scipy)
- Compressed outputs suitable for LLM context (downstream reasoner)
- No prioritization / business impact assessment here
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd


# -----------------------------
# Dataclasses: Signals
# -----------------------------


@dataclass(frozen=True)
class NumericDistributionSignal:
    column: str
    dtype: str
    sample_n: int
    null_percentage: float
    mean: Optional[float]
    median: Optional[float]
    std: Optional[float]
    min: Optional[float]
    p05: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    p95: Optional[float]
    max: Optional[float]
    skew: Optional[float]
    kurtosis: Optional[float]
    histogram_bins: Optional[List[float]]
    histogram_counts: Optional[List[int]]


@dataclass(frozen=True)
class CategoricalDistributionSignal:
    column: str
    dtype: str
    sample_n: int
    null_percentage: float
    cardinality: int
    top_values: Dict[str, int]  # compressed: top-k only
    other_count: int  # remaining mass not shown in top_values


@dataclass(frozen=True)
class DatetimeDistributionSignal:
    column: str
    dtype: str
    sample_n: int
    null_percentage: float
    min: Optional[str]
    max: Optional[str]
    # compressed periodicity hints
    inferred_granularity: Optional[str]  # "second|minute|hour|day|month|year|mixed|unknown"


@dataclass(frozen=True)
class CorrelationSignal:
    method: str  # "pearson" | "spearman"
    col_x: str
    col_y: str
    n: int
    correlation: float


@dataclass(frozen=True)
class OutlierSignal:
    column: str
    method: str  # "iqr" | "robust_z"
    sample_n: int
    outlier_count: int
    outlier_fraction: float
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    median: Optional[float] = None
    mad: Optional[float] = None
    extreme_values: Optional[List[float]] = None  # compressed: top-n most extreme values


@dataclass
class AnalysisResult:
    dataset_id: str
    version: str
    created_at: str
    row_count: int
    column_count: int
    sample_n: int
    numeric_distributions: List[NumericDistributionSignal]
    categorical_distributions: List[CategoricalDistributionSignal]
    datetime_distributions: List[DatetimeDistributionSignal]
    correlations: List[CorrelationSignal]
    outliers: List[OutlierSignal]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "created_at": self.created_at,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "sample_n": self.sample_n,
            "numeric_distributions": [asdict(x) for x in self.numeric_distributions],
            "categorical_distributions": [asdict(x) for x in self.categorical_distributions],
            "datetime_distributions": [asdict(x) for x in self.datetime_distributions],
            "correlations": [asdict(x) for x in self.correlations],
            "outliers": [asdict(x) for x in self.outliers],
            "notes": list(self.notes),
        }

    def to_compressed_json(self) -> str:
        # tight separators minimize LLM context size
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisResult":
        return cls(
            dataset_id=data["dataset_id"],
            version=data["version"],
            created_at=data["created_at"],
            row_count=int(data["row_count"]),
            column_count=int(data["column_count"]),
            sample_n=int(data["sample_n"]),
            numeric_distributions=[NumericDistributionSignal(**x) for x in data.get("numeric_distributions", [])],
            categorical_distributions=[
                CategoricalDistributionSignal(**x) for x in data.get("categorical_distributions", [])
            ],
            datetime_distributions=[DatetimeDistributionSignal(**x) for x in data.get("datetime_distributions", [])],
            correlations=[CorrelationSignal(**x) for x in data.get("correlations", [])],
            outliers=[OutlierSignal(**x) for x in data.get("outliers", [])],
            notes=data.get("notes", []),
        )

    @classmethod
    def from_compressed_json(cls, json_str: str) -> "AnalysisResult":
        return cls.from_dict(json.loads(json_str))


# -----------------------------
# Analysis Engine
# -----------------------------


class AnalysisEngine:
    """
    Deterministic EDA engine that emits compressed analytical signals.

    Notes:
    - This engine may apply sampling for performance on large datasets.
    - Sampling is deterministic via a fixed random seed.
    - Correlation results are compressed via top-K by absolute correlation.
    """

    def __init__(
        self,
        sample_size: int = 200_000,
        random_state: int = 42,
        numeric_hist_bins: int = 10,
        categorical_top_k: int = 10,
        correlation_methods: Sequence[str] = ("pearson", "spearman"),
        correlation_top_k: int = 50,
        outlier_extremes_k: int = 10,
    ) -> None:
        self.sample_size = int(sample_size)
        self.random_state = int(random_state)
        self.numeric_hist_bins = int(numeric_hist_bins)
        self.categorical_top_k = int(categorical_top_k)
        self.correlation_methods = tuple(correlation_methods)
        self.correlation_top_k = int(correlation_top_k)
        self.outlier_extremes_k = int(outlier_extremes_k)

    def load_dataset(self, file_path: Union[str, Path]) -> pd.DataFrame:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Dataset file not found: {file_path}")

        suffix = file_path.suffix.lower()
        if suffix == ".parquet":
            return pd.read_parquet(file_path)
        if suffix == ".csv":
            return pd.read_csv(file_path, low_memory=False)
        raise ValueError(f"Unsupported file format: {suffix}")

    def _maybe_sample(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
        n = len(df)
        if self.sample_size is None or n <= self.sample_size:
            return df, n
        return df.sample(n=self.sample_size, random_state=self.random_state), self.sample_size

    def _null_percentage(self, s: pd.Series, total_n: int) -> float:
        if total_n <= 0:
            return 0.0
        return float(s.isna().sum() / total_n * 100.0)

    def _numeric_distribution(self, s: pd.Series, total_n: int) -> NumericDistributionSignal:
        x = pd.to_numeric(s, errors="coerce")
        x_nonnull = x.dropna()
        sample_n = int(len(x))

        if len(x_nonnull) == 0:
            return NumericDistributionSignal(
                column=str(s.name),
                dtype=str(s.dtype),
                sample_n=sample_n,
                null_percentage=self._null_percentage(s, total_n),
                mean=None,
                median=None,
                std=None,
                min=None,
                p05=None,
                p25=None,
                p75=None,
                p95=None,
                max=None,
                skew=None,
                kurtosis=None,
                histogram_bins=None,
                histogram_counts=None,
            )

        # Quantiles are deterministic and compact
        q05, q25, q50, q75, q95 = np.quantile(
            x_nonnull.to_numpy(dtype=float), [0.05, 0.25, 0.50, 0.75, 0.95]
        ).tolist()

        hist_bins: Optional[List[float]] = None
        hist_counts: Optional[List[int]] = None
        if len(x_nonnull) >= max(20, self.numeric_hist_bins * 2):
            counts, bins = np.histogram(x_nonnull.to_numpy(dtype=float), bins=self.numeric_hist_bins)
            hist_bins = bins.tolist()
            hist_counts = counts.tolist()

        # Pandas skew/kurt are deterministic; NaN if too small
        skew = float(x_nonnull.skew()) if len(x_nonnull) >= 3 else None
        kurt = float(x_nonnull.kurt()) if len(x_nonnull) >= 4 else None

        std = float(x_nonnull.std(ddof=1)) if len(x_nonnull) >= 2 else 0.0

        return NumericDistributionSignal(
            column=str(s.name),
            dtype=str(s.dtype),
            sample_n=sample_n,
            null_percentage=self._null_percentage(s, total_n),
            mean=float(x_nonnull.mean()),
            median=float(q50),
            std=std,
            min=float(x_nonnull.min()),
            p05=float(q05),
            p25=float(q25),
            p75=float(q75),
            p95=float(q95),
            max=float(x_nonnull.max()),
            skew=skew,
            kurtosis=kurt,
            histogram_bins=hist_bins,
            histogram_counts=hist_counts,
        )

    def _categorical_distribution(self, s: pd.Series, total_n: int) -> CategoricalDistributionSignal:
        sample_n = int(len(s))
        # Convert to string for stable keys; keep NaNs out
        s_nonnull = s.dropna().astype(str)
        vc = s_nonnull.value_counts(dropna=True)
        cardinality = int(vc.shape[0])

        top = vc.head(self.categorical_top_k)
        top_values = {str(k): int(v) for k, v in top.items()}
        other_count = int(vc.sum() - top.sum())

        return CategoricalDistributionSignal(
            column=str(s.name),
            dtype=str(s.dtype),
            sample_n=sample_n,
            null_percentage=self._null_percentage(s, total_n),
            cardinality=cardinality,
            top_values=top_values,
            other_count=other_count,
        )

    def _infer_datetime_granularity(self, dt: pd.Series) -> Optional[str]:
        # Uses median delta between sorted unique timestamps; compressed heuristic
        dt = pd.to_datetime(dt, errors="coerce").dropna()
        if len(dt) < 3:
            return None
        uniq = dt.sort_values().drop_duplicates()
        if len(uniq) < 3:
            return None
        deltas = uniq.diff().dropna()
        if len(deltas) == 0:
            return None
        median_seconds = float(deltas.median().total_seconds())
        if median_seconds <= 0:
            return "unknown"
        # bucket to human-friendly granularity
        if median_seconds < 60:
            return "second"
        if median_seconds < 3600:
            return "minute"
        if median_seconds < 86400:
            return "hour"
        if median_seconds < 86400 * 32:
            return "day"
        if median_seconds < 86400 * 366:
            return "month"
        return "year"

    def _datetime_distribution(self, s: pd.Series, total_n: int) -> DatetimeDistributionSignal:
        sample_n = int(len(s))
        dt = pd.to_datetime(s, errors="coerce")
        dt_nonnull = dt.dropna()
        if len(dt_nonnull) == 0:
            return DatetimeDistributionSignal(
                column=str(s.name),
                dtype=str(s.dtype),
                sample_n=sample_n,
                null_percentage=self._null_percentage(s, total_n),
                min=None,
                max=None,
                inferred_granularity=None,
            )
        return DatetimeDistributionSignal(
            column=str(s.name),
            dtype=str(s.dtype),
            sample_n=sample_n,
            null_percentage=self._null_percentage(s, total_n),
            min=str(dt_nonnull.min()),
            max=str(dt_nonnull.max()),
            inferred_granularity=self._infer_datetime_granularity(dt_nonnull),
        )

    def _correlation_signals(self, df: pd.DataFrame, numeric_cols: List[str]) -> List[CorrelationSignal]:
        if len(numeric_cols) < 2:
            return []

        # Use pairwise complete observations for each method; compress output via top-K abs corr
        signals: List[CorrelationSignal] = []
        x = df[numeric_cols].apply(pd.to_numeric, errors="coerce")

        for method in self.correlation_methods:
            corr = x.corr(method=method)
            # extract upper triangle
            pairs: List[Tuple[str, str, float, int]] = []
            for i, col_x in enumerate(numeric_cols):
                for j in range(i + 1, len(numeric_cols)):
                    col_y = numeric_cols[j]
                    c = corr.loc[col_x, col_y]
                    if pd.isna(c):
                        continue
                    # n: pairwise non-null count
                    n = int(x[[col_x, col_y]].dropna().shape[0])
                    pairs.append((col_x, col_y, float(c), n))

            pairs.sort(key=lambda t: abs(t[2]), reverse=True)
            for col_x, col_y, c, n in pairs[: self.correlation_top_k]:
                signals.append(
                    CorrelationSignal(method=str(method), col_x=col_x, col_y=col_y, n=n, correlation=float(c))
                )

        return signals

    def _outlier_signals(self, df: pd.DataFrame, numeric_cols: List[str]) -> List[OutlierSignal]:
        out: List[OutlierSignal] = []

        for col in numeric_cols:
            s = pd.to_numeric(df[col], errors="coerce")
            s_nonnull = s.dropna()
            sample_n = int(len(s))
            if len(s_nonnull) < 8:
                continue

            # IQR method
            q25, q50, q75 = np.quantile(s_nonnull.to_numpy(dtype=float), [0.25, 0.5, 0.75]).tolist()
            iqr = float(q75 - q25)
            if iqr == 0.0:
                lower, upper = float(q25), float(q75)
            else:
                lower, upper = float(q25 - 1.5 * iqr), float(q75 + 1.5 * iqr)

            mask = (s_nonnull < lower) | (s_nonnull > upper)
            outlier_vals = s_nonnull[mask].to_numpy(dtype=float)
            outlier_count = int(outlier_vals.size)
            outlier_fraction = float(outlier_count / max(1, len(s_nonnull)))

            extreme_values = None
            if outlier_count > 0:
                # store most extreme by distance from median (compressed)
                dist = np.abs(outlier_vals - float(q50))
                idx = np.argsort(dist)[::-1][: self.outlier_extremes_k]
                extreme_values = [float(outlier_vals[i]) for i in idx]

            out.append(
                OutlierSignal(
                    column=col,
                    method="iqr",
                    sample_n=sample_n,
                    outlier_count=outlier_count,
                    outlier_fraction=outlier_fraction,
                    lower_bound=float(lower),
                    upper_bound=float(upper),
                    extreme_values=extreme_values,
                )
            )

            # Robust z-score via MAD (median absolute deviation)
            median = float(q50)
            abs_dev = np.abs(s_nonnull.to_numpy(dtype=float) - median)
            mad = float(np.median(abs_dev))
            if mad > 0:
                # 0.6745 makes MAD consistent w/ std for normal; thresholding is just signal extraction here
                robust_z = 0.6745 * (s_nonnull.to_numpy(dtype=float) - median) / mad
                mask2 = np.abs(robust_z) > 3.5
                outlier_vals2 = s_nonnull.to_numpy(dtype=float)[mask2]
                outlier_count2 = int(outlier_vals2.size)
                outlier_fraction2 = float(outlier_count2 / max(1, len(s_nonnull)))

                extreme_values2 = None
                if outlier_count2 > 0:
                    dist2 = np.abs(outlier_vals2 - median)
                    idx2 = np.argsort(dist2)[::-1][: self.outlier_extremes_k]
                    extreme_values2 = [float(outlier_vals2[i]) for i in idx2]

                out.append(
                    OutlierSignal(
                        column=col,
                        method="robust_z",
                        sample_n=sample_n,
                        outlier_count=outlier_count2,
                        outlier_fraction=outlier_fraction2,
                        median=median,
                        mad=mad,
                        extreme_values=extreme_values2,
                    )
                )

        return out

    def analyze(
        self,
        df: pd.DataFrame,
        dataset_id: str = "dataset",
        version: Optional[str] = None,
    ) -> AnalysisResult:
        """
        Produce deterministic analytical signals from a dataframe.

        Important: This function does not do business reasoning or prioritization.
        """
        if version is None:
            version = datetime.now().isoformat()

        df_s, sample_n = self._maybe_sample(df)
        notes: List[str] = []
        if sample_n < len(df):
            notes.append(f"analysis_used_sampling=true sample_n={sample_n} full_n={len(df)}")

        # Identify column types
        numeric_cols = [c for c in df_s.columns if pd.api.types.is_numeric_dtype(df_s[c])]
        datetime_cols = [c for c in df_s.columns if pd.api.types.is_datetime64_any_dtype(df_s[c])]

        # Treat low-cardinality numerics as numeric here; schema engine captures cardinality anyway.
        categorical_cols = [
            c
            for c in df_s.columns
            if c not in numeric_cols and c not in datetime_cols
        ]

        numeric_distributions: List[NumericDistributionSignal] = []
        categorical_distributions: List[CategoricalDistributionSignal] = []
        datetime_distributions: List[DatetimeDistributionSignal] = []

        total_n = len(df_s)

        for c in numeric_cols:
            numeric_distributions.append(self._numeric_distribution(df_s[c], total_n=total_n))

        for c in categorical_cols:
            categorical_distributions.append(self._categorical_distribution(df_s[c], total_n=total_n))

        for c in datetime_cols:
            datetime_distributions.append(self._datetime_distribution(df_s[c], total_n=total_n))

        correlations = self._correlation_signals(df_s, numeric_cols=numeric_cols)
        outliers = self._outlier_signals(df_s, numeric_cols=numeric_cols)

        return AnalysisResult(
            dataset_id=str(dataset_id),
            version=str(version),
            created_at=datetime.now().isoformat(),
            row_count=int(len(df)),
            column_count=int(df.shape[1]),
            sample_n=int(sample_n),
            numeric_distributions=numeric_distributions,
            categorical_distributions=categorical_distributions,
            datetime_distributions=datetime_distributions,
            correlations=correlations,
            outliers=outliers,
            notes=notes,
        )

    def analyze_file(
        self,
        file_path: Union[str, Path],
        dataset_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> AnalysisResult:
        df = self.load_dataset(file_path)
        if dataset_id is None:
            dataset_id = Path(file_path).stem
        return self.analyze(df=df, dataset_id=str(dataset_id), version=version)

