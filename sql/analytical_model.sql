/*
  Procurement analytical model — SQL Server Express (inventory database)

  This script documents the analytical structures published by
  get_vendor_summary.py / procurement_analytics.py. Raw source tables are
  preserved. Re-run Python to rebuild; do not treat this file as a standalone
  replacement for the Python pipeline.

  Conceptual star-schema mapping (only entities supported by source fields):

    DimSupplier              -> dbo.dim_supplier
    DimBrand                 -> dbo.dim_brand
    DimStore / DimLocation   -> dbo.dim_store
    DimDate                  -> dbo.dim_date
    FactPurchase (monthly)   -> dbo.fact_procurement_monthly
    FactPurchaseOrder        -> dbo.fact_purchase_order / dbo.purchase_order_summary
    FactLeadTime             -> dbo.fact_lead_time
    FactDeliveryReconcile    -> dbo.fact_delivery_reconciliation
    FactInventory            -> dbo.fact_inventory_summary / dbo.inventory_position
    FactSupplierPerformance  -> dbo.fact_supplier_performance

  NOT CREATED (unsupported by source):
    - True OTIF / promised-date on-time flags
    - Open-PO aging as-of a current date
    - Ordered-vs-received fill rate distinct from invoice reconciliation
*/

-- Example relationship keys for Power BI modeling:
-- dim_supplier[VendorNumber] 1->* fact_purchase_order[VendorNumber]
-- dim_supplier[VendorNumber] 1->* fact_supplier_performance[VendorNumber]
-- dim_brand[Brand] 1->* fact_procurement_monthly[Brand]
-- dim_store[Store] 1->* fact_inventory_summary[Store]
-- dim_date[FullDate] 1->* fact_purchase_order[PODate]  (date relationship)

-- Observed lead time (supported):
--   ObservedLeadTimeDays = DATEDIFF(day, PODate, LastReceivingDate)
--   only when one consistent PODate exists and a receipt date exists.

-- PO cycle-time aging buckets (supported for completed receipts):
--   0-7 / 8-15 / 16-30 / 31-60 / 60+ days based on ObservedLeadTimeDays.
--   This is NOT open-order aging.

-- Delivery performance proxy (supported):
--   Invoice match rate and matched quantity/dollar reconciliation alignment.
--   Label clearly as reconciliation performance, not OTIF.
