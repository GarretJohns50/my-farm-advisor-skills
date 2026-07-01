#!/usr/bin/env python3
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""Cross-grower EDA comparison for field boundaries, weather, CDL, and soil.

Loads all three growers (IA, IL, NE), compares field sizes, weather patterns,
crop rotations, and soil properties across farms.
"""

from __future__ import annotations

import argparse
import json
import os
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
    farm_soil_sample_path,
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


def load_grower_data(grower_slug: str, farm_slug: str, label: str) -> dict:
    """Load all data for a single grower/farm."""
    data: dict = {"label": label, "grower_slug": grower_slug, "farm_slug": farm_slug}

    # Boundaries
    boundary_path = farm_boundary_path(grower_slug, farm_slug)
    if boundary_path.exists():
        gdf = gpd.read_file(boundary_path)
        gdf["circularity"] = gdf.geometry.apply(lambda g: _circularity(g, gdf.crs))
        data["fields"] = gdf
    else:
        data["fields"] = gpd.GeoDataFrame()

    # Weather
    weather_path = farm_weather_path(grower_slug, farm_slug)
    if weather_path.exists():
        w = pd.read_csv(weather_path, parse_dates=["date"])
        data["weather"] = w
    else:
        data["weather"] = pd.DataFrame()

    # CDL
    cdl_path = farm_cdl_full_composition_path(grower_slug, farm_slug)
    if cdl_path.exists():
        data["cdl"] = pd.read_csv(cdl_path)
    else:
        data["cdl"] = pd.DataFrame()

    # Soil
    soil_path = farm_soil_sample_path(grower_slug, farm_slug)
    if soil_path.exists():
        data["soil"] = pd.read_csv(soil_path)
    else:
        data["soil"] = pd.DataFrame()

    return data


def analyze_boundaries(data_list: list[dict], out_dir: Path) -> None:
    print("\n=== Field Boundary Analysis ===")
    rows = []
    for d in data_list:
        gdf = d["fields"]
        if gdf.empty:
            continue
        rows.append({
            "grower": d["label"],
            "field_count": len(gdf),
            "total_acres": gdf["area_acres"].sum(),
            "mean_acres": gdf["area_acres"].mean(),
            "median_acres": gdf["area_acres"].median(),
            "min_acres": gdf["area_acres"].min(),
            "max_acres": gdf["area_acres"].max(),
            "mean_circularity": gdf["circularity"].mean(),
        })
        print(f"  {d['label']}: {len(gdf)} fields, {gdf['area_acres'].sum():.1f} ac, circ={gdf['circularity'].mean():.3f}")

    summary = pd.DataFrame(rows)
    summary.to_csv(out_dir / "boundary_summary.csv", index=False)

    # Plot 1: Field size distribution
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    ax = axes[0]
    labels, sizes = [], []
    for d in data_list:
        gdf = d["fields"]
        if not gdf.empty:
            labels.append(d["label"])
            sizes.append(gdf["area_acres"].tolist())
    bp = ax.boxplot(sizes, tick_labels=labels, patch_artist=True)
    colors = ["#2E7D32", "#1565C0", "#E65100"]
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)
    ax.set_ylabel("Field Area (acres)")
    ax.set_title("Field Size Distribution by Grower", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")

    # Plot 2: Circularity distribution
    ax = axes[1]
    circ_data = []
    circ_labels = []
    for d in data_list:
        gdf = d["fields"]
        if not gdf.empty:
            circ_data.append(gdf["circularity"])
            circ_labels.append(d["label"])
    bp2 = ax.boxplot(circ_data, tick_labels=circ_labels, patch_artist=True)
    for patch, color in zip(bp2["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)
    ax.set_ylabel("Circularity (4πA/P²)")
    ax.set_title("Polygon Circularity by Grower", fontweight="bold")
    ax.axhline(y=0.75, color="red", linestyle="--", alpha=0.5, label="Pivot threshold")
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(out_dir / "field_size_distribution.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"✓ Saved: {out_dir / 'field_size_distribution.png'}")


def analyze_weather(data_list: list[dict], out_dir: Path) -> None:
    print("\n=== Weather Analysis ===")
    weather_rows = []
    gdd_rows = []

    for d in data_list:
        w = d["weather"]
        if w.empty:
            continue
        w["date"] = pd.to_datetime(w["date"])
        w["year"] = w["date"].dt.year
        w["month"] = w["date"].dt.month
        w["GDD"] = w["T2M"].apply(lambda t: max(0, t - 10))

        # Step 1: per-field annual stats (weather CSV has one row per field per day)
        per_field_annual = w.groupby(["year", "field_id"]).agg({
            "T2M": "mean",
            "PRECTOTCORR": "sum",
            "ALLSKY_SFC_SW_DWN": "mean",
            "GDD": "sum",
        }).reset_index()

        # Step 2: average across fields for the grower
        annual = per_field_annual.groupby("year").agg({
            "T2M": "mean",
            "PRECTOTCORR": "mean",
            "ALLSKY_SFC_SW_DWN": "mean",
            "GDD": "mean",
        }).reset_index()

        # Growing season (Apr-Sep) — same two-step fix
        gs = w[(w["month"] >= 4) & (w["month"] <= 9)]
        gs_per_field = gs.groupby(["year", "field_id"]).agg({
            "PRECTOTCORR": "sum",
            "GDD": "sum",
        }).reset_index()
        gs_annual = gs_per_field.groupby("year").agg({
            "PRECTOTCORR": "mean",
            "GDD": "mean",
        }).reset_index()

        for _, row in annual.iterrows():
            gs_row = gs_annual[gs_annual["year"] == row["year"]]
            gs_precip = gs_row["PRECTOTCORR"].iloc[0] if not gs_row.empty else 0
            gs_gdd = gs_row["GDD"].iloc[0] if not gs_row.empty else 0
            weather_rows.append({
                "grower": d["label"],
                "year": int(row["year"]),
                "mean_temp_c": round(row["T2M"], 1),
                "total_precip_mm": round(row["PRECTOTCORR"], 1),
                "growing_season_precip_mm": round(gs_precip, 1),
                "mean_solar_wm2": round(row["ALLSKY_SFC_SW_DWN"], 1),
                "annual_gdd": round(row["GDD"], 0),
                "growing_season_gdd": round(gs_gdd, 0),
            })

        # Per-field GDD
        field_gdd = gs.groupby("field_id")["GDD"].sum().reset_index()
        for _, row in field_gdd.iterrows():
            gdd_rows.append({
                "grower": d["label"],
                "field_id": row["field_id"],
                "growing_season_gdd": round(row["GDD"], 0),
            })

    weather_df = pd.DataFrame(weather_rows)
    weather_df.to_csv(out_dir / "weather_annual_summary.csv", index=False)
    print(f"  Records: {len(weather_df)} annual summaries")

    # Plot: weather comparison
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # Mean temp
    ax = axes[0, 0]
    for d in data_list:
        sub = weather_df[weather_df["grower"] == d["label"]]
        if not sub.empty:
            ax.plot(sub["year"], sub["mean_temp_c"], marker="o", label=d["label"], linewidth=2)
    ax.set_xlabel("Year")
    ax.set_ylabel("Mean Temperature (°C)")
    ax.set_title("Annual Mean Temperature", fontweight="bold")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # Growing season precip
    ax = axes[0, 1]
    for d in data_list:
        sub = weather_df[weather_df["grower"] == d["label"]]
        if not sub.empty:
            ax.plot(sub["year"], sub["growing_season_precip_mm"], marker="s", label=d["label"], linewidth=2)
    ax.set_xlabel("Year")
    ax.set_ylabel("Precipitation (mm)")
    ax.set_title("Growing Season Precip (Apr-Sep)", fontweight="bold")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # GDD
    ax = axes[1, 0]
    for d in data_list:
        sub = weather_df[weather_df["grower"] == d["label"]]
        if not sub.empty:
            ax.plot(sub["year"], sub["growing_season_gdd"], marker="^", label=d["label"], linewidth=2)
    ax.set_xlabel("Year")
    ax.set_ylabel("GDD (base 10°C)")
    ax.set_title("Growing Season GDD", fontweight="bold")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # Solar
    ax = axes[1, 1]
    for d in data_list:
        sub = weather_df[weather_df["grower"] == d["label"]]
        if not sub.empty:
            ax.plot(sub["year"], sub["mean_solar_wm2"], marker="d", label=d["label"], linewidth=2)
    ax.set_xlabel("Year")
    ax.set_ylabel("Solar Radiation (W/m²)")
    ax.set_title("Mean Solar Radiation", fontweight="bold")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.suptitle("Cross-Grower Weather Comparison (2021-2025)", fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.savefig(out_dir / "weather_comparison.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"✓ Saved: {out_dir / 'weather_comparison.png'}")

    # GDD per field
    gdd_df = pd.DataFrame(gdd_rows)
    if not gdd_df.empty:
        fig, ax = plt.subplots(figsize=(12, 6))
        for d in data_list:
            sub = gdd_df[gdd_df["grower"] == d["label"]]
            if not sub.empty:
                x = range(len(sub))
                ax.bar([i + 0.3 * data_list.index(d) for i in x], sub["growing_season_gdd"],
                       width=0.25, label=d["label"], alpha=0.8)
        ax.set_ylabel("Growing Season GDD")
        ax.set_title("GDD by Field (Apr-Sep, base 10°C)", fontweight="bold")
        ax.legend()
        ax.grid(True, alpha=0.3, axis="y")
        plt.tight_layout()
        plt.savefig(out_dir / "gdd_by_field.png", dpi=150, bbox_inches="tight")
        plt.close()
        print(f"✓ Saved: {out_dir / 'gdd_by_field.png'}")


def analyze_cdl(data_list: list[dict], out_dir: Path) -> None:
    print("\n=== CDL / Cropland Analysis ===")
    cdl_rows = []
    rotation_rows = []

    for d in data_list:
        cdl = d["cdl"]
        if cdl.empty:
            continue
        # Per-field per-year dominant crop
        for (field_id, year), group in cdl.groupby(["field_id", "year"]):
            top = group.loc[group["pct"].idxmax()]
            cdl_rows.append({
                "grower": d["label"],
                "field_id": field_id,
                "year": int(year),
                "dominant_crop": top["crop_name"],
                "dominant_pct": round(top["pct"], 1),
            })

    cdl_df = pd.DataFrame(cdl_rows)
    if cdl_df.empty:
        print("  No CDL data")
        return

    cdl_df.to_csv(out_dir / "cdl_dominant_by_field_year.csv", index=False)

    # Crop mix by grower and year
    crop_mix = cdl_df.groupby(["grower", "year", "dominant_crop"]).size().reset_index(name="count")
    crop_mix.to_csv(out_dir / "crop_mix_summary.csv", index=False)

    # Plot: stacked bar chart
    fig, ax = plt.subplots(figsize=(12, 6))
    pivot = cdl_df.groupby(["grower", "year", "dominant_crop"]).size().unstack(fill_value=0)
    pivot.plot(kind="bar", stacked=True, ax=ax, colormap="tab10", edgecolor="black")
    ax.set_xlabel("Grower × Year")
    ax.set_ylabel("Field Count")
    ax.set_title("Crop Mix by Grower and Year", fontweight="bold")
    ax.legend(title="Crop", bbox_to_anchor=(1.02, 1), loc="upper left")
    plt.tight_layout()
    plt.savefig(out_dir / "crop_mix_by_year.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"✓ Saved: {out_dir / 'crop_mix_by_year.png'}")

    # Rotation consistency
    for d in data_list:
        sub = cdl_df[cdl_df["grower"] == d["label"]]
        if sub.empty:
            continue
        for field_id, group in sub.groupby("field_id"):
            crops = group["dominant_crop"].tolist()
            consistency = max(crops.count(c) for c in set(crops)) / len(crops) if crops else 0
            rotation_rows.append({
                "grower": d["label"],
                "field_id": field_id,
                "crop_sequence": " → ".join(crops),
                "consistency": round(consistency, 2),
                "unique_crops": len(set(crops)),
            })

    rotation_df = pd.DataFrame(rotation_rows)
    if not rotation_df.empty:
        rotation_df.to_csv(out_dir / "crop_rotation_summary.csv", index=False)
        print(f"  Rotation records: {len(rotation_df)}")


def analyze_soil(data_list: list[dict], out_dir: Path) -> None:
    print("\n=== Soil Analysis ===")
    soil_rows = []

    for d in data_list:
        soil = d["soil"]
        if soil.empty:
            continue
        # Get dominant component per field
        if {"field_id", "comppct_r"}.issubset(soil.columns):
            dom = soil.sort_values(["field_id", "comppct_r"], ascending=[True, False]).groupby("field_id").first().reset_index()
        else:
            dom = soil.groupby("field_id").first().reset_index()

        for _, row in dom.iterrows():
            soil_rows.append({
                "grower": d["label"],
                "field_id": row["field_id"],
                "muname": str(row.get("muname", "—")),
                "compname": str(row.get("compname", "—")),
                "om_pct": round(row.get("om_r", 0) or 0, 2),
                "ph": round(row.get("ph1to1h2o_r", 0) or 0, 2),
                "cec": round(row.get("cec7_r", 0) or 0, 2),
                "clay_pct": round(row.get("claytotal_r", 0) or 0, 2),
                "awc": round(row.get("awc_r", 0) or 0, 3),
                "drainage": str(row.get("drainagecl", "—")),
            })

    soil_df = pd.DataFrame(soil_rows)
    if soil_df.empty:
        print("  No soil data")
        return

    soil_df.to_csv(out_dir / "soil_dominant_summary.csv", index=False)

    # Plot: soil property comparison
    numeric_cols = ["om_pct", "ph", "cec", "clay_pct", "awc"]
    available = [c for c in numeric_cols if c in soil_df.columns and soil_df[c].notna().any()]

    if available:
        fig, axes = plt.subplots(1, len(available), figsize=(4 * len(available), 5))
        if len(available) == 1:
            axes = [axes]
        for ax, col in zip(axes, available):
            data = [soil_df[soil_df["grower"] == d["label"]][col].dropna().tolist() for d in data_list]
            labels = [d["label"] for d in data_list]
            bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
            colors = ["#2E7D32", "#1565C0", "#E65100"]
            for patch, color in zip(bp["boxes"], colors):
                patch.set_facecolor(color)
                patch.set_alpha(0.6)
            ax.set_title(col.upper(), fontweight="bold")
            ax.grid(True, alpha=0.3, axis="y")
        plt.suptitle("Soil Property Comparison by Grower", fontsize=14, fontweight="bold")
        plt.tight_layout()
        plt.savefig(out_dir / "soil_properties_comparison.png", dpi=150, bbox_inches="tight")
        plt.close()
        print(f"✓ Saved: {out_dir / 'soil_properties_comparison.png'}")


def write_report(data_list: list[dict], out_dir: Path) -> None:
    lines = [
        "# Cross-Grower EDA Report",
        "",
        "## Growers",
        "",
    ]
    for d in data_list:
        gdf = d["fields"]
        lines.append(f"- **{d['label']}** — {len(gdf)} fields, {gdf['area_acres'].sum():.1f} acres")
    lines.extend(["", "## Key Findings", ""])

    # Boundaries
    lines.append("### Field Boundaries")
    for d in data_list:
        gdf = d["fields"]
        if not gdf.empty:
            lines.append(f"- {d['label']}: mean={gdf['area_acres'].mean():.1f} ac, circ={gdf['circularity'].mean():.3f}")
    lines.append("")

    # Weather (read from saved CSV if available)
    weather_path = out_dir / "weather_annual_summary.csv"
    if weather_path.exists():
        w = pd.read_csv(weather_path)
        lines.append("### Weather (2021-2025)")
        for d in data_list:
            sub = w[w["grower"] == d["label"]]
            if not sub.empty:
                mean_gdd = sub["growing_season_gdd"].mean()
                mean_precip = sub["growing_season_precip_mm"].mean()
                lines.append(f"- {d['label']}: avg GDD={mean_gdd:.0f}, avg growing precip={mean_precip:.0f} mm")
        lines.append("")

    # CDL
    cdl_path = out_dir / "cdl_dominant_by_field_year.csv"
    if cdl_path.exists():
        cdl = pd.read_csv(cdl_path)
        lines.append("### CDL Cropland")
        for d in data_list:
            sub = cdl[cdl["grower"] == d["label"]]
            if not sub.empty:
                crops = sub["dominant_crop"].value_counts().to_dict()
                top_crop = max(crops, key=crops.get)
                lines.append(f"- {d['label']}: mostly {top_crop} ({crops[top_crop]} field-years)")
        lines.append("")

    report_path = out_dir / "eda_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ Saved: {report_path}")


def main() -> None:
    out_dir = DATA_ROOT / "eda" / "cross_grower"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Cross-Grower EDA Comparison")
    print("=" * 60)
    print(f"Output: {out_dir}")

    # Load all growers
    data_list = [load_grower_data(g, f, l) for g, f, l in _GROWERS]

    analyze_boundaries(data_list, out_dir)
    analyze_weather(data_list, out_dir)
    analyze_cdl(data_list, out_dir)
    analyze_soil(data_list, out_dir)
    write_report(data_list, out_dir)

    print("\n" + "=" * 60)
    print("Cross-grower EDA complete")
    print(f"Output: {out_dir}")


if __name__ == "__main__":
    main()
