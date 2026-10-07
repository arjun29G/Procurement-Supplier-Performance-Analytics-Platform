import logging
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from procurement_analytics import build_procurement_analytics, export_procurement_excel


PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "get_vendor_summary.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)


VENDOR_SUMMARY_SQL = """
WITH PurchaseSummary AS (
    SELECT
        VendorNumber,
        Brand,
        MAX(LTRIM(RTRIM(VendorName))) AS VendorName,
        MAX(LTRIM(RTRIM(Description))) AS Description,
        SUM(CAST(Quantity AS DECIMAL(28, 4))) AS TotalPurchaseQuantity,
        SUM(CAST(Dollars AS DECIMAL(28, 4))) AS TotalPurchaseDollars,
        SUM(CASE WHEN PurchasePrice > 0 AND Quantity > 0
            THEN CAST(Quantity AS DECIMAL(28, 4)) * CAST(PurchasePrice AS DECIMAL(28, 4)) END)
            / NULLIF(SUM(CASE WHEN PurchasePrice > 0 AND Quantity > 0
                THEN CAST(Quantity AS DECIMAL(28, 4)) END), 0) AS PurchasePrice,
        SUM(CASE WHEN PurchasePrice > 0 AND Quantity > 0
            THEN CAST(Quantity AS DECIMAL(28, 4)) END) AS CostCoveredPurchaseQuantity
    FROM dbo.purchases
    GROUP BY VendorNumber, Brand
),
PriceSummary AS (
    SELECT
        VendorNumber,
        Brand,
        CASE WHEN MIN(Price) = MAX(Price) THEN MAX(Price) END AS ActualPrice,
        CASE WHEN MIN(PurchasePrice) = MAX(PurchasePrice) THEN MAX(PurchasePrice) END
            AS ReferencePurchasePrice,
        MAX(TRY_CONVERT(FLOAT, Volume)) AS Volume
    FROM dbo.purchase_prices
    GROUP BY VendorNumber, Brand
),
SalesSummary AS (
    SELECT
        VendorNo,
        Brand,
        MAX(LTRIM(RTRIM(VendorName))) AS VendorName,
        MAX(LTRIM(RTRIM(Description))) AS Description,
        SUM(CAST(SalesDollars AS DECIMAL(28, 4))) AS TotalSalesDollars,
        SUM(CAST(SalesQuantity AS DECIMAL(28, 4))) AS TotalSalesQuantity,
        SUM(CAST(ExciseTax AS DECIMAL(28, 4))) AS TotalExciseTax,
        SUM(CAST(SalesPrice AS DECIMAL(28, 4)) * CAST(SalesQuantity AS DECIMAL(28, 4)))
            / NULLIF(SUM(CAST(SalesQuantity AS DECIMAL(28, 4))), 0) AS TotalSalesPrice
    FROM dbo.sales
    GROUP BY VendorNo, Brand
),
FreightSummary AS (
    SELECT VendorNumber, SUM(CAST(Freight AS DECIMAL(28, 4))) AS VendorFreight
    FROM dbo.vendor_invoice
    GROUP BY VendorNumber
),
VendorPurchaseTotal AS (
    SELECT VendorNumber, SUM(TotalPurchaseDollars) AS VendorPurchaseDollars
    FROM PurchaseSummary
    GROUP BY VendorNumber
),
BrandVendorCount AS (
    SELECT Brand, COUNT(DISTINCT VendorNumber) AS VendorCount
    FROM dbo.purchases
    GROUP BY Brand
),
BeginningInventory AS (
    SELECT Store, Brand, SUM(CAST(onHand AS DECIMAL(28, 4))) AS BeginningUnits
    FROM dbo.begin_inventory
    GROUP BY Store, Brand
),
EndingInventory AS (
    SELECT
        Store,
        Brand,
        SUM(CAST(onHand AS DECIMAL(28, 4))) AS EndingUnits,
        SUM(CAST(onHand AS DECIMAL(28, 4)) * CAST(Price AS DECIMAL(28, 4))) AS EndingRetailValue
    FROM dbo.end_inventory
    GROUP BY Store, Brand
),
InventoryByStoreBrand AS (
    SELECT
        COALESCE(b.Store, e.Store) AS Store,
        COALESCE(b.Brand, e.Brand) AS Brand,
        b.BeginningUnits,
        e.EndingUnits,
        e.EndingRetailValue
    FROM BeginningInventory b
    FULL OUTER JOIN EndingInventory e
        ON b.Store = e.Store AND b.Brand = e.Brand
),
InventoryByBrand AS (
    SELECT
        Brand,
        COUNT(*) AS StoreCount,
        SUM(CASE WHEN BeginningUnits IS NOT NULL AND EndingUnits IS NOT NULL THEN 1 ELSE 0 END)
            AS CompleteStoreCount,
        SUM(CASE WHEN BeginningUnits IS NOT NULL AND EndingUnits IS NOT NULL
            THEN (BeginningUnits + EndingUnits) / 2.0 END) AS AverageInventoryUnits,
        SUM(CASE WHEN BeginningUnits IS NOT NULL AND EndingUnits IS NOT NULL
            THEN EndingUnits END) AS EndingInventoryUnits,
        SUM(CASE WHEN BeginningUnits IS NOT NULL AND EndingUnits IS NOT NULL
            THEN EndingRetailValue END) AS EndingInventoryRetailValue
    FROM InventoryByStoreBrand
    GROUP BY Brand
),
VendorBrandKeys AS (
    SELECT VendorNumber, Brand FROM PurchaseSummary
    UNION
    SELECT VendorNo AS VendorNumber, Brand FROM SalesSummary
)
SELECT
    k.VendorNumber,
    COALESCE(p.VendorName, s.VendorName) AS VendorName,
    k.Brand,
    COALESCE(p.Description, s.Description) AS Description,
    p.PurchasePrice,
    pr.ActualPrice,
    pr.Volume,
    COALESCE(p.TotalPurchaseQuantity, 0) AS TotalPurchaseQuantity,
    p.CostCoveredPurchaseQuantity,
    COALESCE(p.TotalPurchaseDollars, 0) AS TotalPurchaseDollars,
    COALESCE(s.TotalSalesDollars, 0) AS TotalSalesDollars,
    s.TotalSalesPrice,
    COALESCE(s.TotalSalesQuantity, 0) AS TotalSalesQuantity,
    COALESCE(s.TotalExciseTax, 0) AS TotalExciseTax,
    CASE WHEN vpt.VendorPurchaseDollars > 0
        THEN fs.VendorFreight * p.TotalPurchaseDollars / vpt.VendorPurchaseDollars END AS FreightCost,
    pr.ReferencePurchasePrice,
    CASE WHEN pr.ReferencePurchasePrice IS NOT NULL
        THEN p.PurchasePrice - pr.ReferencePurchasePrice END AS PurchasePriceVariance,
    CASE WHEN p.VendorNumber IS NOT NULL AND bvc.VendorCount = 1 AND ib.StoreCount = ib.CompleteStoreCount
        THEN ib.AverageInventoryUnits END AS AverageInventoryUnits,
    CASE WHEN p.VendorNumber IS NOT NULL AND bvc.VendorCount = 1 AND ib.StoreCount = ib.CompleteStoreCount
        THEN ib.EndingInventoryUnits END AS EndingInventoryUnits,
    CASE WHEN p.VendorNumber IS NOT NULL AND bvc.VendorCount = 1 AND ib.StoreCount = ib.CompleteStoreCount
        THEN ib.EndingInventoryRetailValue END AS EndingInventoryRetailValue,
    CASE WHEN p.VendorNumber IS NOT NULL AND bvc.VendorCount = 1 AND ib.StoreCount = ib.CompleteStoreCount
            AND ib.AverageInventoryUnits > 0
        THEN s.TotalSalesQuantity / ib.AverageInventoryUnits END AS StockTurnover,
    CASE WHEN p.VendorNumber IS NOT NULL AND bvc.VendorCount = 1 AND ib.StoreCount = ib.CompleteStoreCount
            AND p.PurchasePrice IS NOT NULL
        THEN ib.EndingInventoryUnits * p.PurchasePrice END AS [Unsold Capital],
        CASE WHEN p.PurchasePrice IS NOT NULL AND p.TotalPurchaseQuantity > 0
                AND s.TotalSalesQuantity > 0
            THEN s.TotalSalesDollars END AS CostCoveredSalesDollars,
        CASE WHEN s.TotalSalesDollars IS NOT NULL
                AND (p.PurchasePrice IS NULL OR p.TotalPurchaseQuantity <= 0
                    OR s.TotalSalesQuantity <= 0)
            THEN s.TotalSalesDollars ELSE 0 END AS UncostedSalesDollars
FROM VendorBrandKeys k
LEFT JOIN PurchaseSummary p
    ON k.VendorNumber = p.VendorNumber AND k.Brand = p.Brand
LEFT JOIN SalesSummary s
    ON k.VendorNumber = s.VendorNo AND k.Brand = s.Brand
LEFT JOIN PriceSummary pr
    ON k.VendorNumber = pr.VendorNumber AND k.Brand = pr.Brand
LEFT JOIN FreightSummary fs
    ON k.VendorNumber = fs.VendorNumber
LEFT JOIN VendorPurchaseTotal vpt
    ON k.VendorNumber = vpt.VendorNumber
LEFT JOIN BrandVendorCount bvc
    ON k.Brand = bvc.Brand
LEFT JOIN InventoryByBrand ib
    ON k.Brand = ib.Brand
ORDER BY TotalPurchaseDollars DESC, TotalSalesDollars DESC
"""

INVENTORY_POSITION_SQL = """
WITH BeginningInventory AS (
    SELECT Store, Brand, SUM(CAST(onHand AS DECIMAL(28, 4))) AS BeginningUnits,
        SUM(CAST(onHand AS DECIMAL(28, 4)) * CAST(Price AS DECIMAL(28, 4))) AS BeginningRetailValue
    FROM dbo.begin_inventory
    GROUP BY Store, Brand
),
EndingInventory AS (
    SELECT Store, Brand, SUM(CAST(onHand AS DECIMAL(28, 4))) AS EndingUnits,
        SUM(CAST(onHand AS DECIMAL(28, 4)) * CAST(Price AS DECIMAL(28, 4))) AS EndingRetailValue
    FROM dbo.end_inventory
    GROUP BY Store, Brand
)
SELECT
    COALESCE(b.Store, e.Store) AS Store,
    COALESCE(b.Brand, e.Brand) AS Brand,
    b.BeginningUnits,
    e.EndingUnits,
    b.BeginningRetailValue,
    e.EndingRetailValue,
    CASE WHEN b.BeginningUnits IS NOT NULL AND e.EndingUnits IS NOT NULL
        THEN (b.BeginningUnits + e.EndingUnits) / 2.0 END AS AverageInventoryUnits,
    CASE WHEN b.BeginningUnits IS NOT NULL
        THEN CASE WHEN b.BeginningUnits > 0 THEN 1 ELSE 0 END END AS BeginningAvailable,
    CASE WHEN e.EndingUnits IS NOT NULL
        THEN CASE WHEN e.EndingUnits > 0 THEN 1 ELSE 0 END END AS EndingAvailable,
    CASE WHEN b.BeginningUnits IS NOT NULL AND e.EndingUnits IS NOT NULL THEN 1 ELSE 0 END
        AS HasBothSnapshots
FROM BeginningInventory b
FULL OUTER JOIN EndingInventory e
    ON b.Store = e.Store AND b.Brand = e.Brand
"""

PURCHASE_ORDER_SQL = """
WITH PurchaseOrders AS (
    SELECT
        VendorNumber,
        PONumber,
        MAX(LTRIM(RTRIM(VendorName))) AS VendorName,
        MIN(PODate) AS PODate,
        MIN(ReceivingDate) AS FirstReceivingDate,
        MAX(ReceivingDate) AS LastReceivingDate,
        MIN(InvoiceDate) AS InvoiceDate,
        MIN(PayDate) AS PayDate,
        SUM(CAST(Quantity AS DECIMAL(28, 4))) AS PurchaseQuantity,
        SUM(CAST(Dollars AS DECIMAL(28, 4))) AS PurchaseDollars,
        COUNT_BIG(*) AS PurchaseLineCount,
        CASE WHEN MIN(PODate) = MAX(PODate) AND MAX(ReceivingDate) IS NOT NULL
            THEN DATEDIFF(day, MIN(PODate), MAX(ReceivingDate)) END AS ObservedLeadTimeDays
    FROM dbo.purchases
    GROUP BY VendorNumber, PONumber
),
InvoiceOrders AS (
    SELECT VendorNumber, PONumber,
        MAX(LTRIM(RTRIM(VendorName))) AS InvoiceVendorName,
        MIN(PODate) AS InvoicePODate,
        MIN(InvoiceDate) AS InvoiceDate,
        MIN(PayDate) AS InvoicePayDate,
        SUM(CAST(Quantity AS DECIMAL(28, 4))) AS InvoiceQuantity,
        SUM(CAST(Dollars AS DECIMAL(28, 4))) AS InvoiceDollars,
        SUM(CAST(Freight AS DECIMAL(28, 4))) AS Freight
    FROM dbo.vendor_invoice
    GROUP BY VendorNumber, PONumber
)
SELECT
    COALESCE(p.VendorNumber, i.VendorNumber) AS VendorNumber,
    COALESCE(p.PONumber, i.PONumber) AS PONumber,
    COALESCE(p.VendorName, i.InvoiceVendorName) AS VendorName,
    COALESCE(p.PODate, i.InvoicePODate) AS PODate,
    p.FirstReceivingDate,
    p.LastReceivingDate,
    COALESCE(p.InvoiceDate, i.InvoiceDate) AS InvoiceDate,
    COALESCE(p.PayDate, i.InvoicePayDate) AS PayDate,
    p.PurchaseQuantity,
    p.PurchaseDollars,
    p.PurchaseLineCount,
    p.ObservedLeadTimeDays,
    i.InvoiceQuantity,
    i.InvoiceDollars,
    i.Freight,
    CASE
        WHEN p.PONumber IS NULL THEN 'Invoice only'
        WHEN i.PONumber IS NULL THEN 'Purchase only'
        ELSE 'Matched'
    END AS PurchaseOrderMatchStatus,
    CASE WHEN p.PONumber IS NOT NULL AND i.PONumber IS NOT NULL
        THEN i.InvoiceQuantity - p.PurchaseQuantity END AS InvoiceQuantityVariance,
    CASE WHEN p.PONumber IS NOT NULL AND i.PONumber IS NOT NULL
        THEN i.InvoiceDollars - p.PurchaseDollars END AS InvoiceDollarsVariance
FROM PurchaseOrders p
FULL OUTER JOIN InvoiceOrders i
    ON p.VendorNumber = i.VendorNumber AND p.PONumber = i.PONumber
"""


def _run_query(conn, query):
    return pd.read_sql_query(text(query), conn)


def create_vendor_summary(conn):
    summary = _run_query(conn, VENDOR_SUMMARY_SQL)
    key_columns = ["VendorNumber", "Brand"]
    if summary.duplicated(key_columns).any():
        raise ValueError("Vendor summary is not unique at VendorNumber + Brand grain")

    purchase_totals = conn.execute(text(
        "SELECT SUM(CAST(Dollars AS DECIMAL(28, 4))), "
        "SUM(CAST(Quantity AS DECIMAL(28, 4))) FROM dbo.purchases"
    )).one()
    sales_totals = conn.execute(text(
        "SELECT SUM(CAST(SalesDollars AS DECIMAL(28, 4))), "
        "SUM(CAST(SalesQuantity AS DECIMAL(28, 4))) FROM dbo.sales"
    )).one()

    checks = (
        ("TotalPurchaseDollars", purchase_totals[0]),
        ("TotalPurchaseQuantity", purchase_totals[1]),
        ("TotalSalesDollars", sales_totals[0]),
        ("TotalSalesQuantity", sales_totals[1]),
    )
    for column, raw_total in checks:
        summary_total = pd.to_numeric(summary[column], errors="coerce").sum()
        if raw_total is not None and not math.isclose(
            float(summary_total), float(raw_total), rel_tol=0.0, abs_tol=0.01
        ):
            raise ValueError(
                f"Summary {column} does not reconcile to its raw source: "
                f"{summary_total} != {raw_total}"
            )
    logger.info("Vendor-brand summary reconciles to raw purchase and sales totals")
    return summary


def clean_data(df):
    cleaned = df.copy()
    for column in ("VendorName", "Description"):
        cleaned[column] = cleaned[column].astype("string").str.strip()

    numeric_columns = (
        "PurchasePrice", "ActualPrice", "Volume", "TotalPurchaseQuantity",
        "CostCoveredPurchaseQuantity",
        "TotalPurchaseDollars", "TotalSalesDollars", "TotalSalesPrice",
        "TotalSalesQuantity", "TotalExciseTax", "CostCoveredSalesDollars",
        "UncostedSalesDollars", "FreightCost",
        "ReferencePurchasePrice", "PurchasePriceVariance", "AverageInventoryUnits",
        "EndingInventoryUnits", "EndingInventoryRetailValue", "StockTurnover",
        "Unsold Capital",
    )
    for column in numeric_columns:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")

    valid_cost = cleaned["PurchasePrice"].notna() & (cleaned["CostCoveredPurchaseQuantity"] > 0)
    valid_sales = cleaned["TotalSalesDollars"].notna() & (cleaned["TotalSalesQuantity"] > 0)
    gross_profit = cleaned["TotalSalesDollars"] - (
        cleaned["TotalSalesQuantity"] * cleaned["PurchasePrice"]
    )
    cleaned["GrossProfit"] = gross_profit.where(valid_cost & valid_sales)
    cleaned["ProfitMargin"] = (
        cleaned["GrossProfit"] / cleaned["CostCoveredSalesDollars"] * 100
    ).where(cleaned["CostCoveredSalesDollars"] > 0)
    cleaned["SalesToPurchaseRatio"] = (
        cleaned["TotalSalesDollars"] / cleaned["TotalPurchaseDollars"]
    ).where(cleaned["TotalPurchaseDollars"] > 0)
    cleaned["PurchasePriceVariancePct"] = (
        cleaned["PurchasePriceVariance"] / cleaned["ReferencePurchasePrice"] * 100
    ).where(cleaned["ReferencePurchasePrice"] > 0)
    return cleaned


def create_inventory_position(conn):
    return _run_query(conn, INVENTORY_POSITION_SQL)


def create_purchase_order_summary(conn):
    return _run_query(conn, PURCHASE_ORDER_SQL)


def create_power_bi_support_tables(summary):
    vendor_source = summary.copy()
    vendor_source["WeightedPurchaseCost"] = (
        vendor_source["PurchasePrice"] * vendor_source["CostCoveredPurchaseQuantity"]
    )
    vendor_performance = vendor_source.groupby(
        ["VendorNumber", "VendorName"], dropna=False, as_index=False
    ).agg(
        TotalPurchaseQuantity=("TotalPurchaseQuantity", "sum"),
        CostCoveredPurchaseQuantity=("CostCoveredPurchaseQuantity", "sum"),
        TotalPurchaseDollars=("TotalPurchaseDollars", "sum"),
        TotalSalesQuantity=("TotalSalesQuantity", "sum"),
        TotalSalesDollars=("TotalSalesDollars", "sum"),
        CostCoveredSalesDollars=("CostCoveredSalesDollars", lambda values: values.sum(min_count=1)),
        UncostedSalesDollars=("UncostedSalesDollars", "sum"),
        WeightedPurchaseCost=("WeightedPurchaseCost", "sum"),
        EstimatedGrossProfit=("GrossProfit", lambda values: values.sum(min_count=1)),
        AllocatedFreight=("FreightCost", lambda values: values.sum(min_count=1)),
    )
    vendor_performance["WeightedPurchasePrice"] = (
        vendor_performance["WeightedPurchaseCost"]
        / vendor_performance["CostCoveredPurchaseQuantity"]
    ).where(vendor_performance["CostCoveredPurchaseQuantity"] > 0)
    vendor_performance["EstimatedProfitMarginPct"] = (
        vendor_performance["EstimatedGrossProfit"]
        / vendor_performance["CostCoveredSalesDollars"] * 100
    ).where(vendor_performance["CostCoveredSalesDollars"] > 0)
    vendor_performance["SalesToPurchaseRatio"] = (
        vendor_performance["TotalSalesDollars"]
        / vendor_performance["TotalPurchaseDollars"]
    ).where(vendor_performance["TotalPurchaseDollars"] > 0)
    vendor_performance["FreightPerPurchaseDollar"] = (
        vendor_performance["AllocatedFreight"]
        / vendor_performance["TotalPurchaseDollars"]
    ).where(vendor_performance["TotalPurchaseDollars"] > 0)

    total_spend = vendor_performance.loc[
        vendor_performance["TotalPurchaseDollars"] > 0, "TotalPurchaseDollars"
    ].sum()
    total_sales = vendor_performance["TotalSalesDollars"].sum()
    vendor_performance["PurchaseContributionPct"] = (
        vendor_performance["TotalPurchaseDollars"] / total_spend * 100
        if total_spend > 0 else np.nan
    ).where(vendor_performance["TotalPurchaseDollars"] > 0)
    vendor_performance["SalesContributionPct"] = (
        vendor_performance["TotalSalesDollars"] / total_sales * 100
        if total_sales > 0 else np.nan
    )
    vendor_performance = vendor_performance.sort_values(
        "TotalPurchaseDollars", ascending=False
    )
    vendor_performance["VendorSpendRank"] = vendor_performance[
        "TotalPurchaseDollars"
    ].rank(method="min", ascending=False)
    vendor_performance["CumulativePurchaseContributionPct"] = (
        vendor_performance["PurchaseContributionPct"].fillna(0).cumsum()
    )

    eligible_inventory = summary[summary["StockTurnover"].notna()].groupby(
        ["VendorNumber", "VendorName"], dropna=False, as_index=False
    ).agg(
        EligibleInventoryBrandCount=("Brand", "nunique"),
        EligibleSalesUnits=("TotalSalesQuantity", "sum"),
        EligibleAverageInventoryUnits=("AverageInventoryUnits", lambda values: values.sum(min_count=1)),
        EligibleEndingInventoryUnits=("EndingInventoryUnits", lambda values: values.sum(min_count=1)),
        EstimatedEndingInventoryCost=("Unsold Capital", lambda values: values.sum(min_count=1)),
    )
    vendor_performance = vendor_performance.merge(
        eligible_inventory,
        on=["VendorNumber", "VendorName"],
        how="left",
        validate="one_to_one",
    )
    vendor_performance["EligibleInventoryTurnover"] = (
        vendor_performance["EligibleSalesUnits"]
        / vendor_performance["EligibleAverageInventoryUnits"]
    ).where(vendor_performance["EligibleAverageInventoryUnits"] > 0)

    vendor = summary.groupby(["VendorNumber", "VendorName"], dropna=False, as_index=False).agg(
        TotalPurchaseDollars=("TotalPurchaseDollars", "sum"),
        TotalSalesDollars=("TotalSalesDollars", "sum"),
        GrossProfit=("GrossProfit", lambda values: values.sum(min_count=1)),
    )
    vendor = vendor[vendor["TotalPurchaseDollars"] > 0].sort_values(
        "TotalPurchaseDollars", ascending=False
    )
    total_spend = vendor["TotalPurchaseDollars"].sum()
    top_vendors = set(vendor.head(10)["VendorNumber"])
    vendor["Vendor Category"] = vendor.apply(
        lambda row: row["VendorName"] if row["VendorNumber"] in top_vendors else "Others",
        axis=1,
    )
    vendor_purchase = vendor.groupby("Vendor Category", as_index=False).agg(
        TotalPurchaseDollars=("TotalPurchaseDollars", "sum")
    )
    top_ten_contribution = (
        vendor.loc[vendor["VendorNumber"].isin(top_vendors), "TotalPurchaseDollars"].sum()
        / total_spend * 100
        if total_spend > 0 else np.nan
    )
    vendor_purchase["Top10Cont"] = 0.0
    if not vendor_purchase.empty:
        top_row = vendor_purchase["TotalPurchaseDollars"].idxmax()
        vendor_purchase.loc[top_row, "Top10Cont"] = top_ten_contribution

    turnover = summary[summary["StockTurnover"].notna()].groupby(
        ["VendorNumber", "VendorName"], dropna=False, as_index=False
    ).agg(
        SalesUnits=("TotalSalesQuantity", "sum"),
        AverageInventoryUnits=("AverageInventoryUnits", "sum"),
    )
    turnover["AvgStockTurnOver"] = (
        turnover["SalesUnits"] / turnover["AverageInventoryUnits"]
    ).where(turnover["AverageInventoryUnits"] > 0)
    low_turnover = turnover[["VendorNumber", "VendorName", "AvgStockTurnOver"]].rename(
        columns={"VendorName": "VendorName"}
    )

    brand = summary.groupby(["Brand", "Description"], dropna=False, as_index=False).agg(
        TotalSales=("TotalSalesDollars", "sum"),
        CostCoveredSalesDollars=("CostCoveredSalesDollars", lambda values: values.sum(min_count=1)),
        GrossProfit=("GrossProfit", lambda values: values.sum(min_count=1)),
    )
    brand["AvgProfitMargin"] = (
        brand["GrossProfit"] / brand["CostCoveredSalesDollars"] * 100
    ).where(brand["CostCoveredSalesDollars"] > 0)
    sales_threshold = brand.loc[brand["TotalSales"] > 0, "TotalSales"].quantile(0.15)
    margin_threshold = brand["AvgProfitMargin"].quantile(0.85)
    brand["TargetBrand"] = (
        (brand["TotalSales"] <= sales_threshold)
        & (brand["AvgProfitMargin"] >= margin_threshold)
    )
    brand_performance = brand[["Brand", "Description", "TotalSales", "AvgProfitMargin", "TargetBrand"]]

    return {
        "vendor_performance_summary": vendor_performance,
        "Vendor_Purchase_Summary": vendor_purchase,
        "LowTurnoverVendor": low_turnover,
        "BrandPerformance": brand_performance,
    }


def _publish_tables(engine, tables):
    staging_names = {}
    for table_name, frame in tables.items():
        staging_name = f"{table_name}__staging"
        frame.to_sql(
            staging_name,
            con=engine,
            schema="dbo",
            if_exists="replace",
            index=False,
            chunksize=10000,
            method=None,
        )
        staging_names[table_name] = staging_name

    with engine.begin() as connection:
        for table_name, staging_name in staging_names.items():
            connection.execute(text(
                f"IF OBJECT_ID('dbo.{table_name}', 'U') IS NOT NULL DROP TABLE dbo.[{table_name}]"
            ))
            connection.execute(text(f"EXEC sp_rename 'dbo.{staging_name}', '{table_name}'"))


def create_engine_for_inventory():
    url = URL.create(
        "mssql+pyodbc",
        host=os.getenv("SQL_SERVER", r"localhost\SQLEXPRESS"),
        database="inventory",
        query={
            "driver": os.getenv("SQL_DRIVER", "ODBC Driver 17 for SQL Server"),
            "trusted_connection": "yes",
        },
    )
    return create_engine(url, fast_executemany=True, pool_pre_ping=True)


def main():
    engine = create_engine_for_inventory()
    with engine.connect() as conn:
        logger.info("Building vendor-brand analytical summary")
        summary = clean_data(create_vendor_summary(conn))
        inventory_position = create_inventory_position(conn)
        purchase_orders = create_purchase_order_summary(conn)
        support_tables = create_power_bi_support_tables(summary)
        procurement_tables = build_procurement_analytics(
            conn,
            purchase_orders=purchase_orders,
            vendor_performance=support_tables["vendor_performance_summary"],
            vendor_sales_summary=summary,
            inventory_position=inventory_position,
        )

    # Preserve legacy table names for existing Dashboard.pbix mappings, and publish
    # enhanced PO grain under both purchase_order_summary and fact_purchase_order.
    enhanced_po = procurement_tables["fact_purchase_order"]
    tables = {
        "vendor_sales_summary": summary,
        "inventory_position": inventory_position,
        "purchase_order_summary": enhanced_po,
        **support_tables,
        **procurement_tables,
    }
    _publish_tables(engine, tables)
    excel_path = export_procurement_excel(procurement_tables)
    logger.info(
        "Published analytical tables: %s; excel=%s",
        {name: len(frame) for name, frame in tables.items()},
        excel_path,
    )
    engine.dispose()


if __name__ == "__main__":
    main()