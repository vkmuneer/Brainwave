"""Server-side chart rendering for exam analysis reports.

Charts are rendered to a base64 PNG so the same <img> tag works both for the
on-screen analysis page and the downloadable PDF (xhtml2pdf renders base64
data-URI images fine, unlike live JS charting libraries). That also rules out
hover tooltips, so every chart here direct-labels its values instead.

Colours are the validated categorical slots, assigned in fixed order - never
cycled - so a subject keeps its colour as series come and go.
"""
import base64
from io import BytesIO

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]

SURFACE = "#ffffff"
INK = "#1c1c1c"
INK_MUTED = "#6b7280"
GRID = "#e8e8e6"

GOOD = "#1baf7a"
CRITICAL = "#d64545"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "text.color": INK,
        "axes.labelcolor": INK_MUTED,
        "xtick.color": INK_MUTED,
        "ytick.color": INK_MUTED,
        "axes.edgecolor": GRID,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
    }
)


def _fig_to_data_uri(fig):
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor=SURFACE)
    plt.close(fig)
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _style_axes(ax, ylabel=None):
    """Recessive chrome: horizontal grid only, no box around the plot."""
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8)


def _pass_mark_line(ax, value=35):
    """The 35% pass mark, labelled inside the plot.

    Positioned in axes fractions rather than data units - at data x it lands
    beyond the last point and stretches the figure with empty space.
    """
    ax.axhline(value, color=CRITICAL, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
    # Labelled in the left margin beside the y ticks: inside the plot it
    # collides with whichever mark happens to sit near the pass mark.
    ax.annotate(
        f"{value:g}",
        xy=(0, value),
        xycoords=ax.get_yaxis_transform(),
        xytext=(-6, 0),
        textcoords="offset points",
        ha="right",
        va="center",
        fontsize=8,
        fontweight="bold",
        color=CRITICAL,
    )


def _title(ax, text):
    ax.set_title(text, color=INK, fontweight="bold", fontsize=11, pad=12, loc="left")


def bar_chart(labels, values, title, ylabel="Average (%)"):
    """Subject averages - one series, so no legend; the title names it."""
    fig, ax = plt.subplots(figsize=(7, 3.6))
    bars = ax.bar(labels, values, color=SERIES[0], width=0.62, zorder=3)
    # 4px rounded data-ends would need a patch path; a flat cap plus a 2px
    # surface gap between bars is the closest matplotlib equivalent.
    for bar in bars:
        bar.set_linewidth(1.5)
        bar.set_edgecolor(SURFACE)

    _style_axes(ax, ylabel)
    _title(ax, title)
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_locator(MultipleLocator(25))

    for bar, value in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + 2,
            f"{value:.0f}",
            ha="center",
            va="bottom",
            fontsize=8.5,
            color=INK,
            fontweight="bold",
        )

    if len(max(labels, key=len)) > 8:
        plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def meter_chart(pass_count, fail_count, title="Pass rate"):
    """A single proportion against its limit - a meter, not a two-slice pie."""
    total = pass_count + fail_count
    pct = (pass_count / total * 100) if total else 0

    fig, ax = plt.subplots(figsize=(7, 1.5))
    ax.barh([0], [100], color=GRID, height=0.5, zorder=2)
    ax.barh([0], [pct], color=GOOD if pct >= 60 else CRITICAL, height=0.5, zorder=3)

    ax.set_xlim(0, 100)
    ax.set_ylim(-0.6, 0.9)
    ax.axis("off")
    ax.text(0, 0.62, title, fontsize=11, fontweight="bold", color=INK)
    ax.text(
        100,
        0.62,
        f"{pct:.0f}%  ({pass_count} passed, {fail_count} failed)",
        fontsize=9,
        color=INK_MUTED,
        ha="right",
    )
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def trend_chart(labels, values, title, ylabel="Percentage (%)"):
    """One student's percentage across exams - change over time, so a line."""
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.plot(
        labels,
        values,
        color=SERIES[0],
        linewidth=2,
        marker="o",
        markersize=8,
        markerfacecolor=SERIES[0],
        markeredgecolor=SURFACE,
        markeredgewidth=2,
        zorder=3,
    )

    _style_axes(ax, ylabel)
    _title(ax, title)
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_locator(MultipleLocator(25))
    _pass_mark_line(ax)

    for i, value in enumerate(values):
        ax.annotate(
            f"{value:.0f}%",
            (i, value),
            textcoords="offset points",
            xytext=(0, 11),
            ha="center",
            fontsize=8.5,
            fontweight="bold",
            color=INK,
        )
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def grouped_bar_chart(categories, series, title, ylabel="Marks (%)"):
    """Subject marks across two or more exams.

    `series` is a list of (name, values) in exam order; slot colours follow that
    order so the same exam keeps its colour between charts.
    """
    fig, ax = plt.subplots(figsize=(7, 3.8))
    count = len(series)
    width = min(0.72 / count, 0.3)
    positions = range(len(categories))

    for index, (name, values) in enumerate(series):
        offset = (index - (count - 1) / 2) * width
        bars = ax.bar(
            [p + offset for p in positions],
            values,
            width=width,
            label=name,
            color=SERIES[index % len(SERIES)],
            zorder=3,
        )
        for bar in bars:
            bar.set_linewidth(1.5)
            bar.set_edgecolor(SURFACE)

    _style_axes(ax, ylabel)
    _title(ax, title)
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_locator(MultipleLocator(25))
    ax.set_xticks(list(positions))
    ax.set_xticklabels(categories)
    if len(max(categories, key=len)) > 8:
        plt.setp(ax.get_xticklabels(), rotation=20, ha="right")

    _pass_mark_line(ax)
    # Two or more series always carry a legend - identity is never colour alone.
    ax.legend(frameon=False, fontsize=8.5, ncols=min(count, 4), loc="upper center",
              bbox_to_anchor=(0.5, -0.18))
    fig.tight_layout()
    return _fig_to_data_uri(fig)
