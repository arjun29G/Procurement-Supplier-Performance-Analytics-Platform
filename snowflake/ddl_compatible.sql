-- Snowflake-compatible DDL sketches mirroring the local analytical model.
-- NOT executed by the local pipeline. No live Snowflake connection is claimed.

CREATE SCHEMA IF NOT EXISTS RAW;
CREATE SCHEMA IF NOT EXISTS ANALYTICS;

-- Example RAW table (subset of purchases columns)
CREATE TABLE IF NOT EXISTS RAW.PURCHASES (
    INVENTORY_ID VARCHAR,
    STORE NUMBER,
    BRAND NUMBER,
    DESCRIPTION VARCHAR,
    SIZE VARCHAR,
    VENDOR_NUMBER NUMBER,
    VENDOR_NAME VARCHAR,
    PO_NUMBER NUMBER,
    PO_DATE DATE,
    RECEIVING_DATE DATE,
    INVOICE_DATE DATE,
    PAY_DATE DATE,
    PURCHASE_PRICE FLOAT,
    QUANTITY NUMBER,
    DOLLARS FLOAT,
    CLASSIFICATION NUMBER
);

-- Observed lead time at vendor-PO grain (same business rule as SQL Server)
CREATE OR REPLACE VIEW ANALYTICS.V_PURCHASE_ORDER_LEAD_TIME AS
SELECT
    VENDOR_NUMBER,
    PO_NUMBER,
    MIN(PO_DATE) AS PO_DATE,
    MAX(RECEIVING_DATE) AS LAST_RECEIVING_DATE,
    SUM(QUANTITY) AS PURCHASE_QUANTITY,
    SUM(DOLLARS) AS PURCHASE_DOLLARS,
    CASE
        WHEN MIN(PO_DATE) = MAX(PO_DATE) AND MAX(RECEIVING_DATE) IS NOT NULL
        THEN DATEDIFF('day', MIN(PO_DATE), MAX(RECEIVING_DATE))
    END AS OBSERVED_LEAD_TIME_DAYS
FROM RAW.PURCHASES
GROUP BY VENDOR_NUMBER, PO_NUMBER;

-- Cycle-time aging bucket (completed receipts only; not open-PO aging)
CREATE OR REPLACE VIEW ANALYTICS.V_PO_CYCLE_AGING AS
SELECT
    *,
    CASE
        WHEN OBSERVED_LEAD_TIME_DAYS IS NULL THEN NULL
        WHEN OBSERVED_LEAD_TIME_DAYS <= 7 THEN '0-7 days'
        WHEN OBSERVED_LEAD_TIME_DAYS <= 15 THEN '8-15 days'
        WHEN OBSERVED_LEAD_TIME_DAYS <= 30 THEN '16-30 days'
        WHEN OBSERVED_LEAD_TIME_DAYS <= 60 THEN '31-60 days'
        ELSE '60+ days'
    END AS PO_CYCLE_AGING_BUCKET
FROM ANALYTICS.V_PURCHASE_ORDER_LEAD_TIME;

-- NOTE: Do not create OTIF views unless promised delivery date and distinct
-- ordered vs received quantities are loaded into RAW.
