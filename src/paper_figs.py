"""One publication style for every HECinBOX chart, on screen and on disk.

Each chart in Tab 5 (Results) and Tab 6 (Validation) is built once as a
Plotly figure with a FIXED physical size.  The app shows that figure at
exactly that size, and the PNG written to the run folder is the same
figure rendered at 400 dpi, so what you see is what you save.

Size convention: a time-series panel is laid out at 1000 x 420 px on
screen and prints 7.5 in wide, so its PNG is rendered at scale
7.5 * 400 / 1000 = 3, i.e. 3000 x 1260 px, tagged as 400 dpi so Word or
LaTeX place it at 7.5 in wide without rescaling.

Style: no in-figure title (the caption carries it), black box axes with
outward ticks, a fine dotted grid, the legend boxed inside the plot, Arial-metric font at 9-10 pt, and the same
two colours the manuscript figures use (model blue, observed orange).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import numpy as np

EXPORT_DPI = 400

# Physical panel sizes, inches (what Word/LaTeX place on the page).
TS_SIZE_IN = (7.5, 3.15)     # time series: full text width
SQUARE_SIZE_IN = (4.2, 4.2)  # 1:1 scatter

# On-screen layout size in pixels.  Large enough to read comfortably in
# the app; the PNG is rendered at scale = inches * 400 / pixels, so it is
# exactly 400 dpi at the physical size above (3000 px for 7.5 in).  Font
# sizes below are in these layout pixels: 17 px on a 1000 px panel that
# prints 7.5 in wide is about 9 pt on paper.
TS_PX = (1000, 420)
SQUARE_PX = (560, 560)

FONT = "Arial, Liberation Sans, Helvetica, sans-serif"
INK = "#000000"
GRID = "#d4d4d4"
FS_TICK, FS_TITLE, FS_LEGEND = 17, 19, 16
LINE_W = 2.2

MODEL_COLOR = "#1f77b4"
OBS_COLOR = "#ff7f0e"
ALT_COLOR = "#7f7f7f"
# Extra series (e.g. several runs): Okabe-Ito, colour-blind safe.
SERIES_COLORS = [
    "#0072B2", "#D55E00", "#009E73", "#CC79A7",
    "#E69F00", "#56B4E9", "#000000", "#F0E442",
]


def px(inches: float) -> int:
    """Layout pixels for a height in inches, at the time-series scale."""
    return int(round(inches * TS_PX[0] / TS_SIZE_IN[0]))


def size_px(square: bool = False) -> tuple[int, int]:
    return SQUARE_PX if square else TS_PX


def export_scale(width_px: int, square: bool = False) -> float:
    """Kaleido scale that gives 400 dpi at the panel's physical width."""
    inches = SQUARE_SIZE_IN[0] if square else TS_SIZE_IN[0]
    return inches * EXPORT_DPI / max(int(width_px), 1)


def style(fig, ytitle: str, xtitle: str = "Time", square: bool = False):
    """Apply the paper style and the fixed panel size to ``fig``."""
    w, h = size_px(square)
    axis = dict(
        showline=True, linewidth=1.3, linecolor=INK, mirror=True,
        ticks="outside", ticklen=6, tickwidth=1.3, tickcolor=INK,
        # Fine dotted grid: visible for reading values, quiet in print.
        showgrid=True, gridcolor=GRID, gridwidth=1, griddash="dot",
        zeroline=False,
        title_font=dict(family=FONT, size=FS_TITLE, color=INK),
        tickfont=dict(family=FONT, size=FS_TICK, color=INK),
        title_standoff=10,
        automargin=True,
    )
    fig.update_xaxes(title_text=xtitle, **axis)
    fig.update_yaxes(title_text=ytitle, **axis)
    fig.update_layout(
        template="plotly_white",
        width=w, height=h, autosize=False,
        # Top margin keeps the hover toolbar off the plot frame.
        margin=dict(l=84, r=22, t=34, b=66),
        font=dict(family=FONT, size=FS_TICK, color=INK),
        paper_bgcolor="white", plot_bgcolor="white",
        title=None,
        hovermode="closest" if square else "x unified",
        showlegend=True,
        modebar=dict(bgcolor="rgba(0,0,0,0)", color="#8a8a8a",
                     activecolor=INK),
        # Legend boxed inside the plot area, top-left.
        legend=dict(
            orientation="v", x=0.012, xanchor="left",
            y=0.975, yanchor="top",
            bgcolor="rgba(255,255,255,0.92)",
            bordercolor=INK, borderwidth=1,
            font=dict(family=FONT, size=FS_LEGEND, color=INK),
            itemwidth=36, tracegroupgap=2,
        ),
    )
    # Uniform, print-weight lines (markers keep their own size).
    for tr in fig.data:
        if getattr(tr, "mode", None) and "lines" in tr.mode:
            if tr.line.width is None or tr.line.width < LINE_W:
                tr.line.width = LINE_W
    if square:
        fig.update_yaxes(scaleanchor="x", scaleratio=1)
        # Legend stays top-left: in a 1:1 plot that corner (low observed,
        # high model) is almost always empty, while the lower right holds
        # the under-predicted peaks.
    return fig


def plotly_config(filename: str, square: bool = False,
                  extra: Optional[dict] = None) -> dict:
    """Streamlit/Plotly config: the camera button downloads this exact
    figure at 400 dpi pixel size."""
    w, h = size_px(square)
    cfg = {
        "displaylogo": False,
        "toImageButtonOptions": {
            "format": "png", "filename": filename,
            "width": w, "height": h, "scale": export_scale(w, square),
        },
    }
    if extra:
        cfg.update(extra)
    return cfg


def save_png(fig, path: Path | str) -> Optional[Path]:
    """Write ``fig`` as a 400-dpi PNG (pixel size AND dpi tag).

    Needs ``kaleido``.  Returns None (and says why) when it cannot
    render, so a missing renderer never breaks a run.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    w = int(fig.layout.width or size_px()[0])
    h = int(fig.layout.height or size_px()[1])
    try:
        fig.write_image(str(path), format="png", width=w, height=h,
                        scale=export_scale(w, square=(w == h)))
    except Exception as e:  # kaleido missing / renderer failure
        print(f"paper_figs: could not write {path.name} ({e}).")
        return None
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
            im.save(path, dpi=(EXPORT_DPI, EXPORT_DPI))
    except Exception as e:
        print(f"paper_figs: {path.name} saved without a dpi tag ({e}).")
    return path


# ── Figure builders shared by the pipeline and the app ───────────────
def _go():
    import plotly.graph_objects as go
    return go


def model_vs_observed(x, model, observed, ytitle: str, xtitle: str,
                      unit: str = ""):
    """Model and observed series on one time axis."""
    go = _go()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=model, mode="lines", name="Model",
        line=dict(color=MODEL_COLOR, width=LINE_W),
        hovertemplate="%{x|%Y-%m-%d %H:%M}<br>Model: %{y:.2f} "
                      f"{unit}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=observed, mode="lines", name="Observed",
        line=dict(color=OBS_COLOR, width=LINE_W),
        hovertemplate="%{x|%Y-%m-%d %H:%M}<br>Observed: %{y:.2f} "
                      f"{unit}<extra></extra>",
    ))
    return style(fig, ytitle, xtitle)


def one_to_one(observed, model, unit: str = ""):
    """Observed (x) vs model (y) with a 1:1 line, equal axes."""
    go = _go()
    o = np.asarray(observed, float)
    m = np.asarray(model, float)
    lo = float(np.nanmin([np.nanmin(o), np.nanmin(m)]))
    hi = float(np.nanmax([np.nanmax(o), np.nanmax(m)]))
    pad = 0.05 * (hi - lo) if hi > lo else 0.5
    rng = [lo - pad, hi + pad]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=o, y=m, mode="markers", name="Paired samples",
        marker=dict(size=7, color=MODEL_COLOR, opacity=0.55,
                    line=dict(width=0)),
        hovertemplate="Observed: %{x:.2f}<br>Model: %{y:.2f}"
                      "<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=rng, y=rng, mode="lines", name="1:1 line",
        line=dict(color=INK, dash="dash", width=1.5), hoverinfo="skip",
    ))
    u = f" ({unit})" if unit else ""
    style(fig, f"Model{u}", f"Observed{u}", square=True)
    step = _nice_step(rng[1] - rng[0])
    fig.update_xaxes(range=rng, dtick=step)
    fig.update_yaxes(range=rng, dtick=step)
    return fig


def _nice_step(span: float, target: int = 6) -> float:
    """A 1-2-2.5-5 tick step giving about ``target`` ticks."""
    if not np.isfinite(span) or span <= 0:
        return 1.0
    raw = span / target
    mag = 10 ** np.floor(np.log10(raw))
    for k in (1, 2, 2.5, 5, 10):
        if raw <= k * mag:
            return float(k * mag)
    return float(10 * mag)


def series(traces: Sequence[dict], ytitle: str, xtitle: str,
           unit: str = ""):
    """Generic multi-line time series.  Each trace dict: x, y, name and
    optional color / dash / width."""
    go = _go()
    fig = go.Figure()
    for i, t in enumerate(traces):
        fig.add_trace(go.Scatter(
            x=t["x"], y=t["y"], mode="lines", name=t.get("name", ""),
            connectgaps=False,
            line=dict(
                color=t.get("color") or SERIES_COLORS[i % len(
                    SERIES_COLORS)],
                width=t.get("width", 1.5), dash=t.get("dash", "solid"),
            ),
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{y:.3f} "
                          f"{unit}<extra>{t.get('name', '')}</extra>",
        ))
    return style(fig, ytitle, xtitle)
