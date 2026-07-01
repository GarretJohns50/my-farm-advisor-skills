#!/usr/bin/env python3
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""Field-level EDA for boundaries, CDL, and weather across fields and growers.

Generates static PNGs and CSVs per farm, plus optional cross-grower comparisons.
No markdown report — outputs are meant for later assembly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from paths import (  # noqa: E402
    DATA_ROOT,
    farm_boundary_path,
    farm_cdl_full_composition_path,
    farm_weather_path,
)

_GROWERS = [
    ("central-ia-grower", "central-ia-grower-iowa", "Iowa (Kossuth)"),
    ("central-il-grower", "central-il-grower-illinois", "Illinois (Iroquois)"),
    ("central-ne-grower", "central-ne-grower-nebraska", "Nebraska (Hamilton)"),
]


def _circularity(geom, crs) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    temp = gpd.GeoDataFrame([{"geometry": geom}], crs=crs).to_crs("EPSG:5070")
    area = temp.geometry.area.iloc[0]
    perimeter = temp.geometry.length.iloc[0]
    if perimeter == 0:
        return 0.0
    return (4.0 * np.pi * area) / (perimeter ** 2)


def load_data(grower_slug: str, farm_slug: str) -> dict:
    data: dict = {"grower_slug": grower_slug, "farm_slug": farm_slug}

    # Boundaries
    bp = farm_boundary_path(grower_slug, farm_slug)
    if bp.exists():
        gdf = gpd.read_file(bp)
        gdf["circularity"] = gdf.geometry.apply(lambda g: _circularity(g, gdf.crs))
        data["fields"] = gdf
    else:
        data["fields"] = gpd.GeoDataFrame()

    # Weather
    wp = farm_weather_path(grower_slug, farm_slug)
    if wp.exists():
        w = pd.read_csv(wp, parse_dates=["date"])
        w["year"] = w["date"].dt.year
        w["month"] = w["date"].dt.month
        w["GDD"] = w["T2M"].apply(lambda t: max(0, t - 10))
        data["weather"] = w
    else:
        data["weather"] = pd.DataFrame()

    # CDL
    cp = farm_cdl_full_composition_path(grower_slug, farm_slug)
    if cp.exists():
        data["cdl"] = pd.read_csv(cp)
    else:
        data["cdl"] = pd.DataFrame()

    return data


def _ensure_out(data: dict) -> Path:
    slug = data["farm_slug"]
    out = (
        DATA_ROOT
        / "growers"
        / data["grower_slug"]
        / "farms"
        / slug
        / "derived"
        / "eda_field_level"
    )
    out.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------------------
# Category A: Field Boundaries
# ---------------------------------------------------------------------------
def analyze_boundaries(data: dict, out: Path) -> None:
    print("\n--- Category A: Boundaries ---")
    gdf = data["fields"]
    if gdf.empty:
        print("  No boundary data")
        return

    # A1: Size distribution
    fig, ax = plt.subplots(figsize=(10, 5))
    bp = ax.boxplot(
        gdf["area_acres"].dropna(), tick_labels=["Fields"], patch_artist=True,
        widths=0.4
    )
    bp["boxes"][0].set_facecolor("#4CAF50")
    bp["boxes"][0].set_alpha(0.6)
    # Overlay swarm
    y = gdf["area_acres"].dropna().values
    x = np.random.normal(1, 0.04, size=len(y))
    ax.scatter(x, y, alpha=0.6, color="#1B5E20", s=30, zorder=3)
    ax.set_ylabel("Field Area (acres)")
    ax.set_title("A1: Field Size Distribution", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")
    plt.tight_layout()
    plt.savefig(out / "A1_boundary_size_distribution.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {out / 'A1_boundary_size_distribution.png'}")

    # A2: Shape scatter (area vs circularity, colored by crop if available)
    fig, ax = plt.subplots(figsize=(10, 6))
    sc = ax.scatter(
        gdf["area_acres"],
        gdf["circularity"],
        c=gdf["circularity"],
        cmap="viridis",
        alpha=0.7,
        s=80,
        edgecolors="black",
        linewidth=0.5,
    )
    ax.axhline(y=0.70, color="red", linestyle="--", alpha=0.5, label="Pivot threshold")
    ax.set_xlabel("Field Area (acres)")
    ax.set_ylabel("Circularity (4πA/P²)")
    ax.set_title("A2: Field Size vs. Shape Circularity", fontweight="bold")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.colorbar(sc, ax=ax, label="Circularity")
    plt.tight_layout()
    plt.savefig(out / "A2_boundary_shape_scatter.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {out / 'A2_boundary_shape_scatter.png'}")

    # A3: Correlation CSV
    from scipy import stats
    r, p = stats.pearsonr(gdf["area_acres"], gdf["circularity"])
    corr = pd.DataFrame([{
        "metric": "area_vs_circularity",
        "pearson_r": round(r, 4),
        "p_value": round(p, 4),
        "n_fields": len(gdf),
        "significant_05": p < 0.05,
    }])
    corr.to_csv(out / "A3_boundary_correlation.csv", index=False)
    print(f"  ✓ {out / 'A3_boundary_correlation.csv'} (r={r:.3f}, p={p:.4f})")


# ---------------------------------------------------------------------------
# Category B: CDL / Cropland
# ---------------------------------------------------------------------------
def analyze_cdl(data: dict, out: Path) -> None:
    print("\n--- Category B: CDL ---")
    cdl = data["cdl"]
    if cdl.empty:
        print("  No CDL data")
        return

    # Dominant crop per field-year
    dom = (
        cdl.loc[cdl.groupby(["field_id", "year"])["pct"].idxmax()]
        .reset_index(drop=True)[["field_id", "year", "crop_name", "pct"]]
    )

    # B1: Dominant crop heatmap
    pivot = dom.pivot(index="field_id", columns="year", values="crop_name")
    # Encode crops as numeric for colormap
    all_crops = sorted(dom["crop_name"].unique())
    crop_to_num = {c: i for i, c in enumerate(all_crops)}
    num_mat = pivot.replace(crop_to_num).astype(float)

    fig, ax = plt.subplots(figsize=(10, max(4, len(pivot) * 0.4)))
    cmap = plt.get_cmap("tab10", len(all_crops))
    im = ax.imshow(num_mat.values, cmap=cmap, aspect="auto", vmin=-0.5, vmax=len(all_crops) - 0.5)
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=8)
    ax.set_xlabel("Year")
    ax.set_ylabel("Field ID")
    ax.set_title("B1: Dominant Crop by Field and Year", fontweight="bold")
    # Custom colorbar with crop labels
    cbar = plt.colorbar(im, ax=ax, ticks=range(len(all_crops)))
    cbar.ax.set_yticklabels(all_crops, fontsize=8)
    plt.tight_layout()
    plt.savefig(out / "B1_cdl_dominant_heatmap.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {out / 'B1_cdl_dominant_heatmap.png'}")

    # B2: Shannon diversity index per field
    def _shannon(field_df: pd.DataFrame) -> float:
        # Sum pct per crop across all years for this field
        summed = field_df.groupby("crop_name")["pct"].sum()
        total = summed.sum()
        if total == 0:
            return 0.0
        probs = summed / total
        return -sum(p * np.log(p) for p in probs if p > 0)

    diversity = (
        cdl.groupby("field_id")
        .apply(_shannon, include_groups=False)
        .reset_index(name="shannon_index")
    )
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(diversity["field_id"], diversity["shannon_index"], color="#795548")
    ax.set_xlabel("Shannon Diversity Index")
    ax.set_ylabel("Field ID")
    ax.set_title("B2: Crop Diversity by Field (2021–2025)", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="x")
    plt.tight_layout()
    plt.savefig(out / "B2_cdl_diversity_index.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {out / 'B2_cdl_diversity_index.png'}")

    # B3: Rotation transition matrix
    transitions = []
    for fid in dom["field_id"].unique():
        sub = dom[dom["field_id"] == fid].sort_values("year")
        for i in range(len(sub) - 1):
            transitions.append({
                "from": sub.iloc[i]["crop_name"],
                "to": sub.iloc[i + 1]["crop_name"],
            })
    if transitions:
        tdf = pd.DataFrame(transitions)
        mat = pd.crosstab(tdf["from"], tdf["to"], normalize="index").round(3)
        mat.to_csv(out / "B3_cdl_rotation_matrix.csv")
        print(f"  ✓ {out / 'B3_cdl_rotation_matrix.csv'}")
    else:
        print("  ! Not enough years for rotation matrix")


# ---------------------------------------------------------------------------
# Category C: Weather
# ---------------------------------------------------------------------------
def analyze_weather(data: dict, out: Path) -> None:
    print("\n--- Category C: Weather ---")
    w = data["weather"]
    if w.empty:
        print("  No weather data")
        return

    # Per-field annual growing-season stats (Apr-Sep)
    gs = w[(w["month"] >= 4) & (w["month"] <= 9)]
    gs_pf = gs.groupby(["year", "field_id"]).agg({
        "GDD": "sum",
        "PRECTOTCORR": "sum",
    }).reset_index()
    gs_pf.rename(columns={"PRECTOTCORR": "gs_precip", "GDD": "gs_gdd"}, inplace=True)

    # C1: Temporal trends — per-field GDD by year (line plot)
    fig, ax = plt.subplots(figsize=(12, 6))
    years = sorted(gs_pf["year"].unique())
    colors = plt.cm.tab10(np.linspace(0, 1, gs_pf["field_id"].nunique()))
    for i, fid in enumerate(gs_pf["field_id"].unique()):
        sub = gs_pf[gs_pf["field_id"] == fid].sort_values("year")
        ax.plot(sub["year"], sub["gs_gdd"], marker="o", label=fid, color=colors[i], alpha=0.7)
    ax.set_xlabel("Year")
    ax.set_ylabel("Growing-Season GDD (°C·day)")
    ax.set_title("C1: Per-Field Growing-Season GDD Trends", fontweight="bold")
    ax.set_xticks(years)
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out / "C1_weather_temporal_trends.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {out / 'C1_weather_temporal_trends.png'}")

    # C2: Inter-field spread — box plot of per-field GDD by year
    fig, ax = plt.subplots(figsize=(12, 6))
    year_groups = [gs_pf[gs_pf["year"] == y]["gs_gdd"].values for y in years]
    bp = ax.boxplot(year_groups, tick_labels=years, patch_artist=True)
    for patch in bp["boxes"]:
        patch.set_facecolor("#2196F3")
        patch.set_alpha(0.6)
    ax.set_xlabel("Year")
    ax.set_ylabel("Growing-Season GDD (°C·day)")
    ax.set_title("C2: Inter-Field GDD Variability by Year", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")
    plt.tight_layout()
    plt.savefig(out / "C2_weather_interfield_spread.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  ✓ {out / 'C2_weather_interfield_spread.png'}")

    # C3: CV analysis — coefficient of variation across fields, per year
    cv = (
        gs_pf.groupby("year")
        .agg({
            "gs_gdd": lambda s: s.std() / s.mean() if s.mean() != 0 else 0,
            "gs_precip": lambda s: s.std() / s.mean() if s.mean() != 0 else 0,
        })
        .reset_index()
    )
    cv.rename(columns={"gs_gdd": "cv_gdd", "gs_precip": "cv_precip"}, inplace=True)
    cv.to_csv(out / "C3_weather_cv_analysis.csv", index=False)
    print(f"  ✓ {out / 'C3_weather_cv_analysis.csv'}")


# ---------------------------------------------------------------------------
# Cross-Grower (X)
# ---------------------------------------------------------------------------
def analyze_cross_grower(primary_data: dict, out: Path) -> None:
    print("\n--- Cross-Grower Analysis ---")
    all_data = []
    for g, f, label in _GROWERS:
        d = load_data(g, f)
        d["label"] = label
        all_data.append(d)

    # X1: Boundary sizes across growers
    fig, ax = plt.subplots(figsize=(10, 6))
    sizes = []
    labels = []
    for d in all_data:
        gdf = d["fields"]
        if not gdf.empty:
            sizes.append(gdf["area_acres"].tolist())
            labels.append(d["label"])
    if sizes:
        bp = ax.boxplot(sizes, tick_labels=labels, patch_artist=True)
        colors = ["#2E7D32", "#1565C0", "#E65100"]
        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.6)
        ax.set_ylabel("Field Area (acres)")
        ax.set_title("X1: Field Size Distribution Across Growers", fontweight="bold")
        ax.grid(True, alpha=0.3, axis="y")
        plt.tight_layout()
        plt.savefig(out / "X1_cross_boundary_sizes.png", dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  ✓ {out / 'X1_cross_boundary_sizes.png'}")

    # X2: CDL mix across growers
    fig, ax = plt.subplots(figsize=(10, 6))
    mix_rows = []
    for d in all_data:
        cdl = d["cdl"]
        if cdl.empty:
            continue
        dom = cdl.loc[cdl.groupby(["field_id", "year"])["pct"].idxmax()]
        counts = dom["crop_name"].value_counts().to_dict()
        total = sum(counts.values())
        for crop, n in counts.items():
            mix_rows.append({
                "grower": d["label"],
                "crop": crop,
                "pct": round(100 * n / total, 1),
            })
    if mix_rows:
        mdf = pd.DataFrame(mix_rows)
        pivot2 = mdf.pivot(index="crop", columns="grower", values="pct").fillna(0)
        pivot2.plot(kind="bar", ax=ax, color=["#2E7D32", "#1565C0", "#E65100"], alpha=0.8)
        ax.set_ylabel("% of Field-Years")
        ax.set_title("X2: Crop Mix Across Growers", fontweight="bold")
        ax.legend(title="Grower", loc="upper right")
        ax.grid(True, alpha=0.3, axis="y")
        plt.tight_layout()
        plt.savefig(out / "X2_cross_cdl_mix.png", dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  ✓ {out / 'X2_cross_cdl_mix.png'}")

    # X3: Weather CV across growers
    cv_rows = []
    for d in all_data:
        w = d["weather"]
        if w.empty:
            continue
        gs = w[(w["month"] >= 4) & (w["month"] <= 9)]
        gs_pf = gs.groupby(["year", "field_id"]).agg({"GDD": "sum"}).reset_index()
        yearly_cv = gs_pf.groupby("year").apply(
            lambda s: s["GDD"].std() / s["GDD"].mean() if s["GDD"].mean() != 0 else 0,
            include_groups=False,
        ).reset_index(name="cv_gdd")
        for _, row in yearly_cv.iterrows():
            cv_rows.append({
                "grower": d["label"],
                "year": int(row["year"]),
                "cv_gdd": round(row["cv_gdd"], 3),
            })
    if cv_rows:
        cvdf = pd.DataFrame(cv_rows)
        mean_cv = cvdf.groupby("grower")["cv_gdd"].mean().reset_index()
        fig, ax = plt.subplots(figsize=(8, 5))
        colors = ["#2E7D32", "#1565C0", "#E65100"]
        bars = ax.bar(mean_cv["grower"], mean_cv["cv_gdd"], color=colors, alpha=0.8)
        ax.set_ylabel("Mean Inter-Field CV of GDD")
        ax.set_title("X3: Weather Uniformity Across Growers\n(Lower = More Uniform)", fontweight="bold")
        ax.grid(True, alpha=0.3, axis="y")
        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width() / 2, height + 0.005,
                    f"{height:.3f}", ha="center", va="bottom", fontsize=10)
        plt.tight_layout()
        plt.savefig(out / "X3_cross_weather_cv.png", dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  ✓ {out / 'X3_cross_weather_cv.png'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grower-slug", required=True)
    parser.add_argument("--farm-slug", required=True)
    parser.add_argument("--cross-grower", action="store_true",
                        help="Include cross-grower comparison plots")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("=" * 60)
    print("Field-Level EDA")
    print("=" * 60)
    print(f"Grower: {args.grower_slug} | Farm: {args.farm_slug}")

    data = load_data(args.grower_slug, args.farm_slug)
    out = _ensure_out(data)
    print(f"Output: {out}")

    analyze_boundaries(data, out)
    analyze_cdl(data, out)
    analyze_weather(data, out)

    if args.cross_grower:
        analyze_cross_grower(data, out)

    print("\n" + "=" * 60)
    print("Field-level EDA complete")
    print(f"Output: {out}")


if __name__ == "__main__":
    main()
