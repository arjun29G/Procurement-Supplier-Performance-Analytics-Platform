"""Export key analytical tables to CSV for Power BI Desktop refresh/import."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

PROJECT_DIR = Path(__file__).resolve().parent
OUT_DIR = PROJECT_DIR / "exports" / "powerbi"

TABLES = (
    "vendor_sales_summary",
    "vendor_performance_summary",
    "Vendor_Purchase_Summary",
    "LowTurnoverVendor",
    "BrandPerformance",
    "purchase_order_summary",
    "inventory_position",
    "dim_supplier",
    "dim_brand",
    "dim_store",
    "dim_date",
    "fact_purchase_order",
    "fact_lead_time",
    "supplier_lead_time_summary",
    "brand_lead_time_summary",
    "fact_delivery_reconciliation",
    "fact_supplier_performance",
    "procurement_planning_risk",
    "po_cycle_aging_summary",
    "fact_procurement_monthly",
    "fact_inventory_summary",
)


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
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine_for_inventory()
    written = []
    with engine.connect() as conn:
        for table in TABLES:
            exists = conn.execute(text(
                "SELECT 1 FROM sys.tables WHERE schema_id = SCHEMA_ID('dbo') AND name = :n"
            ), {"n": table}).scalar()
            if not exists:
                continue
            frame = pd.read_sql_query(text(f"SELECT * FROM dbo.[{table}]"), conn)
            path = OUT_DIR / f"{table}.csv"
            frame.to_csv(path, index=False)
            written.append((table, len(frame), str(path)))
    engine.dispose()
    for table, rows, path in written:
        print(f"{table}: {rows} rows -> {path}")


if __name__ == "__main__":
    main()
