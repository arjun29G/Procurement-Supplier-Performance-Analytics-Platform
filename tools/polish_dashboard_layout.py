"""Final polish: rename visual titles + clarify PO/reconciliation terminology in Dashboard.pbix.

Does NOT rebuild pages, change data, or replace visuals — Layout metadata only.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PBIX = PROJECT / "Dashboard.pbix"
BACKUP = PROJECT / "Dashboard_BeforePolish.pbix"


def _lit(text: str) -> dict:
    return {"expr": {"Literal": {"Value": f"'{text}'"}}}


def set_title(sv: dict, text: str) -> None:
    objs = sv.setdefault("objects", {})
    objs["title"] = [{
        "properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": _lit(text),
        }
    }]


def set_card_label(sv: dict, query_ref: str, label: str) -> None:
    sv.setdefault("columnProperties", {})[query_ref] = {"displayName": label}
    pq = sv.get("prototypeQuery") or {}
    for sel in pq.get("Select", []) or []:
        if sel.get("Name") == query_ref or query_ref in str(sel.get("Name", "")):
            sel["NativeReferenceName"] = label


def set_select_natives(sv: dict, mapping: dict[str, str]) -> None:
    """Map Select Name (or Property) -> NativeReferenceName / columnProperties displayName."""
    pq = sv.get("prototypeQuery") or {}
    colprops = sv.setdefault("columnProperties", {})
    for sel in pq.get("Select", []) or []:
        name = sel.get("Name") or ""
        prop = None
        if "Column" in sel:
            prop = sel["Column"].get("Property")
        elif "Aggregation" in sel:
            prop = sel["Aggregation"]["Expression"]["Column"].get("Property")
        new = None
        if name in mapping:
            new = mapping[name]
        elif prop and prop in mapping:
            new = mapping[prop]
        if new:
            sel["NativeReferenceName"] = new
            colprops[name] = {"displayName": new}


def set_textbox(sv: dict, title: str, body: str) -> None:
    objs = sv.setdefault("objects", {})
    objs["general"] = [{
        "properties": {
            "paragraphs": [
                {"textRuns": [{"value": title, "textStyle": {"fontWeight": "bold", "fontSize": "12pt"}}]},
                {"textRuns": [{"value": body, "textStyle": {"fontSize": "10pt"}}]},
            ]
        }
    }]


def filter_card_metric(sv: dict, metric_value: str, label: str) -> None:
    """Restrict a card on chart_po_key_metrics to one Metric row."""
    pq = sv.setdefault("prototypeQuery", {})
    pq["Where"] = [{
        "Condition": {
            "Comparison": {
                "ComparisonKind": 0,
                "Left": {
                    "Column": {
                        "Expression": {"SourceRef": {"Source": "t"}},
                        "Property": "Metric",
                    }
                },
                "Right": {"Literal": {"Value": f"'{metric_value}'"}},
            }
        }
    }]
    # Value aggregation select
    for sel in pq.get("Select", []) or []:
        sel["NativeReferenceName"] = label
        if sel.get("Name"):
            sv.setdefault("columnProperties", {})[sel["Name"]] = {"displayName": label}


def polish(layout: dict) -> list[str]:
    changes: list[str] = []
    sections = layout["sections"]

    # --- Page 1 Executive Overview ---
    p = sections[0]["visualContainers"]
    cfg0 = json.loads(p[0]["config"])
    set_textbox(
        cfg0["singleVisual"],
        "OTIF — Not Supported",
        "True OTIF requires promised delivery date and actual received quantity/date fields, "
        "which are not available in the source dataset. "
        "Purchase Orders (purchase-side keys) = 5,543. "
        "Reconciliation Keys (full outer join purchases↔invoices) = 6,879. "
        "Matched POs = 4,207. Delivery metrics use invoice reconciliation — not OTIF.",
    )
    p[0]["config"] = json.dumps(cfg0, separators=(",", ":"))
    changes.append("P1 textbox: clarified Purchase Orders vs Reconciliation Keys; OTIF preserved")

    cfg1 = json.loads(p[1]["config"])
    set_card_label(cfg1["singleVisual"], "Sum(chart_top_suppliers.TotalPurchaseDollars)", "Procurement Spend")
    p[1]["config"] = json.dumps(cfg1, separators=(",", ":"))
    changes.append("P1 card: Top Suppliers Spend → Procurement Spend")

    cfg2 = json.loads(p[2]["config"])
    filter_card_metric(cfg2["singleVisual"], "PurchaseSidePOKeys", "Purchase Orders")
    p[2]["config"] = json.dumps(cfg2, separators=(",", ":"))
    changes.append("P1 card: PO Key Metrics → Purchase Orders (filtered to 5,543 purchase-side keys)")

    renames_p1 = {
        3: ("Monthly Procurement Spend", {"PurchaseSpend": "Procurement Spend", "YearMonth": "YearMonth"}),
        4: ("Procurement Spend by Supplier", {"TotalPurchaseDollars": "Procurement Spend", "VendorName": "Supplier"}),
        5: ("Supplier Performance Status", {"SupplierCount": "Supplier Count", "PerformanceStatus": "Performance Status"}),
        6: ("PO Cycle-Time Aging", {"POCount": "PO Count", "POCycleAgingBucket": "Cycle-Time Bucket"}),
        7: ("PO Reconciliation Status", {"POCount": "Reconciliation Keys", "PurchaseOrderMatchStatus": "Match Status"}),
    }
    for idx, (title, fmap) in renames_p1.items():
        cfg = json.loads(p[idx]["config"])
        set_title(cfg["singleVisual"], title)
        set_select_natives(cfg["singleVisual"], fmap)
        p[idx]["config"] = json.dumps(cfg, separators=(",", ":"))
        changes.append(f"P1 visual {idx}: title → {title}")

    # --- Page 2 Procurement Spend ---
    p = sections[1]["visualContainers"]
    cfg = json.loads(p[0]["config"])
    set_card_label(cfg["singleVisual"], "Sum(chart_monthly_spend.PurchaseSpend)", "Total Procurement Spend")
    p[0]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P2 card: Monthly Spend Total → Total Procurement Spend")

    cfg = json.loads(p[1]["config"])
    set_card_label(cfg["singleVisual"], "Sum(chart_monthly_spend.PurchaseQuantity)", "Purchase Quantity")
    p[1]["config"] = json.dumps(cfg, separators=(",", ":"))

    for idx, title, fmap in [
        (2, "Monthly Procurement Spend", {"PurchaseSpend": "Procurement Spend", "YearMonth": "YearMonth"}),
        (3, "Procurement Spend by Supplier", {"TotalPurchaseDollars": "Procurement Spend", "VendorName": "Supplier"}),
        (4, "Procurement Spend by Brand", {"PurchaseSpend": "Procurement Spend", "BrandName": "Brand"}),
        (5, "Supplier Procurement Contribution", {"PurchaseContributionPct": "Contribution %", "VendorName": "Supplier"}),
    ]:
        cfg = json.loads(p[idx]["config"])
        set_title(cfg["singleVisual"], title)
        set_select_natives(cfg["singleVisual"], fmap)
        p[idx]["config"] = json.dumps(cfg, separators=(",", ":"))
        changes.append(f"P2 visual {idx}: title → {title}")

    # --- Page 3 PO Cycle-Time Aging ---
    p = sections[2]["visualContainers"]
    cfg = json.loads(p[0]["config"])
    set_textbox(
        cfg["singleVisual"],
        "Completed PO Cycle-Time",
        "Not open-PO aging. Basis: LastReceivingDate − PODate. "
        "Observed lead-time orders = 5,543 (purchase-side). Median lead time = 10 days. "
        "Average ≈ 9.93 days; StdDev ≈ 2.13 days.",
    )
    p[0]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P3 textbox: clarified completed cycle-time methodology")

    for idx, title, fmap in [
        (1, "PO Cycle-Time Distribution", {"POCount": "PO Count", "POCycleAgingBucket": "Cycle-Time Bucket"}),
        (2, "Median Lead Time by Supplier", {"MedianLeadTimeDays": "Median Lead Time (Days)", "VendorName": "Supplier"}),
    ]:
        cfg = json.loads(p[idx]["config"])
        set_title(cfg["singleVisual"], title)
        set_select_natives(cfg["singleVisual"], fmap)
        p[idx]["config"] = json.dumps(cfg, separators=(",", ":"))
        changes.append(f"P3 visual {idx}: title → {title}")

    cfg = json.loads(p[3]["config"])
    set_title(cfg["singleVisual"], "Supplier Cycle-Time Detail")
    set_select_natives(cfg["singleVisual"], {
        "VendorName": "Supplier",
        "ObservedLeadTimeOrderCount": "Observed LT Orders",
        "AverageLeadTimeDays": "Avg Lead Time (Days)",
        "MedianLeadTimeDays": "Median Lead Time (Days)",
        "LeadTimeStdDevDays": "Lead-Time Std Dev",
        "LeadTimeVarianceDays": "Lead-Time Variance",
    })
    p[3]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P3 table: title → Supplier Cycle-Time Detail")

    # --- Page 4 Delivery Reconciliation ---
    p = sections[3]["visualContainers"]
    cfg = json.loads(p[0]["config"])
    set_textbox(
        cfg["singleVisual"],
        "OTIF — Not Supported",
        "True OTIF requires promised delivery date and actual received quantity/date fields, "
        "which are not available in the source dataset. "
        "Purchase Orders = 5,543 | Reconciliation Keys = 6,879 | Matched POs = 4,207 | "
        "Purchase only = 1,336 | Invoice only = 1,336. "
        "Invoice match / quantity / dollar alignment are reconciliation metrics — not OTIF.",
    )
    p[0]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P4 textbox: Purchase Orders vs Reconciliation Keys terminology")

    for idx, title, fmap in [
        (1, "PO Reconciliation Status", {"POCount": "Reconciliation Keys", "PurchaseOrderMatchStatus": "Match Status"}),
        (2, "Invoice Match Rate by Supplier", {"InvoiceMatchRatePct": "Invoice Match Rate %", "VendorName": "Supplier"}),
    ]:
        cfg = json.loads(p[idx]["config"])
        set_title(cfg["singleVisual"], title)
        set_select_natives(cfg["singleVisual"], fmap)
        p[idx]["config"] = json.dumps(cfg, separators=(",", ":"))
        changes.append(f"P4 visual {idx}: title → {title}")

    cfg = json.loads(p[3]["config"])
    set_title(cfg["singleVisual"], "Supplier Delivery Reconciliation Detail")
    set_select_natives(cfg["singleVisual"], {
        "VendorName": "Supplier",
        "MatchedPOCount": "Matched POs",
        "PurchaseOrderKeyCount": "Purchase Orders",
        "InvoiceMatchRatePct": "Invoice Match Rate %",
        "QuantityAlignmentRatePct": "Quantity Alignment %",
        "DollarAlignmentRatePct": "Dollar Alignment %",
        "TotalPurchaseDollars": "Procurement Spend",
    })
    p[3]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P4 table: title → Supplier Delivery Reconciliation Detail; PO key column → Purchase Orders")

    # --- Page 5 Lead-Time Analytics ---
    p = sections[4]["visualContainers"]
    for idx, title, fmap in [
        (0, "Lead-Time Distribution", {"POCount": "PO Count", "LeadTimeDays": "Lead Time (Days)"}),
        (1, "Average Lead Time by Month", {"AvgLeadTimeDays": "Avg Lead Time (Days)", "YearMonth": "YearMonth"}),
        (2, "Lead-Time Variability by Supplier", {"LeadTimeStdDevDays": "Lead-Time Std Dev", "VendorName": "Supplier"}),
    ]:
        cfg = json.loads(p[idx]["config"])
        set_title(cfg["singleVisual"], title)
        set_select_natives(cfg["singleVisual"], fmap)
        p[idx]["config"] = json.dumps(cfg, separators=(",", ":"))
        changes.append(f"P5 visual {idx}: title → {title}")

    cfg = json.loads(p[3]["config"])
    set_title(cfg["singleVisual"], "Supplier Lead-Time Detail")
    set_select_natives(cfg["singleVisual"], {
        "VendorName": "Supplier",
        "ObservedLeadTimeOrderCount": "Observed LT Orders",
        "AverageLeadTimeDays": "Avg Lead Time (Days)",
        "MedianLeadTimeDays": "Median Lead Time (Days)",
        "LeadTimeStdDevDays": "Lead-Time Std Dev",
        "LeadTimeVarianceDays": "Lead-Time Variance",
    })
    p[3]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P5 table: title → Supplier Lead-Time Detail")

    # --- Page 6 Inventory ---
    p = sections[5]["visualContainers"]
    cfg = json.loads(p[0]["config"])
    set_textbox(
        cfg["singleVisual"],
        "Inventory Availability",
        "Vendor-linked inventory is scored only for the supported single-vendor complete-snapshot brand population.",
    )
    p[0]["config"] = json.dumps(cfg, separators=(",", ":"))

    for idx, title, fmap in [
        (1, "Ending Inventory by Store", {"EndingUnits": "Ending Units", "Store": "Store"}),
        (2, "Beginning Inventory by Store", {"BeginningUnits": "Beginning Units", "Store": "Store"}),
        (3, "Ending Inventory Value by Brand", {"EndingRetailValue": "Ending Retail Value", "Brand": "Brand"}),
        (4, "Low-Inventory Flags by Brand", {"LowInventoryFlag": "Low-Inventory Flags", "Brand": "Brand"}),
    ]:
        cfg = json.loads(p[idx]["config"])
        set_title(cfg["singleVisual"], title)
        set_select_natives(cfg["singleVisual"], fmap)
        p[idx]["config"] = json.dumps(cfg, separators=(",", ":"))
        changes.append(f"P6 visual {idx}: title → {title}")

    # --- Page 7 Supplier Detail ---
    p = sections[6]["visualContainers"]
    cfg = json.loads(p[2]["config"])
    set_title(cfg["singleVisual"], "Supplier Procurement Spend")
    set_select_natives(cfg["singleVisual"], {"TotalPurchaseDollars": "Procurement Spend", "VendorName": "Supplier"})
    p[2]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P7 chart: Supplier Procurement Spend")

    cfg = json.loads(p[3]["config"])
    set_title(cfg["singleVisual"], "Supplier Performance Detail")
    set_select_natives(cfg["singleVisual"], {
        "VendorName": "Supplier",
        "PurchaseOrderKeyCount": "Purchase Orders",
        "TotalPurchaseDollars": "Procurement Spend",
        "TotalPurchaseQuantity": "Purchase Quantity",
        "AverageLeadTimeDays": "Avg Lead Time (Days)",
        "MedianLeadTimeDays": "Median Lead Time (Days)",
        "LeadTimeStdDevDays": "Lead-Time Std Dev",
        "LeadTimeVarianceDays": "Lead-Time Variance",
        "InvoiceMatchRatePct": "Invoice Match Rate %",
        "QuantityAlignmentRatePct": "Quantity Alignment %",
        "DollarAlignmentRatePct": "Dollar Alignment %",
        "AbsoluteAvgPriceVariancePct": "Abs Avg Price Variance %",
        "EstimatedEndingInventoryCost": "Est. Ending Inventory Cost",
        "PerformanceStatus": "Performance Status",
    })
    p[3]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P7 table: Supplier Performance Detail; PO key → Purchase Orders")

    # --- Page 8 Planning & Risk ---
    p = sections[7]["visualContainers"]
    for idx, title, fmap in [
        (0, "Supplier Risk Flags", {"RiskFlagCount": "Risk Flags", "VendorName": "Supplier"}),
        (1, "Procurement Spend by Supplier", {"TotalPurchaseDollars": "Procurement Spend", "VendorName": "Supplier"}),
        (2, "Median Lead Time by Supplier", {"MedianLeadTimeDays": "Median Lead Time (Days)", "VendorName": "Supplier"}),
    ]:
        cfg = json.loads(p[idx]["config"])
        set_title(cfg["singleVisual"], title)
        set_select_natives(cfg["singleVisual"], fmap)
        p[idx]["config"] = json.dumps(cfg, separators=(",", ":"))
        changes.append(f"P8 visual {idx}: title → {title}")

    cfg = json.loads(p[3]["config"])
    set_title(cfg["singleVisual"], "Supplier Risk & Planning Detail")
    set_select_natives(cfg["singleVisual"], {
        "VendorName": "Supplier",
        "TotalPurchaseDollars": "Procurement Spend",
        "PurchaseContributionPct": "Contribution %",
        "MedianLeadTimeDays": "Median Lead Time (Days)",
        "LeadTimeStdDevDays": "Lead-Time Std Dev",
        "InvoiceMatchRatePct": "Invoice Match Rate %",
        "AbsoluteAvgPriceVariancePct": "Abs Avg Price Variance %",
        "HighCycleTimePOCount": "High Cycle-Time POs",
        "RiskFlagCount": "Risk Flags",
        "PerformanceStatus": "Performance Status",
        "RecommendedAttention": "Recommended Attention",
    })
    p[3]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P8 table: Supplier Risk & Planning Detail")

    # --- Page 9 Legacy: only clear technical field titles, keep page intact ---
    p = sections[8]["visualContainers"]
    cfg = json.loads(p[5]["config"])
    set_title(cfg["singleVisual"], "Purchase Contribution by Vendor Category")
    set_select_natives(cfg["singleVisual"], {
        "TotalPurchaseDollars": "Total Purchase ($)",
        "Vendor Category": "Vendor Category",
        "Sum of TotalPurchaseDollars": "Total Purchase ($)",
    })
    p[5]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P9 donut: removed technical 'Sum of TotalPurchaseDollars' label")

    cfg = json.loads(p[7]["config"])
    set_title(cfg["singleVisual"], "Top Brands by Sales")
    p[7]["config"] = json.dumps(cfg, separators=(",", ":"))

    cfg = json.loads(p[8]["config"])
    set_title(cfg["singleVisual"], "Top Vendors by Sales")
    p[8]["config"] = json.dumps(cfg, separators=(",", ":"))

    cfg = json.loads(p[9]["config"])
    set_title(cfg["singleVisual"], "Low Performing Vendors (Stock Turnover)")
    set_select_natives(cfg["singleVisual"], {
        "AvgStockTurnOver": "Avg Stock Turnover",
        "Sum of AvgStockTurnOver": "Avg Stock Turnover",
        "VendorName": "Vendor",
    })
    p[9]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P9 funnel: removed technical 'Sum of AvgStockTurnOver' label")

    cfg = json.loads(p[10]["config"])
    set_title(cfg["singleVisual"], "Low Performing Brands")
    p[10]["config"] = json.dumps(cfg, separators=(",", ":"))
    changes.append("P9: minor business titles on charts; KPIs/layout preserved")

    return changes


def write_pbix(layout: dict) -> None:
    layout_bytes = json.dumps(layout, ensure_ascii=False, separators=(",", ":")).encode("utf-16-le")
    shutil.copy2(PBIX, BACKUP)
    tmp = PROJECT / "_Dashboard_polish_tmp.pbix"
    with zipfile.ZipFile(PBIX, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            compress = info.compress_type
            if info.filename == "Report/Layout":
                data = layout_bytes
                compress = zipfile.ZIP_DEFLATED
            elif info.filename == "DataModel":
                compress = zipfile.ZIP_STORED
            new_info = zipfile.ZipInfo(filename=info.filename)
            new_info.compress_type = compress
            new_info.date_time = info.date_time
            zout.writestr(new_info, data)
    tmp.replace(PBIX)


def main() -> None:
    with zipfile.ZipFile(PBIX) as z:
        layout = json.loads(z.read("Report/Layout").decode("utf-16-le"))

    # Fix botched helper call leftover — polish() starts clean
    changes = polish(layout)
    write_pbix(layout)
    print(json.dumps({
        "output": str(PBIX),
        "backup": str(BACKUP),
        "pages": [s.get("displayName") for s in layout["sections"]],
        "changes": changes,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
