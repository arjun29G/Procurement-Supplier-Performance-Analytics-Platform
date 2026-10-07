"""Procurement and supplier performance analytical layer.

Builds dimension and fact-style tables from existing SQL Server source and
summary tables. Metrics use only fields present in the source data.

True OTIF (on-time in-full vs promised date and ordered quantity) is NOT
supported: the sources have no promised delivery date and no distinct
ordered-vs-received quantity. Closest supported delivery metrics are invoice
match status and matched PO quantity/dollar reconciliation variances.
PO cycle-time aging uses ObservedLeadTimeDays (PODate to last ReceivingDate),
not open-order aging.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
EXPORT_DIR = PROJECT_DIR / "exports"

# Portfolio reference values from validated CSV audit (preserve unless source changes).
VALIDATED_PURCHASE_DOLLARS = 321_900_765.53
VALIDATED_PURCHASE_ROWS = 2_372_474
VALIDATED_LEAD_TIME_ORDERS = 5_543
VALIDATED_LEAD_TIME_MEDIAN = 10.0

DIM_SUPPLIER_SQL = """
SELECT
    VendorNumber,
    MAX(LTRIM(RTRIM(VendorName))) AS VendorName,
    COUNT(DISTINCT Brand) AS BrandCount,
    COUNT(DISTINCT PONumber) AS PurchaseOrderCount,
    SUM(CAST(Quantity AS DECIMAL(28, 4))) AS PurchaseQuantity,
    SUM(CAST(Dollars AS DECIMAL(28, 4))) AS PurchaseSpend
FROM dbo.purchases
GROUP BY VendorNumber
"""

DIM_BRAND_SQL = """
SELECT
    Brand,
    MAX(LTRIM(RTRIM(Description))) AS Description,
    MAX(LTRIM(RTRIM(Size))) AS Size,
    MAX(Classification) AS Classification,
    COUNT(DISTINCT VendorNumber) AS PurchasingVendorCount
FROM dbo.purchases
GROUP BY Brand
"""

DIM_STORE_SQL = """
SELECT Store, MAX(LTRIM(RTRIM(City))) AS City
FROM (
    SELECT Store, City FROM dbo.begin_inventory
    UNION ALL
    SELECT Store, City FROM dbo.end_inventory
    UNION ALL
    SELECT Store, CAST(NULL AS NVARCHAR(255)) AS City FROM dbo.purchases
) s
GROUP BY Store
"""

FACT_PROCUREMENT_MONTHLY_SQL = """
SELECT
    VendorNumber,
    MAX(LTRIM(RTRIM(VendorName))) AS VendorName,
    Brand,
    MAX(LTRIM(RTRIM(Description))) AS Description,
    DATEFROMPARTS(YEAR(PODate), MONTH(PODate), 1) AS SpendMonth,
    COUNT(DISTINCT PONumber) AS PurchaseOrderCount,
    SUM(CAST(Quantity AS DECIMAL(28, 4))) AS PurchaseQuantity,
    SUM(CAST(Dollars AS DECIMAL(28, 4))) AS PurchaseSpend,
    AVG(CAST(PurchasePrice AS FLOAT)) AS AveragePurchasePrice
FROM dbo.purchases
WHERE PODate IS NOT NULL
GROUP BY VendorNumber, Brand, DATEFROMPARTS(YEAR(PODate), MONTH(PODate), 1)
"""

def _run_query(conn, query: str) -> pd.DataFrame:
    return pd.read_sql_query(text(query), conn)


def create_dimensions(conn) -> dict[str, pd.DataFrame]:
    dim_supplier = _run_query(conn, DIM_SUPPLIER_SQL)
    dim_brand = _run_query(conn, DIM_BRAND_SQL)
    dim_store = _run_query(conn, DIM_STORE_SQL)

    date_bounds = conn.execute(text(
        """
        SELECT MIN(d) AS MinDate, MAX(d) AS MaxDate
        FROM (
            SELECT PODate AS d FROM dbo.purchases
            UNION ALL SELECT ReceivingDate FROM dbo.purchases
            UNION ALL SELECT SalesDate FROM dbo.sales
            UNION ALL SELECT InvoiceDate FROM dbo.vendor_invoice
        ) dates
        WHERE d IS NOT NULL
        """
    )).one()
    if date_bounds.MinDate is None or date_bounds.MaxDate is None:
        dim_date = pd.DataFrame(columns=[
            "DateKey", "FullDate", "Year", "Month", "MonthName",
            "YearMonth", "Quarter",
        ])
    else:
        dates = pd.date_range(date_bounds.MinDate, date_bounds.MaxDate, freq="D")
        dim_date = pd.DataFrame({"FullDate": dates})
        dim_date["DateKey"] = dim_date["FullDate"].dt.strftime("%Y%m%d").astype(int)
        dim_date["Year"] = dim_date["FullDate"].dt.year
        dim_date["Month"] = dim_date["FullDate"].dt.month
        dim_date["MonthName"] = dim_date["FullDate"].dt.strftime("%b")
        dim_date["YearMonth"] = dim_date["FullDate"].dt.strftime("%Y-%m")
        dim_date["Quarter"] = dim_date["FullDate"].dt.quarter

    return {
        "dim_supplier": dim_supplier,
        "dim_brand": dim_brand,
        "dim_store": dim_store,
        "dim_date": dim_date,
    }


def enhance_purchase_orders(purchase_orders: pd.DataFrame) -> pd.DataFrame:
    """Add cycle-time aging, processing days, and delivery-reconciliation flags."""
    po = purchase_orders.copy()
    for column in (
        "PODate", "FirstReceivingDate", "LastReceivingDate", "InvoiceDate", "PayDate",
    ):
        if column in po.columns:
            po[column] = pd.to_datetime(po[column], errors="coerce")

    po["ObservedLeadTimeDays"] = pd.to_numeric(po["ObservedLeadTimeDays"], errors="coerce")
    po["PurchaseQuantity"] = pd.to_numeric(po["PurchaseQuantity"], errors="coerce")
    po["PurchaseDollars"] = pd.to_numeric(po["PurchaseDollars"], errors="coerce")
    po["InvoiceQuantity"] = pd.to_numeric(po["InvoiceQuantity"], errors="coerce")
    po["InvoiceDollars"] = pd.to_numeric(po["InvoiceDollars"], errors="coerce")
    po["Freight"] = pd.to_numeric(po["Freight"], errors="coerce")
    po["InvoiceQuantityVariance"] = pd.to_numeric(
        po["InvoiceQuantityVariance"], errors="coerce"
    )
    po["InvoiceDollarsVariance"] = pd.to_numeric(
        po["InvoiceDollarsVariance"], errors="coerce"
    )

    # Cycle-time aging of completed observed lead times (NOT open-PO aging).
    bins = [-0.1, 7, 15, 30, 60, float("inf")]
    labels = ["0-7 days", "8-15 days", "16-30 days", "31-60 days", "60+ days"]
    po["POCycleAgingBucket"] = pd.cut(
        po["ObservedLeadTimeDays"], bins=bins, labels=labels
    ).astype("string")

    po["POToInvoiceDays"] = (
        po["InvoiceDate"] - po["PODate"]
    ).dt.days.where(po["PODate"].notna() & po["InvoiceDate"].notna())
    po["ReceiptToInvoiceDays"] = (
        po["InvoiceDate"] - po["LastReceivingDate"]
    ).dt.days.where(
        po["LastReceivingDate"].notna() & po["InvoiceDate"].notna()
    )

    matched = po["PurchaseOrderMatchStatus"].eq("Matched")
    po["HasInvoiceMatch"] = matched.astype(int)
    # Reconciliation discrepancy flags — NOT OTIF short/late classifications.
    po["QuantityReconciliationFlag"] = np.where(
        ~matched,
        "Not matched",
        np.where(
            po["InvoiceQuantityVariance"].isna(),
            "Unknown",
            np.where(
                po["InvoiceQuantityVariance"].abs() < 1e-9,
                "Quantity aligned",
                np.where(
                    po["InvoiceQuantityVariance"] < 0,
                    "Invoice qty below purchase qty",
                    "Invoice qty above purchase qty",
                ),
            ),
        ),
    )
    po["DollarReconciliationFlag"] = np.where(
        ~matched,
        "Not matched",
        np.where(
            po["InvoiceDollarsVariance"].isna(),
            "Unknown",
            np.where(
                po["InvoiceDollarsVariance"].abs() < 0.01,
                "Dollars aligned",
                np.where(
                    po["InvoiceDollarsVariance"] < 0,
                    "Invoice dollars below purchase dollars",
                    "Invoice dollars above purchase dollars",
                ),
            ),
        ),
    )
    po["DeliveryMetricBasis"] = (
        "Observed lead time + invoice reconciliation "
        "(true OTIF unsupported: no promised date / ordered qty)"
    )
    return po


def create_fact_procurement_monthly(conn) -> pd.DataFrame:
    return _run_query(conn, FACT_PROCUREMENT_MONTHLY_SQL)


def create_fact_inventory_summary(inventory_position: pd.DataFrame) -> pd.DataFrame:
    inv = inventory_position.copy()
    for column in (
        "BeginningUnits", "EndingUnits", "BeginningRetailValue",
        "EndingRetailValue", "AverageInventoryUnits",
    ):
        if column in inv.columns:
            inv[column] = pd.to_numeric(inv[column], errors="coerce")
    inv["LowInventoryFlag"] = np.where(
        inv["EndingUnits"].isna(),
        np.nan,
        np.where(inv["EndingUnits"] <= 0, 1, 0),
    )
    inv["InventoryMovementUnits"] = (
        inv["EndingUnits"] - inv["BeginningUnits"]
    ).where(inv["BeginningUnits"].notna() & inv["EndingUnits"].notna())
    return inv


def create_lead_time_distribution(purchase_orders: pd.DataFrame) -> pd.DataFrame:
    lt = purchase_orders.loc[
        purchase_orders["ObservedLeadTimeDays"].notna(),
        ["VendorNumber", "VendorName", "PONumber", "PODate", "LastReceivingDate",
         "ObservedLeadTimeDays", "PurchaseDollars", "POCycleAgingBucket"],
    ].copy()
    if lt.empty:
        return lt

    portfolio_median = float(lt["ObservedLeadTimeDays"].median())
    portfolio_mean = float(lt["ObservedLeadTimeDays"].mean())
    portfolio_std = float(lt["ObservedLeadTimeDays"].std(ddof=1))
    # Tukey-style outlier rule on observed lead time.
    q1 = float(lt["ObservedLeadTimeDays"].quantile(0.25))
    q3 = float(lt["ObservedLeadTimeDays"].quantile(0.75))
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    lt["LeadTimeOutlierFlag"] = (
        (lt["ObservedLeadTimeDays"] < lower) | (lt["ObservedLeadTimeDays"] > upper)
    ).astype(int)
    lt["PortfolioMedianLeadTimeDays"] = portfolio_median
    lt["PortfolioMeanLeadTimeDays"] = portfolio_mean
    lt["PortfolioStdLeadTimeDays"] = portfolio_std
    lt["LeadTimeVsMedianDays"] = lt["ObservedLeadTimeDays"] - portfolio_median
    return lt


def create_supplier_lead_time_summary(lead_time_detail: pd.DataFrame) -> pd.DataFrame:
    if lead_time_detail.empty:
        return pd.DataFrame()

    grouped = lead_time_detail.groupby(
        ["VendorNumber", "VendorName"], dropna=False, as_index=False
    ).agg(
        ObservedLeadTimeOrderCount=("PONumber", "nunique"),
        AverageLeadTimeDays=("ObservedLeadTimeDays", "mean"),
        MedianLeadTimeDays=("ObservedLeadTimeDays", "median"),
        MinLeadTimeDays=("ObservedLeadTimeDays", "min"),
        MaxLeadTimeDays=("ObservedLeadTimeDays", "max"),
        LeadTimeStdDevDays=("ObservedLeadTimeDays", lambda s: s.std(ddof=1)),
        LeadTimeVarianceDays=("ObservedLeadTimeDays", lambda s: s.var(ddof=1)),
        OutlierLeadTimeOrderCount=("LeadTimeOutlierFlag", "sum"),
        LeadTimePurchaseSpend=("PurchaseDollars", "sum"),
    )
    grouped["LeadTimeConsistencyScore"] = (
        1 / (1 + grouped["LeadTimeStdDevDays"].fillna(0))
    )
    return grouped


def create_delivery_reconciliation_summary(purchase_orders: pd.DataFrame) -> pd.DataFrame:
    """Closest supported delivery-performance metrics (NOT OTIF)."""
    po = purchase_orders.copy()
    grouped = po.groupby(["VendorNumber", "VendorName"], dropna=False, as_index=False).agg(
        PurchaseOrderKeyCount=("PONumber", "count"),
        PurchaseSidePOCount=("PurchaseQuantity", lambda s: s.notna().sum()),
        MatchedPOCount=("HasInvoiceMatch", "sum"),
        PurchaseOnlyPOCount=(
            "PurchaseOrderMatchStatus",
            lambda s: (s == "Purchase only").sum(),
        ),
        InvoiceOnlyPOCount=(
            "PurchaseOrderMatchStatus",
            lambda s: (s == "Invoice only").sum(),
        ),
        PurchaseSpend=("PurchaseDollars", "sum"),
        InvoiceSpend=("InvoiceDollars", "sum"),
        FreightSpend=("Freight", "sum"),
        QuantityAlignedPOCount=(
            "QuantityReconciliationFlag",
            lambda s: (s == "Quantity aligned").sum(),
        ),
        QuantityDiscrepancyPOCount=(
            "QuantityReconciliationFlag",
            lambda s: s.isin([
                "Invoice qty below purchase qty",
                "Invoice qty above purchase qty",
            ]).sum(),
        ),
        DollarAlignedPOCount=(
            "DollarReconciliationFlag",
            lambda s: (s == "Dollars aligned").sum(),
        ),
        DollarDiscrepancyPOCount=(
            "DollarReconciliationFlag",
            lambda s: s.isin([
                "Invoice dollars below purchase dollars",
                "Invoice dollars above purchase dollars",
            ]).sum(),
        ),
    )
    grouped["InvoiceMatchRatePct"] = (
        grouped["MatchedPOCount"] / grouped["PurchaseOrderKeyCount"] * 100
    ).where(grouped["PurchaseOrderKeyCount"] > 0)
    grouped["QuantityAlignmentRatePct"] = (
        grouped["QuantityAlignedPOCount"] / grouped["MatchedPOCount"] * 100
    ).where(grouped["MatchedPOCount"] > 0)
    grouped["DollarAlignmentRatePct"] = (
        grouped["DollarAlignedPOCount"] / grouped["MatchedPOCount"] * 100
    ).where(grouped["MatchedPOCount"] > 0)
    grouped["OTIFSupported"] = False
    grouped["DeliveryMetricLabel"] = "Invoice reconciliation performance (not OTIF)"
    return grouped


def _percentile_rank(series: pd.Series, higher_is_better: bool) -> pd.Series:
    ranks = series.rank(pct=True, method="average")
    return ranks if higher_is_better else (1 - ranks)


def create_fact_supplier_performance(
    vendor_performance: pd.DataFrame,
    lead_time_summary: pd.DataFrame,
    delivery_summary: pd.DataFrame,
    vendor_sales_summary: pd.DataFrame,
) -> pd.DataFrame:
    """Comprehensive supplier scorecard with documented segmentation thresholds."""
    base = vendor_performance.copy()
    frame = base.merge(
        lead_time_summary,
        on=["VendorNumber", "VendorName"],
        how="left",
        validate="one_to_one",
    ).merge(
        delivery_summary,
        on=["VendorNumber", "VendorName"],
        how="left",
        validate="one_to_one",
    )

    price = vendor_sales_summary.groupby(
        ["VendorNumber", "VendorName"], dropna=False, as_index=False
    ).agg(
        BrandsWithReferencePrice=("ReferencePurchasePrice", lambda s: s.notna().sum()),
        AveragePurchasePriceVariancePct=(
            "PurchasePriceVariancePct",
            lambda s: s.mean(skipna=True),
        ),
        AbsoluteAvgPriceVariancePct=(
            "PurchasePriceVariancePct",
            lambda s: s.abs().mean(skipna=True),
        ),
    )
    frame = frame.merge(
        price, on=["VendorNumber", "VendorName"], how="left", validate="one_to_one"
    )

    # Composite score only for suppliers with enough observed lead-time orders.
    eligible = frame["ObservedLeadTimeOrderCount"].fillna(0) >= 5
    score_parts = pd.DataFrame(index=frame.index)
    score_parts["lead_consistency"] = _percentile_rank(
        frame["LeadTimeConsistencyScore"], higher_is_better=True
    )
    score_parts["invoice_match"] = _percentile_rank(
        frame["InvoiceMatchRatePct"], higher_is_better=True
    )
    score_parts["qty_align"] = _percentile_rank(
        frame["QuantityAlignmentRatePct"], higher_is_better=True
    )
    score_parts["price_control"] = _percentile_rank(
        frame["AbsoluteAvgPriceVariancePct"], higher_is_better=False
    )
    score_parts["lead_level"] = _percentile_rank(
        frame["MedianLeadTimeDays"], higher_is_better=False
    )
    composite = score_parts.mean(axis=1, skipna=True) * 100
    frame["SupplierPerformanceScore"] = composite.where(eligible)

    scored = frame.loc[eligible & frame["SupplierPerformanceScore"].notna()].copy()
    if scored.empty:
        frame["PerformanceStatus"] = pd.Series("Insufficient data", index=frame.index)
    else:
        p10 = scored["SupplierPerformanceScore"].quantile(0.10)
        p25 = scored["SupplierPerformanceScore"].quantile(0.25)
        p75 = scored["SupplierPerformanceScore"].quantile(0.75)

        def _status(row):
            if not eligible.loc[row.name] or pd.isna(row["SupplierPerformanceScore"]):
                return "Insufficient data"
            score = row["SupplierPerformanceScore"]
            if score >= p75:
                return "High Performing"
            if score >= p25:
                return "Stable / Acceptable"
            if score >= p10:
                return "Watchlist"
            return "Underperforming"

        frame["PerformanceStatus"] = frame.apply(_status, axis=1)
        frame["ScoreThresholdP75"] = p75
        frame["ScoreThresholdP25"] = p25
        frame["ScoreThresholdP10"] = p10

    # Explicit unsupported OTIF placeholders kept null (never fabricated).
    frame["OTIFPct"] = np.nan
    frame["OnTimePct"] = np.nan
    frame["InFullPct"] = np.nan
    frame["OTIFSupported"] = False
    frame["OTIFLimitationNote"] = (
        "Source data has no promised delivery date and no distinct ordered quantity; "
        "OTIF/On-Time/In-Full cannot be calculated. Use InvoiceMatchRatePct and "
        "QuantityAlignmentRatePct as delivery-reconciliation proxies."
    )
    return frame


def create_procurement_planning_risk(
    supplier_performance: pd.DataFrame,
    purchase_orders: pd.DataFrame,
) -> pd.DataFrame:
    portfolio_median = purchase_orders["ObservedLeadTimeDays"].median()
    portfolio_p75 = purchase_orders["ObservedLeadTimeDays"].quantile(0.75)
    total_spend = supplier_performance["TotalPurchaseDollars"].sum()

    risk = supplier_performance[[
        "VendorNumber", "VendorName", "TotalPurchaseDollars", "PurchaseContributionPct",
        "MedianLeadTimeDays", "LeadTimeStdDevDays", "LeadTimeVarianceDays",
        "InvoiceMatchRatePct", "QuantityAlignmentRatePct",
        "AbsoluteAvgPriceVariancePct", "EligibleInventoryTurnover",
        "EstimatedEndingInventoryCost", "SupplierPerformanceScore", "PerformanceStatus",
        "ObservedLeadTimeOrderCount", "MatchedPOCount", "PurchaseOrderKeyCount",
    ]].copy()

    risk["HighLeadTimeRisk"] = (
        risk["MedianLeadTimeDays"].notna()
        & (risk["MedianLeadTimeDays"] > portfolio_p75)
    ).astype(int)
    risk["LeadTimeConsistencyRisk"] = (
        risk["LeadTimeStdDevDays"].notna()
        & (risk["LeadTimeStdDevDays"] > purchase_orders["ObservedLeadTimeDays"].std(ddof=1))
    ).astype(int)
    risk["DeliveryReconciliationRisk"] = (
        risk["InvoiceMatchRatePct"].notna()
        & (risk["InvoiceMatchRatePct"] < 70)
    ).astype(int)
    risk["PriceVarianceRisk"] = (
        risk["AbsoluteAvgPriceVariancePct"].notna()
        & (risk["AbsoluteAvgPriceVariancePct"] > risk["AbsoluteAvgPriceVariancePct"].median())
    ).astype(int)
    risk["HighSpendConcentrationFlag"] = (
        risk["PurchaseContributionPct"].fillna(0) >= 5
    ).astype(int)
    risk["InventoryExposureRisk"] = (
        risk["EstimatedEndingInventoryCost"].notna()
        & (
            risk["EstimatedEndingInventoryCost"]
            > risk["EstimatedEndingInventoryCost"].median(skipna=True)
        )
    ).astype(int)

    cycle_high = purchase_orders[
        purchase_orders["POCycleAgingBucket"].isin(["16-30 days", "31-60 days", "60+ days"])
    ]
    # With current data max LT is 14 days; also flag relative high cycle time (>= P75).
    relative_high = purchase_orders[
        purchase_orders["ObservedLeadTimeDays"].notna()
        & (purchase_orders["ObservedLeadTimeDays"] >= portfolio_p75)
    ]
    high_aging_counts = relative_high.groupby("VendorNumber").size().rename(
        "HighCycleTimePOCount"
    )
    risk = risk.merge(high_aging_counts, on="VendorNumber", how="left")
    risk["HighCycleTimePOCount"] = risk["HighCycleTimePOCount"].fillna(0).astype(int)
    risk["POAgingRisk"] = (risk["HighCycleTimePOCount"] > 0).astype(int)

    risk["RiskFlagCount"] = (
        risk["HighLeadTimeRisk"]
        + risk["LeadTimeConsistencyRisk"]
        + risk["DeliveryReconciliationRisk"]
        + risk["PriceVarianceRisk"]
        + risk["InventoryExposureRisk"]
        + risk["POAgingRisk"]
    )
    risk["SupplierConcentrationPct"] = (
        risk["TotalPurchaseDollars"] / total_spend * 100 if total_spend else np.nan
    )
    risk["PortfolioMedianLeadTimeDays"] = portfolio_median
    risk["RecommendedAttention"] = np.where(
        risk["PerformanceStatus"].isin(["Underperforming", "Watchlist"])
        | (risk["RiskFlagCount"] >= 3)
        | (risk["HighSpendConcentrationFlag"] & (risk["RiskFlagCount"] >= 2)),
        "Prioritize procurement review",
        np.where(
            risk["RiskFlagCount"] >= 1,
            "Monitor",
            "Maintain",
        ),
    )
    # Keep unused variable referenced for clarity that absolute long buckets may be empty.
    _ = cycle_high
    return risk


def create_po_aging_summary(purchase_orders: pd.DataFrame) -> pd.DataFrame:
    with_lt = purchase_orders[purchase_orders["ObservedLeadTimeDays"].notna()].copy()
    if with_lt.empty:
        return pd.DataFrame(columns=[
            "POCycleAgingBucket", "POCount", "PurchaseSpend", "ShareOfObservedPOsPct",
        ])
    summary = with_lt.groupby("POCycleAgingBucket", dropna=False, as_index=False).agg(
        POCount=("PONumber", "count"),
        PurchaseSpend=("PurchaseDollars", "sum"),
        AverageLeadTimeDays=("ObservedLeadTimeDays", "mean"),
    )
    total = summary["POCount"].sum()
    summary["ShareOfObservedPOsPct"] = summary["POCount"] / total * 100 if total else np.nan
    summary["AgingBasis"] = (
        "Observed PO cycle time = LastReceivingDate - PODate "
        "(completed receipts only; not open-order aging)"
    )
    return summary


def create_brand_lead_time_summary(conn, purchase_orders: pd.DataFrame) -> pd.DataFrame:
    """Brand-level lead time using purchase lines' vendor-PO lead times."""
    brand_po = _run_query(conn, """
        SELECT
            p.Brand,
            MAX(LTRIM(RTRIM(p.Description))) AS Description,
            p.VendorNumber,
            p.PONumber
        FROM dbo.purchases p
        GROUP BY p.Brand, p.VendorNumber, p.PONumber
    """)
    lt_lookup = purchase_orders[
        purchase_orders["ObservedLeadTimeDays"].notna()
    ][["VendorNumber", "PONumber", "ObservedLeadTimeDays", "PurchaseDollars"]]
    merged = brand_po.merge(lt_lookup, on=["VendorNumber", "PONumber"], how="inner")
    if merged.empty:
        return pd.DataFrame()
    return merged.groupby(["Brand", "Description"], dropna=False, as_index=False).agg(
        ObservedLeadTimeOrderCount=("PONumber", "nunique"),
        AverageLeadTimeDays=("ObservedLeadTimeDays", "mean"),
        MedianLeadTimeDays=("ObservedLeadTimeDays", "median"),
        LeadTimeStdDevDays=("ObservedLeadTimeDays", lambda s: s.std(ddof=1)),
        LeadTimeVarianceDays=("ObservedLeadTimeDays", lambda s: s.var(ddof=1)),
        PurchasingVendorCount=("VendorNumber", "nunique"),
    )


def validate_procurement_outputs(
    purchase_orders: pd.DataFrame,
    supplier_performance: pd.DataFrame,
) -> dict:
    lt = purchase_orders["ObservedLeadTimeDays"].dropna()
    spend = float(supplier_performance["TotalPurchaseDollars"].sum())
    results = {
        "observed_lead_time_orders": int(lt.shape[0]),
        "observed_lead_time_median_days": float(lt.median()) if len(lt) else None,
        "observed_lead_time_std_days": float(lt.std(ddof=1)) if len(lt) > 1 else None,
        "supplier_purchase_spend": spend,
        "lead_time_order_count_matches_validated": (
            int(lt.shape[0]) == VALIDATED_LEAD_TIME_ORDERS
        ),
        "lead_time_median_matches_validated": (
            len(lt) > 0
            and math.isclose(float(lt.median()), VALIDATED_LEAD_TIME_MEDIAN, abs_tol=1e-9)
        ),
        "purchase_spend_matches_validated": math.isclose(
            spend, VALIDATED_PURCHASE_DOLLARS, abs_tol=0.01
        ),
        "otif_fabricated": False,
        "otif_columns_all_null": bool(
            supplier_performance["OTIFPct"].isna().all()
            and supplier_performance["OnTimePct"].isna().all()
            and supplier_performance["InFullPct"].isna().all()
        ),
    }
    failures = [
        name for name, ok in results.items()
        if name.endswith("_matches_validated") and not ok
    ]
    if failures:
        raise ValueError(f"Procurement validation failed: {failures}; details={results}")
    if not results["otif_columns_all_null"]:
        raise ValueError("OTIF columns must remain null; fabrication detected")
    logger.info("Procurement analytical validation passed: %s", results)
    return results


def build_procurement_analytics(
    conn,
    purchase_orders: pd.DataFrame,
    vendor_performance: pd.DataFrame,
    vendor_sales_summary: pd.DataFrame,
    inventory_position: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    logger.info("Building procurement analytical dimensions and facts")
    dimensions = create_dimensions(conn)
    enhanced_po = enhance_purchase_orders(purchase_orders)
    lead_detail = create_lead_time_distribution(enhanced_po)
    lead_supplier = create_supplier_lead_time_summary(lead_detail)
    delivery = create_delivery_reconciliation_summary(enhanced_po)
    supplier_perf = create_fact_supplier_performance(
        vendor_performance, lead_supplier, delivery, vendor_sales_summary
    )
    planning_risk = create_procurement_planning_risk(supplier_perf, enhanced_po)
    po_aging = create_po_aging_summary(enhanced_po)
    brand_lead = create_brand_lead_time_summary(conn, enhanced_po)
    monthly = create_fact_procurement_monthly(conn)
    inventory_summary = create_fact_inventory_summary(inventory_position)

    validate_procurement_outputs(enhanced_po, supplier_perf)

    return {
        **dimensions,
        "fact_purchase_order": enhanced_po,
        "fact_lead_time": lead_detail,
        "supplier_lead_time_summary": lead_supplier,
        "brand_lead_time_summary": brand_lead,
        "fact_delivery_reconciliation": delivery,
        "fact_supplier_performance": supplier_perf,
        "procurement_planning_risk": planning_risk,
        "po_cycle_aging_summary": po_aging,
        "fact_procurement_monthly": monthly,
        "fact_inventory_summary": inventory_summary,
    }


def export_procurement_excel(tables: dict[str, pd.DataFrame], output_path: Path | None = None) -> Path:
    """Advanced Excel supporting extract for procurement planning (not primary engine)."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output_path or (EXPORT_DIR / "Procurement_Supplier_Performance_Extract.xlsx")

    supplier = tables["fact_supplier_performance"].copy()
    export_cols = [
        "VendorNumber", "VendorName", "PurchaseOrderKeyCount", "MatchedPOCount",
        "TotalPurchaseDollars", "TotalPurchaseQuantity", "AverageLeadTimeDays",
        "MedianLeadTimeDays", "LeadTimeVarianceDays", "LeadTimeStdDevDays",
        "InvoiceMatchRatePct", "QuantityAlignmentRatePct", "DollarAlignmentRatePct",
        "AbsoluteAvgPriceVariancePct", "EstimatedEndingInventoryCost",
        "EligibleInventoryTurnover", "SupplierPerformanceScore", "PerformanceStatus",
        "OTIFSupported", "OTIFLimitationNote",
    ]
    available = [c for c in export_cols if c in supplier.columns]
    planning = tables["procurement_planning_risk"]
    aging = tables["po_cycle_aging_summary"]
    lead = tables["supplier_lead_time_summary"]

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        supplier[available].to_excel(writer, sheet_name="Supplier Scorecard", index=False)
        planning.to_excel(writer, sheet_name="Procurement Planning Risk", index=False)
        aging.to_excel(writer, sheet_name="PO Cycle Aging", index=False)
        lead.to_excel(writer, sheet_name="Supplier Lead Time", index=False)
        tables["fact_procurement_monthly"].head(100000).to_excel(
            writer, sheet_name="Monthly Spend Sample", index=False
        )
        notes = pd.DataFrame({
            "Note": [
                "True OTIF is not supported by source fields.",
                "Delivery metrics are invoice reconciliation rates.",
                "PO aging buckets are observed cycle-time (PO to last receipt), not open-PO aging.",
                "Vendor inventory exposure only for single-vendor complete-snapshot brands.",
                f"Validated purchase spend target: {VALIDATED_PURCHASE_DOLLARS}",
                f"Validated observed lead-time orders: {VALIDATED_LEAD_TIME_ORDERS}",
                f"Validated median lead time days: {VALIDATED_LEAD_TIME_MEDIAN}",
            ]
        })
        notes.to_excel(writer, sheet_name="Methodology Notes", index=False)
    logger.info("Wrote Excel extract to %s", output_path)
    return output_path
