"""Prepare compact chart-ready datasets and fix aging bucket completeness."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
CSV = PROJECT / "exports" / "powerbi"


def main():
    monthly = pd.read_csv(CSV / "fact_procurement_monthly.csv")
    monthly["SpendMonth"] = pd.to_datetime(monthly["SpendMonth"], errors="coerce")
    monthly_spend = (
        monthly.groupby(monthly["SpendMonth"].dt.strftime("%Y-%m"), dropna=True)
        .agg(PurchaseSpend=("PurchaseSpend", "sum"), PurchaseQuantity=("PurchaseQuantity", "sum"))
        .reset_index()
        .rename(columns={"SpendMonth": "YearMonth"})
        .sort_values("YearMonth")
    )
    monthly_spend.to_csv(CSV / "chart_monthly_spend.csv", index=False)

    sp = pd.read_csv(CSV / "fact_supplier_performance.csv")
    top = sp.nlargest(20, "TotalPurchaseDollars")[
        [
            "VendorNumber", "VendorName", "TotalPurchaseDollars", "TotalPurchaseQuantity",
            "PurchaseContributionPct", "MedianLeadTimeDays", "AverageLeadTimeDays",
            "LeadTimeStdDevDays", "InvoiceMatchRatePct", "QuantityAlignmentRatePct",
            "DollarAlignmentRatePct", "AbsoluteAvgPriceVariancePct",
            "EstimatedEndingInventoryCost", "PerformanceStatus", "PurchaseOrderKeyCount",
            "MatchedPOCount", "SupplierPerformanceScore",
        ]
    ]
    top.to_csv(CSV / "chart_top_suppliers.csv", index=False)

    brand = (
        monthly.groupby(["Brand", "Description"], dropna=False)["PurchaseSpend"]
        .sum()
        .nlargest(25)
        .reset_index()
        .rename(columns={"PurchaseSpend": "PurchaseSpend", "Description": "BrandName"})
    )
    brand.to_csv(CSV / "chart_brand_spend.csv", index=False)

    lt = pd.read_csv(CSV / "fact_lead_time.csv")
    hist = (
        pd.to_numeric(lt["ObservedLeadTimeDays"], errors="coerce")
        .dropna()
        .astype(int)
        .value_counts()
        .sort_index()
        .rename_axis("LeadTimeDays")
        .reset_index(name="POCount")
    )
    hist.to_csv(CSV / "chart_lead_time_hist.csv", index=False)

    lt["PODate"] = pd.to_datetime(lt["PODate"], errors="coerce")
    lt_trend = (
        lt.dropna(subset=["PODate"])
        .assign(YearMonth=lambda d: d["PODate"].dt.strftime("%Y-%m"))
        .groupby("YearMonth")["ObservedLeadTimeDays"]
        .agg(AvgLeadTimeDays="mean", MedianLeadTimeDays="median", POCount="count")
        .reset_index()
        .sort_values("YearMonth")
    )
    lt_trend.to_csv(CSV / "chart_lead_time_trend.csv", index=False)

    buckets = ["0-7 days", "8-15 days", "16-30 days", "31-60 days", "60+ days"]
    aging = pd.read_csv(CSV / "po_cycle_aging_summary.csv")
    aging_map = {r.POCycleAgingBucket: r for r in aging.itertuples()}
    rows = []
    for b in buckets:
        r = aging_map.get(b)
        rows.append({
            "POCycleAgingBucket": b,
            "POCount": int(getattr(r, "POCount", 0) or 0),
            "PurchaseSpend": float(getattr(r, "PurchaseSpend", 0) or 0),
            "ShareOfObservedPOsPct": float(getattr(r, "ShareOfObservedPOsPct", 0) or 0),
            "AverageLeadTimeDays": getattr(r, "AverageLeadTimeDays", None),
        })
    pd.DataFrame(rows).to_csv(CSV / "chart_aging_buckets.csv", index=False)

    po = pd.read_csv(CSV / "fact_purchase_order.csv")
    match = (
        po["PurchaseOrderMatchStatus"]
        .value_counts()
        .rename_axis("PurchaseOrderMatchStatus")
        .reset_index(name="POCount")
    )
    match.to_csv(CSV / "chart_match_status.csv", index=False)

    # Purchase-side PO keys only (excludes invoice-only) — reconciles to 5,543
    purchase_side = po[po["PurchaseQuantity"].notna()].copy()
    pd.DataFrame({
        "Metric": [
            "PurchaseSidePOKeys",
            "ObservedLeadTimeOrders",
            "ReconciliationKeysFullOuter",
            "MatchedPOKeys",
            "PurchaseOnlyKeys",
            "InvoiceOnlyKeys",
        ],
        "Value": [
            int(purchase_side[["VendorNumber", "PONumber"]].drop_duplicates().shape[0]),
            int(po["ObservedLeadTimeDays"].notna().sum()),
            int(len(po)),
            int((po["PurchaseOrderMatchStatus"] == "Matched").sum()),
            int((po["PurchaseOrderMatchStatus"] == "Purchase only").sum()),
            int((po["PurchaseOrderMatchStatus"] == "Invoice only").sum()),
        ],
    }).to_csv(CSV / "chart_po_key_metrics.csv", index=False)

    inv = pd.read_csv(CSV / "fact_inventory_summary.csv")
    store = (
        inv.groupby("Store", dropna=False)
        .agg(
            BeginningUnits=("BeginningUnits", "sum"),
            EndingUnits=("EndingUnits", "sum"),
            EndingRetailValue=("EndingRetailValue", "sum"),
            LowInventoryFlag=("LowInventoryFlag", "sum"),
        )
        .reset_index()
        .sort_values("EndingUnits", ascending=False)
        .head(30)
    )
    store.to_csv(CSV / "chart_store_inventory.csv", index=False)

    brand_inv = (
        inv.groupby("Brand", dropna=False)
        .agg(
            BeginningUnits=("BeginningUnits", "sum"),
            EndingUnits=("EndingUnits", "sum"),
            EndingRetailValue=("EndingRetailValue", "sum"),
            LowInventoryFlag=("LowInventoryFlag", "sum"),
        )
        .reset_index()
        .sort_values("EndingRetailValue", ascending=False)
        .head(30)
    )
    brand_inv.to_csv(CSV / "chart_brand_inventory.csv", index=False)

    risk = pd.read_csv(CSV / "procurement_planning_risk.csv")
    risk.sort_values(["RiskFlagCount", "TotalPurchaseDollars"], ascending=[False, False]).head(40).to_csv(
        CSV / "chart_planning_risk.csv", index=False
    )

    supplier_lt = pd.read_csv(CSV / "supplier_lead_time_summary.csv").sort_values(
        "MedianLeadTimeDays", ascending=False
    ).head(25)
    supplier_lt.to_csv(CSV / "chart_supplier_lead_time.csv", index=False)

    status = (
        sp["PerformanceStatus"].value_counts().rename_axis("PerformanceStatus").reset_index(name="SupplierCount")
    )
    status.to_csv(CSV / "chart_performance_status.csv", index=False)

    # Portfolio inventory KPIs from full fact (not top-N chart extracts)
    ending_avail = pd.to_numeric(inv["EndingAvailable"], errors="coerce")
    ending_units = pd.to_numeric(inv["EndingUnits"], errors="coerce")
    avail_rate = float(ending_avail.sum() / ending_units.notna().sum()) if ending_units.notna().any() else None
    pd.DataFrame({
        "Metric": [
            "BeginningUnits",
            "EndingUnits",
            "EndingRetailValue",
            "LowInventoryFlags",
            "AvailabilityRate",
        ],
        "Value": [
            float(pd.to_numeric(inv["BeginningUnits"], errors="coerce").fillna(0).sum()),
            float(ending_units.fillna(0).sum()),
            float(pd.to_numeric(inv["EndingRetailValue"], errors="coerce").fillna(0).sum()),
            float(pd.to_numeric(inv["LowInventoryFlag"], errors="coerce").fillna(0).sum()),
            avail_rate,
        ],
    }).to_csv(CSV / "chart_inventory_kpis.csv", index=False)

    print("chart datasets written to", CSV)


if __name__ == "__main__":
    main()
