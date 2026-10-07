import logging
import os
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.types import Date, Float, Integer, Unicode


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VENDOR_DATA_DIR", PROJECT_DIR / "data"))
LOG_DIR = PROJECT_DIR / "logs"
CHUNK_ROWS = int(os.getenv("CSV_CHUNK_ROWS", "100000"))
SQL_CHUNK_ROWS = int(os.getenv("SQL_CHUNK_ROWS", "10000"))
SOURCE_TABLES = {
    "begin_inventory.csv": "begin_inventory",
    "end_inventory.csv": "end_inventory",
    "purchases.csv": "purchases",
    "purchase_prices.csv": "purchase_prices",
    "sales.csv": "sales",
    "vendor_invoice.csv": "vendor_invoice",
}
DATE_COLUMNS = {
    "begin_inventory": ("startDate",),
    "end_inventory": ("endDate",),
    "purchases": ("PODate", "ReceivingDate", "InvoiceDate", "PayDate"),
    "sales": ("SalesDate",),
    "vendor_invoice": ("InvoiceDate", "PODate", "PayDate"),
}
INTEGER_COLUMNS = {
    "begin_inventory": ("Store", "Brand", "onHand"),
    "end_inventory": ("Store", "Brand", "onHand"),
    "purchases": ("Store", "Brand", "VendorNumber", "PONumber", "Quantity", "Classification"),
    "purchase_prices": ("Brand", "Classification", "VendorNumber"),
    "sales": ("Store", "Brand", "SalesQuantity", "Classification", "VendorNo"),
    "vendor_invoice": ("VendorNumber", "PONumber", "Quantity"),
}
FLOAT_COLUMNS = {
    "begin_inventory": ("Price",),
    "end_inventory": ("Price",),
    "purchases": ("PurchasePrice", "Dollars"),
    "purchase_prices": ("Price", "PurchasePrice"),
    "sales": ("SalesDollars", "SalesPrice", "Volume", "ExciseTax"),
    "vendor_invoice": ("Dollars", "Freight"),
}
TEXT_COLUMNS = {
    "begin_inventory": ("InventoryId", "City", "Description", "Size"),
    "end_inventory": ("InventoryId", "City", "Description", "Size"),
    "purchases": ("InventoryId", "Description", "Size", "VendorName"),
    "purchase_prices": ("Description", "Size", "Volume", "VendorName"),
    "sales": ("InventoryId", "Description", "Size", "VendorName"),
    "vendor_invoice": ("VendorName", "Approval"),
}


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "ingestion_db.log",
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        force=True,
    )


configure_logging()
logger = logging.getLogger(__name__)


def create_db_and_engine():
    server = os.getenv("SQL_SERVER", r"localhost\SQLEXPRESS")
    driver = os.getenv("SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    connection_options = {
        "driver": driver,
        "trusted_connection": "yes",
    }
    master_url = URL.create(
        "mssql+pyodbc",
        host=server,
        database="master",
        query=connection_options,
    )
    master_engine = create_engine(
        master_url,
        isolation_level="AUTOCOMMIT",
        fast_executemany=True,
        pool_pre_ping=True,
    )

    with master_engine.connect() as connection:
        connection.execute(text("IF DB_ID('inventory') IS NULL CREATE DATABASE inventory"))
    master_engine.dispose()

    inventory_url = URL.create(
        "mssql+pyodbc",
        host=server,
        database="inventory",
        query=connection_options,
    )
    return create_engine(
        inventory_url,
        fast_executemany=True,
        pool_pre_ping=True,
    )


def _pandas_dtypes(table_name):
    dtype_map = {column: "Int64" for column in INTEGER_COLUMNS[table_name]}
    dtype_map.update({column: "float64" for column in FLOAT_COLUMNS[table_name]})
    dtype_map.update({column: "string" for column in TEXT_COLUMNS[table_name]})
    dtype_map.update({column: "string" for column in DATE_COLUMNS.get(table_name, ())})
    return dtype_map


def _sql_dtypes(table_name):
    dtype_map = {column: Integer() for column in INTEGER_COLUMNS[table_name]}
    dtype_map.update({column: Float(precision=53) for column in FLOAT_COLUMNS[table_name]})
    dtype_map.update({column: Unicode(length=255) for column in TEXT_COLUMNS[table_name]})
    dtype_map.update({column: Date() for column in DATE_COLUMNS.get(table_name, ())})
    return dtype_map


def _parse_dates(chunk, table_name, file_name):
    for column in DATE_COLUMNS.get(table_name, ()):
        original = chunk[column]
        parsed = pd.to_datetime(original, format="%Y-%m-%d", errors="coerce")
        invalid = original.notna() & original.ne("") & parsed.isna()
        if invalid.any():
            examples = original[invalid].head(3).tolist()
            raise ValueError(f"Invalid {column} values in {file_name}: {examples}")
        chunk[column] = parsed
    return chunk


def _stage_table(engine, path, table_name):
    staging_name = f"{table_name}__staging"
    expected_rows = 0
    first_chunk = True

    for chunk in pd.read_csv(
        path,
        chunksize=CHUNK_ROWS,
        dtype=_pandas_dtypes(table_name),
        keep_default_na=True,
        low_memory=False,
    ):
        chunk = _parse_dates(chunk, table_name, path.name)
        chunk.to_sql(
            staging_name,
            con=engine,
            schema="dbo",
            if_exists="replace" if first_chunk else "append",
            index=False,
            chunksize=SQL_CHUNK_ROWS,
            dtype=_sql_dtypes(table_name),
            method=None,
        )
        expected_rows += len(chunk)
        first_chunk = False
        logger.info("Staged %s rows from %s", expected_rows, path.name)

    if first_chunk:
        raise ValueError(f"No data rows found in {path}")

    with engine.connect() as connection:
        actual_rows = connection.execute(
            text(f"SELECT COUNT_BIG(*) FROM dbo.[{staging_name}]")
        ).scalar_one()
    if actual_rows != expected_rows:
        raise RuntimeError(
            f"Row-count mismatch for {path.name}: read {expected_rows}, staged {actual_rows}"
        )

    logger.info("Validated %s: %s rows", path.name, actual_rows)
    return staging_name, expected_rows


def _publish_tables(engine, staged_tables):
    with engine.begin() as connection:
        for table_name, staging_name in staged_tables.items():
            connection.execute(
                text(f"IF OBJECT_ID('dbo.{table_name}', 'U') IS NOT NULL DROP TABLE dbo.[{table_name}]")
            )
            connection.execute(
                text(f"EXEC sp_rename 'dbo.{staging_name}', '{table_name}'")
            )


def load_raw_data(engine, data_dir=DATA_DIR):
    started = time.time()
    data_dir = Path(data_dir)
    missing = [name for name in SOURCE_TABLES if not (data_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing required CSV files in {data_dir}: {missing}")

    staged_tables = {}
    counts = {}
    try:
        for file_name, table_name in SOURCE_TABLES.items():
            logger.info("Starting staged ingestion of %s", file_name)
            staging_name, row_count = _stage_table(
                engine,
                data_dir / file_name,
                table_name,
            )
            staged_tables[table_name] = staging_name
            counts[table_name] = row_count

        _publish_tables(engine, staged_tables)
    except Exception:
        logger.exception("Ingestion failed; published source tables were not replaced")
        raise

    elapsed_minutes = (time.time() - started) / 60
    logger.info("Ingestion complete: %s", counts)
    logger.info("Total time taken: %.2f minutes", elapsed_minutes)
    return counts


if __name__ == "__main__":
    load_raw_data(create_db_and_engine())