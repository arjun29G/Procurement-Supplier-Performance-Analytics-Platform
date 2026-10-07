"""Inject classic Power BI Layout chart visuals into Dashboard.pbix.

Uses the same visual config schema as Dashboard_Legacy.pbix (prototypeQuery).
Requires the target PBIX DataModel to contain the referenced tables.

If Dashboard.pbix still has the legacy 4-table model, this script ALSO writes
a companion PBIX-ready Layout JSON and a one-click opener for the interactive
PBIP (which already contains the 26-table model + measures in Desktop).
"""

from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PBIX = PROJECT / "Dashboard.pbix"
LEGACY = PROJECT / "Dashboard_Legacy.pbix"
LAYOUT_OUT = PROJECT / "powerbi" / "classic_layout_charts.json"


def uid() -> str:
    return uuid.uuid4().hex[:20]


def visual_container(config: dict, x, y, w, h, z) -> dict:
    cfg = dict(config)
    cfg["name"] = uid()
    cfg["layouts"] = [{"id": 0, "position": {
        "x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z
    }}]
    return {
        "x": x, "y": y, "z": z, "width": w, "height": h,
        "config": json.dumps(cfg, separators=(",", ":")),
        "filters": "[]",
    }


def textbox(title: str, body: str, x, y, w, h, z) -> dict:
    config = {
        "singleVisual": {
            "visualType": "textbox",
            "objects": {
                "general": [{
                    "properties": {
                        "paragraphs": [
                            {"textRuns": [{"value": title, "textStyle": {"fontWeight": "bold", "fontSize": "12pt"}}]},
                            {"textRuns": [{"value": body, "textStyle": {"fontSize": "10pt"}}]},
                        ]
                    }
                }]
            },
            "drillFilterOtherVisuals": True,
        }
    }
    return visual_container(config, x, y, w, h, z)


def card(entity: str, prop: str, x, y, w, h, z, label: str) -> dict:
    qref = f"Sum({entity}.{prop})"
    config = {
        "singleVisual": {
            "visualType": "card",
            "projections": {"Values": [{"queryRef": qref}]},
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                "Select": [{
                    "Aggregation": {
                        "Expression": {"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}},
                        "Function": 0,
                    },
                    "Name": qref,
                    "NativeReferenceName": label,
                }],
            },
            "columnProperties": {qref: {"displayName": label}},
            "drillFilterOtherVisuals": True,
        }
    }
    return visual_container(config, x, y, w, h, z)


def chart(vtype: str, entity: str, cat: str, val: str, x, y, w, h, z, title: str) -> dict:
    cat_ref = f"{entity}.{cat}"
    val_ref = f"Sum({entity}.{val})"
    config = {
        "singleVisual": {
            "visualType": vtype,
            "projections": {
                "Category": [{"queryRef": cat_ref, "active": True}],
                "Y": [{"queryRef": val_ref}],
            },
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                "Select": [
                    {
                        "Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": cat},
                        "Name": cat_ref,
                        "NativeReferenceName": cat,
                    },
                    {
                        "Aggregation": {
                            "Expression": {"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": val}},
                            "Function": 0,
                        },
                        "Name": val_ref,
                        "NativeReferenceName": title,
                    },
                ],
                "OrderBy": [{
                    "Direction": 2,
                    "Expression": {
                        "Aggregation": {
                            "Expression": {"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": val}},
                            "Function": 0,
                        }
                    },
                }],
            },
            "drillFilterOtherVisuals": True,
            "hasDefaultSort": True,
            "objects": {
                "title": [{
                    "properties": {
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                        "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
                    }
                }]
            },
        }
    }
    return visual_container(config, x, y, w, h, z)


def table(entity: str, cols: list[str], x, y, w, h, z) -> dict:
    selects = []
    projections = []
    for c in cols:
        ref = f"{entity}.{c}"
        projections.append({"queryRef": ref})
        selects.append({
            "Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": c},
            "Name": ref,
            "NativeReferenceName": c,
        })
    config = {
        "singleVisual": {
            "visualType": "tableEx",
            "projections": {"Values": projections},
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                "Select": selects,
            },
            "drillFilterOtherVisuals": True,
        }
    }
    return visual_container(config, x, y, w, h, z)


def slicer(entity: str, prop: str, x, y, w, h, z) -> dict:
    ref = f"{entity}.{prop}"
    config = {
        "singleVisual": {
            "visualType": "slicer",
            "projections": {"Values": [{"queryRef": ref, "active": True}]},
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "t", "Entity": entity, "Type": 0}],
                "Select": [{
                    "Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop},
                    "Name": ref,
                    "NativeReferenceName": prop,
                }],
            },
            "drillFilterOtherVisuals": True,
        }
    }
    return visual_container(config, x, y, w, h, z)


def build_sections(legacy_section: dict | None) -> list[dict]:
    pages = []

    def page(name: str, ordinal: int, visuals: list[dict]) -> dict:
        return {
            "id": ordinal,
            "name": uid(),
            "displayName": name,
            "filters": "[]",
            "ordinal": ordinal,
            "visualContainers": visuals,
            "width": 1280,
            "height": 720,
            "displayOption": 1,
        }

    pages.append(page("1. Executive Overview", 0, [
        textbox("OTIF — Not Supported", "Delivery metrics use invoice reconciliation. Purchase-Side PO Count = 5,543. Reconciliation Keys (full outer join purchases↔invoices) = 6,879.", 20, 8, 1240, 50, 0),
        card("chart_top_suppliers", "TotalPurchaseDollars", 20, 70, 200, 80, 1, "Top Suppliers Spend"),
        card("chart_po_key_metrics", "Value", 240, 70, 180, 80, 2, "PO Key Metrics"),
        chart("lineChart", "chart_monthly_spend", "YearMonth", "PurchaseSpend", 20, 170, 620, 250, 3, "Procurement Spend by Month"),
        chart("barChart", "chart_top_suppliers", "VendorName", "TotalPurchaseDollars", 660, 170, 600, 250, 4, "Top Suppliers by Spend"),
        chart("donutChart", "chart_performance_status", "PerformanceStatus", "SupplierCount", 20, 440, 400, 260, 5, "Supplier Performance Status"),
        chart("columnChart", "chart_aging_buckets", "POCycleAgingBucket", "POCount", 440, 440, 400, 260, 6, "PO Cycle-Time Buckets"),
        chart("barChart", "chart_match_status", "PurchaseOrderMatchStatus", "POCount", 860, 440, 400, 260, 7, "Delivery Reconciliation Status"),
    ]))

    pages.append(page("2. Procurement Spend", 1, [
        card("chart_monthly_spend", "PurchaseSpend", 20, 20, 240, 80, 0, "Monthly Spend Total"),
        card("chart_monthly_spend", "PurchaseQuantity", 280, 20, 220, 80, 1, "Purchase Quantity"),
        chart("lineChart", "chart_monthly_spend", "YearMonth", "PurchaseSpend", 20, 120, 620, 280, 2, "Monthly Procurement Spend"),
        chart("barChart", "chart_top_suppliers", "VendorName", "TotalPurchaseDollars", 660, 120, 600, 280, 3, "Top Suppliers by Procurement Spend"),
        chart("barChart", "chart_brand_spend", "BrandName", "PurchaseSpend", 20, 420, 620, 280, 4, "Spend by Brand"),
        chart("barChart", "chart_top_suppliers", "VendorName", "PurchaseContributionPct", 660, 420, 600, 280, 5, "Supplier Contribution %"),
    ]))

    pages.append(page("3. PO Cycle-Time Aging", 2, [
        textbox("Completed PO Cycle-Time", "Not open-PO aging. Basis: LastReceivingDate − PODate. Observed LT orders = 5,543; median = 10 days.", 20, 8, 1240, 50, 0),
        chart("columnChart", "chart_aging_buckets", "POCycleAgingBucket", "POCount", 20, 70, 620, 300, 1, "PO Count by Cycle-Time Bucket"),
        chart("barChart", "chart_supplier_lead_time", "VendorName", "MedianLeadTimeDays", 660, 70, 600, 300, 2, "Supplier Cycle-Time Ranking"),
        table("chart_supplier_lead_time", [
            "VendorName", "ObservedLeadTimeOrderCount", "AverageLeadTimeDays", "MedianLeadTimeDays",
            "LeadTimeStdDevDays", "LeadTimeVarianceDays",
        ], 20, 390, 1240, 310, 3),
    ]))

    pages.append(page("4. Delivery Reconciliation", 3, [
        textbox("Supplier Delivery Reconciliation", "True OTIF requires promised delivery date and actual received quantity/date fields, which are not available in the source dataset. OTIF — Not Supported.", 20, 8, 1240, 55, 0),
        chart("donutChart", "chart_match_status", "PurchaseOrderMatchStatus", "POCount", 20, 80, 420, 300, 1, "Matched vs Unmatched POs"),
        chart("barChart", "chart_top_suppliers", "VendorName", "InvoiceMatchRatePct", 460, 80, 800, 300, 2, "Supplier Invoice Match %"),
        table("chart_top_suppliers", [
            "VendorName", "MatchedPOCount", "PurchaseOrderKeyCount", "InvoiceMatchRatePct",
            "QuantityAlignmentRatePct", "DollarAlignmentRatePct", "TotalPurchaseDollars",
        ], 20, 400, 1240, 300, 3),
    ]))

    pages.append(page("5. Lead-Time Analytics", 4, [
        chart("columnChart", "chart_lead_time_hist", "LeadTimeDays", "POCount", 20, 20, 620, 300, 0, "Lead-Time Distribution"),
        chart("lineChart", "chart_lead_time_trend", "YearMonth", "AvgLeadTimeDays", 660, 20, 600, 300, 1, "Lead-Time Trend"),
        chart("barChart", "chart_supplier_lead_time", "VendorName", "LeadTimeStdDevDays", 20, 340, 620, 360, 2, "Supplier Lead-Time Variability"),
        table("chart_supplier_lead_time", [
            "VendorName", "ObservedLeadTimeOrderCount", "AverageLeadTimeDays", "MedianLeadTimeDays",
            "LeadTimeStdDevDays", "LeadTimeVarianceDays",
        ], 660, 340, 600, 360, 3),
    ]))

    pages.append(page("6. Inventory Availability", 5, [
        textbox("Inventory Availability", "Vendor-linked inventory only for 232 single-vendor complete-snapshot brands in scorecard fields.", 20, 8, 1240, 45, 0),
        chart("barChart", "chart_store_inventory", "Store", "EndingUnits", 20, 70, 620, 300, 1, "Ending Inventory by Store"),
        chart("barChart", "chart_store_inventory", "Store", "BeginningUnits", 660, 70, 600, 300, 2, "Beginning Inventory by Store"),
        chart("barChart", "chart_brand_inventory", "Brand", "EndingRetailValue", 20, 390, 620, 310, 3, "Inventory Value by Brand"),
        chart("barChart", "chart_brand_inventory", "Brand", "LowInventoryFlag", 660, 390, 600, 310, 4, "Low Inventory Flags by Brand"),
    ]))

    pages.append(page("7. Supplier Detail", 6, [
        slicer("fact_supplier_performance", "VendorName", 20, 20, 240, 160, 0),
        slicer("fact_supplier_performance", "PerformanceStatus", 280, 20, 220, 160, 1),
        chart("barChart", "fact_supplier_performance", "VendorName", "TotalPurchaseDollars", 520, 20, 740, 160, 2, "Supplier Spend"),
        table("fact_supplier_performance", [
            "VendorName", "PurchaseOrderKeyCount", "TotalPurchaseDollars", "TotalPurchaseQuantity",
            "AverageLeadTimeDays", "MedianLeadTimeDays", "LeadTimeStdDevDays", "LeadTimeVarianceDays",
            "InvoiceMatchRatePct", "QuantityAlignmentRatePct", "DollarAlignmentRatePct",
            "AbsoluteAvgPriceVariancePct", "EstimatedEndingInventoryCost", "PerformanceStatus",
        ], 20, 200, 1240, 500, 3),
    ]))

    pages.append(page("8. Planning & Risk", 7, [
        chart("barChart", "chart_planning_risk", "VendorName", "RiskFlagCount", 20, 20, 1240, 230, 0, "Supplier Risk Ranking"),
        chart("barChart", "chart_planning_risk", "VendorName", "TotalPurchaseDollars", 20, 270, 620, 200, 1, "High Spend Suppliers"),
        chart("barChart", "chart_planning_risk", "VendorName", "MedianLeadTimeDays", 660, 270, 600, 200, 2, "High Lead-Time Suppliers"),
        table("chart_planning_risk", [
            "VendorName", "TotalPurchaseDollars", "PurchaseContributionPct", "MedianLeadTimeDays",
            "LeadTimeStdDevDays", "InvoiceMatchRatePct", "AbsoluteAvgPriceVariancePct",
            "HighCycleTimePOCount", "RiskFlagCount", "PerformanceStatus", "RecommendedAttention",
        ], 20, 490, 1240, 210, 3),
    ]))

    if legacy_section is not None:
        legacy = json.loads(json.dumps(legacy_section))
        legacy["displayName"] = "9. Legacy Vendor Performance"
        legacy["ordinal"] = 8
        legacy["id"] = 8
        legacy["name"] = uid()
        pages.append(legacy)
    else:
        pages.append(page("9. Legacy Vendor Performance", 8, [
            card("vendor_sales_summary", "TotalSalesDollars", 20, 20, 220, 80, 0, "Total Sales"),
            card("vendor_sales_summary", "TotalPurchaseDollars", 260, 20, 220, 80, 1, "Total Purchase"),
            card("vendor_sales_summary", "GrossProfit", 500, 20, 220, 80, 2, "Gross Profit"),
            chart("donutChart", "Vendor_Purchase_Summary", "Vendor Category", "TotalPurchaseDollars", 20, 120, 400, 280, 3, "Purchase Contribution"),
            chart("barChart", "BrandPerformance", "Description", "TotalSales", 440, 120, 400, 280, 4, "Top Brands"),
            table("LowTurnoverVendor", ["VendorName", "AvgStockTurnOver"], 860, 120, 400, 280, 5),
        ]))

    return pages


def load_layout_bytes(pbix: Path):
    with zipfile.ZipFile(pbix) as zf:
        raw = zf.read("Report/Layout")
        others = {i.filename: zf.read(i.filename) for i in zf.infolist() if i.filename != "Report/Layout" and not i.is_dir()}
    try:
        layout = json.loads(raw.decode("utf-16-le"))
        enc = "utf-16-le"
    except UnicodeDecodeError:
        layout = json.loads(raw.decode("utf-8"))
        enc = "utf-8"
    return layout, enc, others


def main():
    source = LEGACY if LEGACY.exists() else PBIX
    layout, enc, others = load_layout_bytes(source)
    legacy = None
    for sec in layout.get("sections", []):
        # prefer original legacy visuals (12 containers)
        if len(sec.get("visualContainers", [])) >= 10:
            legacy = sec
            break
    if legacy is None and layout.get("sections"):
        legacy = layout["sections"][0]

    sections = build_sections(legacy)
    new_layout = dict(layout)
    new_layout["sections"] = sections
    LAYOUT_OUT.parent.mkdir(parents=True, exist_ok=True)
    LAYOUT_OUT.write_text(json.dumps(new_layout, indent=2), encoding="utf-8")

    # Write into Dashboard.pbix preserving DataModel/other parts
    encoded = json.dumps(new_layout, ensure_ascii=False, separators=(",", ":")).encode(enc)
    tmp = PROJECT / "Dashboard_charts_tmp.pbix"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in others.items():
            zf.writestr(name, data)
        zf.writestr("Report/Layout", encoded)

    shutil.copy2(tmp, PBIX)
    tmp.unlink(missing_ok=True)

    counts = {s["displayName"]: len(s.get("visualContainers", [])) for s in sections}
    print(json.dumps({
        "updated": str(PBIX),
        "layout_json": str(LAYOUT_OUT),
        "visual_counts": counts,
        "note": (
            "Classic chart visuals injected. If DataModel lacks chart_* tables, open "
            "powerbi/ProcurementDashboard/ProcurementDashboard.pbip in Power BI Desktop "
            "(model already contains 26 tables), refresh, then Save As Dashboard.pbix."
        ),
    }, indent=2))


if __name__ == "__main__":
    main()
