import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


SOURCE_FILES = (
    "begin_inventory.csv",
    "end_inventory.csv",
    "purchases.csv",
    "purchase_prices.csv",
    "sales.csv",
    "vendor_invoice.csv",
)


def aggregate_csv(path, chunksize, table_type):
    if table_type == "purchases":
        usecols = [
            "VendorNumber", "Brand", "PONumber", "PODate", "ReceivingDate",
            "Quantity", "Dollars", "PurchasePrice",
        ]
        dtypes = {
            "VendorNumber": "int32", "Brand": "int32", "PONumber": "int32",
            "Quantity": "int32", "Dollars": "float64", "PurchasePrice": "float64",
        }
        keys = ["VendorNumber", "Brand"]
    else:
        usecols = ["VendorNo", "Brand", "SalesQuantity", "SalesDollars"]
        dtypes = {
            "VendorNo": "int32", "Brand": "int32",
            "SalesQuantity": "int32", "SalesDollars": "float64",
        }
        keys = ["VendorNo", "Brand"]

    partials = []
    order_keys = set()
    lead_parts = []
    row_count = 0
    quantity_total = 0
    dollars_total = 0.0

    for chunk in pd.read_csv(
        path,
        usecols=usecols,
        dtype=dtypes,
        chunksize=chunksize,
        low_memory=False,
    ):
        row_count += len(chunk)
        if table_type == "purchases":
            quantity_total += int(chunk["Quantity"].sum())
            dollars_total += float(chunk["Dollars"].sum())
            chunk["CostNumerator"] = chunk["Quantity"] * chunk["PurchasePrice"]
            chunk["CostQuantity"] = chunk["Quantity"].where(chunk["PurchasePrice"].notna(), 0)
            partials.append(chunk.groupby(keys, as_index=False).agg(
                PurchaseQuantity=("Quantity", "sum"),
                PurchaseDollars=("Dollars", "sum"),
                CostNumerator=("CostNumerator", "sum"),
                CostQuantity=("CostQuantity", "sum"),
            ))
            order_keys.update(zip(chunk["VendorNumber"], chunk["PONumber"]))
            dates = chunk[["VendorNumber", "PONumber", "PODate", "ReceivingDate"]].copy()
            dates["PODate"] = pd.to_datetime(dates["PODate"], errors="coerce")
            dates["ReceivingDate"] = pd.to_datetime(dates["ReceivingDate"], errors="coerce")
            lead_parts.append(dates.groupby(["VendorNumber", "PONumber"], as_index=False).agg(
                PODateMin=("PODate", "min"),
                PODateMax=("PODate", "max"),
                FirstReceivingDate=("ReceivingDate", "min"),
                LastReceivingDate=("ReceivingDate", "max"),
            ))
        else:
            quantity_total += int(chunk["SalesQuantity"].sum())
            dollars_total += float(chunk["SalesDollars"].sum())
            partials.append(chunk.groupby(keys, as_index=False).agg(
                SalesQuantity=("SalesQuantity", "sum"),
                SalesDollars=("SalesDollars", "sum"),
            ))

    totals = pd.concat(partials, ignore_index=True).groupby(keys, as_index=False).sum(numeric_only=True)
    if table_type == "purchases":
        totals = totals.rename(columns={"VendorNumber": "Vendor", "Brand": "Brand"})
        orders = pd.concat(lead_parts, ignore_index=True).groupby(
            ["VendorNumber", "PONumber"], as_index=False
        ).agg(
            PODateMin=("PODateMin", "min"),
            PODateMax=("PODateMax", "max"),
            FirstReceivingDate=("FirstReceivingDate", "min"),
            LastReceivingDate=("LastReceivingDate", "max"),
        )
        valid_orders = orders[
            orders["PODateMin"].notna()
            & orders["LastReceivingDate"].notna()
            & orders["PODateMin"].eq(orders["PODateMax"])
        ].copy()
        valid_orders["ObservedLeadTimeDays"] = (
            valid_orders["LastReceivingDate"] - valid_orders["PODateMin"]
        ).dt.days
        return {
            "rows": row_count,
            "quantity": quantity_total,
            "dollars": dollars_total,
            "vendor_brand": totals,
            "order_keys": order_keys,
            "lead_times": valid_orders["ObservedLeadTimeDays"],
        }

    totals = totals.rename(columns={"VendorNo": "Vendor", "Brand": "Brand"})
    return {
        "rows": row_count,
        "quantity": quantity_total,
        "dollars": dollars_total,
        "vendor_brand": totals,
    }


def inspect_inventory(path, chunksize):
    keys = set()
    rows = 0
    for chunk in pd.read_csv(
        path,
        usecols=["Store", "Brand"],
        dtype={"Store": "int32", "Brand": "int32"},
        chunksize=chunksize,
    ):
        rows += len(chunk)
        keys.update(zip(chunk["Store"], chunk["Brand"]))
    return rows, keys


def run_audit(data_dir, chunksize):
    data_dir = Path(data_dir)
    missing = [name for name in SOURCE_FILES if not (data_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing required files in {data_dir}: {missing}")

    purchases = aggregate_csv(data_dir / "purchases.csv", chunksize, "purchases")
    sales = aggregate_csv(data_dir / "sales.csv", chunksize, "sales")
    purchase_keys = set(zip(purchases["vendor_brand"]["Vendor"], purchases["vendor_brand"]["Brand"]))
    sales_keys = set(zip(sales["vendor_brand"]["Vendor"], sales["vendor_brand"]["Brand"]))
    matching_keys = purchase_keys & sales_keys
    combined = purchases["vendor_brand"].merge(
        sales["vendor_brand"], on=["Vendor", "Brand"], how="outer", validate="one_to_one"
    )
    summary_purchase_dollars = float(combined["PurchaseDollars"].sum())
    summary_purchase_quantity = int(combined["PurchaseQuantity"].sum())
    summary_sales_dollars = float(combined["SalesDollars"].sum())
    summary_sales_quantity = int(combined["SalesQuantity"].sum())

    reference = pd.read_csv(
        data_dir / "purchase_prices.csv",
        usecols=["VendorNumber", "Brand"],
        dtype={"VendorNumber": "int32", "Brand": "int32"},
    )
    reference_sizes = reference.groupby(["VendorNumber", "Brand"]).size()
    reference_keys = set(zip(reference["VendorNumber"], reference["Brand"]))
    reference_duplicates = int((reference_sizes > 1).sum())
    reference_match_count = len(purchase_keys & reference_keys)
    brand_vendor_counts = purchases["vendor_brand"].groupby("Brand")["Vendor"].nunique()
    multi_vendor_brands = int((brand_vendor_counts > 1).sum())

    invoice = pd.read_csv(
        data_dir / "vendor_invoice.csv",
        usecols=["VendorNumber", "PONumber"],
        dtype={"VendorNumber": "int32", "PONumber": "int32"},
    )
    invoice_keys = set(zip(invoice["VendorNumber"], invoice["PONumber"]))
    invoice_po_duplicates = int(invoice.duplicated(["VendorNumber", "PONumber"]).sum())
    matched_orders = len(purchases["order_keys"] & invoice_keys)

    begin_rows, begin_keys = inspect_inventory(data_dir / "begin_inventory.csv", chunksize)
    end_rows, end_keys = inspect_inventory(data_dir / "end_inventory.csv", chunksize)
    both_keys = begin_keys & end_keys
    all_inventory_keys = begin_keys | end_keys
    all_positions_per_brand = pd.Series(
        [brand for _, brand in all_inventory_keys], dtype="int32"
    ).value_counts()
    complete_positions_per_brand = pd.Series(
        [brand for _, brand in both_keys], dtype="int32"
    ).value_counts()
    complete_brands = set(
        all_positions_per_brand.index[
            all_positions_per_brand.eq(
                complete_positions_per_brand.reindex(all_positions_per_brand.index).fillna(0)
            )
        ]
    )
    single_vendor_brands = set(brand_vendor_counts[brand_vendor_counts == 1].index)
    eligible_inventory_brands = complete_brands & single_vendor_brands

    eligible_lead_times = purchases["lead_times"].dropna()
    matched_sales_revenue = float(
        sales["vendor_brand"].merge(
            purchases["vendor_brand"][["Vendor", "Brand"]],
            on=["Vendor", "Brand"],
            how="inner",
            validate="one_to_one",
        )["SalesDollars"].sum()
    )

    checks = {
        "purchase_dollars_reconcile": bool(np.isclose(
            summary_purchase_dollars, purchases["dollars"], rtol=1e-10, atol=0.01
        )),
        "purchase_quantity_reconcile": summary_purchase_quantity == purchases["quantity"],
        "sales_dollars_reconcile": bool(np.isclose(
            summary_sales_dollars, sales["dollars"], rtol=1e-10, atol=0.01
        )),
        "sales_quantity_reconcile": summary_sales_quantity == sales["quantity"],
        "vendor_brand_summary_unique": not combined.duplicated(["Vendor", "Brand"]).any(),
    }
    return {
        "data_dir": str(data_dir.resolve()),
        "chunk_rows": chunksize,
        "purchase_rows": purchases["rows"],
        "purchase_dollars": round(purchases["dollars"], 2),
        "purchase_quantity": purchases["quantity"],
        "purchase_vendor_brand_keys": len(purchase_keys),
        "sales_rows": sales["rows"],
        "sales_dollars": round(sales["dollars"], 2),
        "sales_quantity": sales["quantity"],
        "sales_vendor_brand_keys": len(sales_keys),
        "sales_vendor_brand_keys_with_purchases": len(matching_keys),
        "sales_revenue_on_keys_with_purchases": round(matched_sales_revenue, 2),
        "reference_vendor_brand_rows": len(reference),
        "duplicate_reference_vendor_brand_keys": reference_duplicates,
        "purchase_vendor_brand_reference_matches": reference_match_count,
        "brands_with_multiple_purchasing_vendors": multi_vendor_brands,
        "invoice_rows": len(invoice),
        "duplicate_vendor_po_invoice_keys": invoice_po_duplicates,
        "purchase_orders_with_matching_invoice": matched_orders,
        "purchase_order_keys": len(purchases["order_keys"]),
        "begin_inventory_rows": begin_rows,
        "end_inventory_rows": end_rows,
        "begin_store_brand_keys": len(begin_keys),
        "end_store_brand_keys": len(end_keys),
        "store_brand_keys_in_both_snapshots": len(both_keys),
        "complete_snapshot_single_vendor_brands": len(eligible_inventory_brands),
        "observed_lead_time_orders": int(len(eligible_lead_times)),
        "observed_lead_time_median_days": float(eligible_lead_times.median()) if len(eligible_lead_times) else None,
        "observed_lead_time_std_days": float(eligible_lead_times.std()) if len(eligible_lead_times) > 1 else None,
        "raw_total_reconciliation": checks,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Audit vendor-analysis CSV relationships and totals using bounded chunks."
    )
    parser.add_argument("--data-dir", default=Path(__file__).resolve().parent / "data", type=Path)
    parser.add_argument("--chunk-size", default=50000, type=int)
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be greater than zero")
    print(json.dumps(run_audit(args.data_dir, args.chunk_size), indent=2))


if __name__ == "__main__":
    main()
