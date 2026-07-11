"""
Tests for stock ETL normalization.
"""

import pandas as pd

from app.services.etl_service import _normalize_stock_upload_frame


def test_normalize_stock_upload_frame_keeps_last_valid_month_snapshot():
    df = pd.DataFrame(
        [
            {
                "YEAR_MONTH": "2026-01",
                "STOCK_START_OF_PERIOD": "2026-01-05",
                "PRODUCT_FAMILY": "FAMILY_A",
                "STOCK_QTY": 10,
                "CURRENT_STOCK_QTY": 10,
                "PRODUCT_ID": "PROD_A",
                "WAREHOUSE": "WH1",
                "DEALER_ID": "D1",
            },
            {
                "YEAR_MONTH": "2026-01",
                "STOCK_START_OF_PERIOD": "2026-01-25",
                "PRODUCT_FAMILY": "FAMILY_A",
                "STOCK_QTY": 14,
                "CURRENT_STOCK_QTY": 14,
                "PRODUCT_ID": "PROD_A",
                "WAREHOUSE": "WH1",
                "DEALER_ID": "D1",
            },
            {
                "YEAR_MONTH": "2026-01",
                "STOCK_START_OF_PERIOD": "2026-01-28",
                "PRODUCT_FAMILY": "FAMILY_A",
                "STOCK_QTY": 100,
                "CURRENT_STOCK_QTY": 15,
                "PRODUCT_ID": "PROD_A",
                "WAREHOUSE": "WH1",
                "DEALER_ID": "D1",
            },
            {
                "YEAR_MONTH": "2026-02",
                "STOCK_START_OF_PERIOD": "2026-02-03",
                "PRODUCT_FAMILY": "FAMILY_A",
                "STOCK_QTY": 20,
                "CURRENT_STOCK_QTY": None,
                "PRODUCT_ID": "PROD_A",
                "WAREHOUSE": "WH1",
                "DEALER_ID": "D1",
            },
        ]
    )

    normalized = _normalize_stock_upload_frame(df)

    assert len(normalized) == 2
    assert list(normalized["SNAPSHOT_PERIOD"].dt.strftime("%Y-%m-%d")) == ["2026-01-01", "2026-02-01"]
    assert normalized.iloc[0]["CURRENT_STOCK_QTY_NORMALIZED"] == 14
    assert normalized.iloc[1]["CURRENT_STOCK_QTY_NORMALIZED"] == 20


def test_normalize_stock_upload_frame_drops_inconsistent_rows():
    df = pd.DataFrame(
        [
            {
                "YEAR_MONTH": "2026-03",
                "STOCK_START_OF_PERIOD": "2026-03-01",
                "PRODUCT_FAMILY": "FAMILY_B",
                "STOCK_QTY": 50,
                "CURRENT_STOCK_QTY": 500,
                "PRODUCT_ID": "PROD_B",
                "WAREHOUSE": "WH2",
                "DEALER_ID": "D2",
            },
            {
                "YEAR_MONTH": "2026-03",
                "STOCK_START_OF_PERIOD": "2026-03-15",
                "PRODUCT_FAMILY": "FAMILY_B",
                "STOCK_QTY": 55,
                "CURRENT_STOCK_QTY": 55,
                "PRODUCT_ID": "PROD_B",
                "WAREHOUSE": "WH2",
                "DEALER_ID": "D2",
            },
        ]
    )

    normalized = _normalize_stock_upload_frame(df)

    assert len(normalized) == 1
    assert normalized.iloc[0]["CURRENT_STOCK_QTY_NORMALIZED"] == 55
