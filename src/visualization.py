"""Output writers for the HEC-RAS validation workflow.

All artifacts (CSV, TXT, PNG) are written with the simulation window embedded
in the filename - `<name>_<start>_<end>.<ext>` - so repeated runs over
different windows don't overwrite each other.
"""
from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, asdict
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------
@dataclass
class Metrics:
    """Goodness-of-fit metrics between a model and observed series."""
    n: int
    mse: float
    rmse: float
    mae: float
    bias: float          # mean(model - observed)
    r2: float            # coefficient of determination (1 - SSres/SStot)
    pearson_r: float     # linear correlation
    nse: float           # Nash-Sutcliffe efficiency (same as r2 when means match)
    # v3.1.0 - Kling-Gupta efficiency + decomposition (TEEHR-style).
    kge: float = float("nan")        # 1 - sqrt((r-1)^2+(alpha-1)^2+(beta-1)^2)
    kge_r: float = float("nan")      # correlation component
    kge_alpha: float = float("nan")  # variability ratio std(m)/std(o)
    kge_beta: float = float("nan")   # bias ratio mean(m)/mean(o)

    @classmethod
    def compute(cls, model: np.ndarray, observed: np.ndarray) -> "Metrics":
        model = np.asarray(model, dtype=float)
        observed = np.asarray(observed, dtype=float)
        mask = np.isfinite(model) & np.isfinite(observed)
        m, o = model[mask], observed[mask]
        n = m.size
        if n < 2:
            raise ValueError("Need at least 2 finite pairs to compute metrics.")

        err = m - o
        mse = float(np.mean(err ** 2))
        rmse = float(np.sqrt(mse))
        mae = float(np.mean(np.abs(err)))
        bias = float(np.mean(err))

        o_mean = float(np.mean(o))
        ss_res = float(np.sum(err ** 2))
        ss_tot = float(np.sum((o - o_mean) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
        nse = r2  # identical formula for observed-mean baseline

        m_mean = float(np.mean(m))
        num = float(np.sum((m - m_mean) * (o - o_mean)))
        den = float(np.sqrt(np.sum((m - m_mean) ** 2) * np.sum((o - o_mean) ** 2)))
        pearson_r = num / den if den > 0 else float("nan")

        # Kling-Gupta efficiency (and its r/alpha/beta decomposition).
        try:
            from evaluation import kling_gupta_efficiency
            kge, kge_r, kge_alpha, kge_beta = kling_gupta_efficiency(m, o)
        except Exception:
            kge = kge_r = kge_alpha = kge_beta = float("nan")

        return cls(n=n, mse=mse, rmse=rmse, mae=mae, bias=bias,
                   r2=r2, pearson_r=pearson_r, nse=nse,
                   kge=kge, kge_r=kge_r, kge_alpha=kge_alpha,
                   kge_beta=kge_beta)

    def as_text(self) -> str:
        return (
            f"n            : {self.n}\n"
            f"MSE          : {self.mse:.6f}\n"
            f"RMSE         : {self.rmse:.6f}\n"
            f"MAE          : {self.mae:.6f}\n"
            f"Bias         : {self.bias:+.6f}\n"
            f"R^2          : {self.r2:.6f}\n"
            f"Pearson r    : {self.pearson_r:.6f}\n"
            f"NSE          : {self.nse:.6f}\n"
            f"KGE          : {self.kge:.6f}\n"
            f"KGE r        : {self.kge_r:.6f}\n"
            f"KGE alpha    : {self.kge_alpha:.6f}\n"
            f"KGE beta     : {self.kge_beta:.6f}\n"
        )


class ResultsWriter:
    """Persist model-vs-observed comparison artifacts for one simulation run."""

    DATE_FMT = "%Y%m%d"  # compact, filesystem-safe

    def __init__(self, outputs_dir: Path | str, start: pd.Timestamp,
                 end: pd.Timestamp, timebase=None):
        self.outputs_dir = Path(outputs_dir)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end)
        # The run's clock (v4.8.0).  When supplied, the saved CSV
        # carries ISO-8601 timestamps WITH the offset and the plot's
        # x-axis names the clock, so neither artifact is an ambiguous
        # bare wall-clock reading.  None keeps the pre-v4.8.0 output.
        self.timebase = timebase

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _suffix(self) -> str:
        return f"{self.start.strftime(self.DATE_FMT)}_{self.end.strftime(self.DATE_FMT)}"

    def _path(self, name: str, ext: str) -> Path:
        return self.outputs_dir / f"{name}_{self._suffix()}.{ext}"

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------
    def save_csv(self, combined: pd.DataFrame, name: str = "model_vs_observed") -> Path:
        out = self._path(name, "csv")
        if self.timebase is not None and self.timebase.known:
            combined = combined.copy()
            combined.index = [self.timebase.iso(t) for t in combined.index]
            combined.index.name = "datetime"
        combined.to_csv(out)
        print(f"Saved CSV: {out}")
        return out

    def save_metrics(self, metrics: Metrics, name: str = "metrics") -> Path:
        out = self._path(name, "txt")
        with open(out, "w") as f:
            f.write(f"Simulation window: {self.start}  ->  {self.end}\n\n")
            f.write(metrics.as_text())
        print(f"Saved metrics: {out}")
        return out

    def save_metrics_json(
        self,
        metrics: Metrics,
        cis: dict | None = None,
        name: str = "metrics_ci",
        extra: dict | None = None,
    ) -> Path:
        """Persist point metrics + bootstrap CIs as machine-readable JSON.

        Consumed by the Validation tab to render a metric table with
        95% confidence-interval columns.
        """
        import json
        out = self._path(name, "json")
        payload = {
            "window": {
                "start": str(self.start),
                "end": str(self.end),
            },
            "n": int(metrics.n),
            "point": asdict(metrics),
            "ci95": cis or {},
        }
        if extra:
            payload.update(extra)
        with open(out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"Saved metrics JSON: {out}")
        return out

    def save_plot(
        self,
        combined: pd.DataFrame,
        name: str = "comparison_plot",
        title: str = "Model vs Observed",
        ylabel: str = "Stage (ft)",
        dpi: int = 600,
        show: bool = False,
    ) -> Path:
        out = self._path(name, "png")
        # Plot on the site's local standard time so the static figure
        # matches the interactive one; the axis label names the clock.
        if self.timebase is not None and self.timebase.known:
            combined = combined.copy()
            combined.index = self.timebase.to_lst(combined.index)
        fig, ax = plt.subplots()
        ax.plot(combined.index, combined["model"], label="Model")
        ax.plot(combined.index, combined["observed"], label="Observed")
        ax.set_xlabel(
            self.timebase.axis_label() if self.timebase is not None
            else "Time",
            fontweight='bold',
        )
        ax.set_ylabel(ylabel, fontweight = 'bold')
        ax.grid(True, linestyle='--', linewidth=0.7, alpha=0.5)
        ax.set_title(f"{title} ({self.start.date()} -> {self.end.date()})")
        ax.legend()
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
        fig.autofmt_xdate(rotation=30)
        fig.tight_layout()
        fig.savefig(out, dpi=dpi)
        if show:
            plt.show()
        plt.close(fig)
        print(f"Saved plot: {out}")
        return out

    def save_one_to_one(
        self,
        combined: pd.DataFrame,
        metrics: Metrics | None = None,
        name: str = "one_to_one_plot",
        xlabel: str = "Observed",
        ylabel: str = "Model",
        dpi: int = 600,
        show: bool = False,
    ) -> Path:
        """Scatter of observed (x) vs model (y) with a 1:1 reference line."""
        out = self._path(name, "png")
        obs = combined["observed"].to_numpy(dtype=float)
        mod = combined["model"].to_numpy(dtype=float)

        fig, ax = plt.subplots(figsize=(6, 6))
        ax.scatter(obs, mod, s=10, alpha=0.6, label="Paired samples")

        lo = float(np.nanmin([obs.min(), mod.min()]))
        hi = float(np.nanmax([obs.max(), mod.max()]))
        pad = 0.05 * (hi - lo if hi > lo else 1.0)
        lim = (lo - pad, hi + pad)
        ax.plot(lim, lim, "k--", linewidth=1, label="1:1 line")
        ax.set_xlim(lim)
        ax.set_ylim(lim)
        ax.set_aspect("equal", adjustable="box")

        ax.set_xlabel(xlabel, fontweight = 'bold')
        ax.set_ylabel(ylabel, fontweight = 'bold')
        ax.set_title(f"1:1 plot ({self.start.date()} -> {self.end.date()})")

        if metrics is not None:
            txt = (f"n = {metrics.n}\n"
                   f"RMSE = {metrics.rmse:.3f}\n"
                   f"R² = {metrics.r2:.3f}\n"
                   f"NSE = {metrics.nse:.3f}\n"
                   f"Bias = {metrics.bias:+.3f}")
            ax.text(0.04, 0.96, txt, transform=ax.transAxes,
                    va="top", ha="left",
                    bbox=dict(boxstyle="round", facecolor="white", alpha=0.8))

        ax.legend(loc="lower right")
        fig.tight_layout()
        fig.savefig(out, dpi=dpi)
        if show:
            plt.show()
        plt.close(fig)
        print(f"Saved 1:1 plot: {out}")
        return out

    def save_all(
        self,
        combined: pd.DataFrame,
        metrics: Metrics,
        show: bool = False,
        ylabel: str = "Stage (ft)",
    ) -> dict:
        return {
            "csv": self.save_csv(combined),
            "metrics": self.save_metrics(metrics),
            "plot": self.save_plot(combined, ylabel=ylabel, show=show),
            "one_to_one": self.save_one_to_one(combined, metrics=metrics, show=show),
        }
