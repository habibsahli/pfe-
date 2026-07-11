from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import re
import logging
from contextlib import nullcontext
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

SERVICE_MAP = {
    "FIBRE": ["FIBER", "FIBRE", "FTTH", "OPTIC", "FIBRE_OPTIQUE"],
    "5G": ["5G", "FIXE JDID", "JDID", "HOME_5G"],
    "DATA_BUNDLE": ["DATA", "GO", "GB", "BUNDLE", "INTERNET MOBILE"],
    "VOD": ["VOD", "VIDEO", "STREAM", "IPTV", "TV"],
}

DATE_CANDIDATES = ["CREATION_DATE", "DATE", "TRANSACTION_DATE", "SNAPSHOT_DATE", "CREATED_AT"]
ID_CANDIDATES = ["MSISDN", "CLIENT_ID"]
DEALER_CANDIDATES = ["DEALER_ID", "DEALER", "POINT_VENTE", "DEALER_CODE"]
QTY_CANDIDATES = [
    "STOCK_QTY",
    "CURRENT_STOCK_QTY",
    "STOCK_QUANTITY",
    "INVENTORY_QTY",
    "AVAILABLE_QTY",
    "QTY",
]
PRODUCT_ID_CANDIDATES = [
    "PRODUCT_ID",
    "PRODUCT_NAME",
    "SKU",
    "ITEM_CODE",
    "COD_PROD",
    "PRODUCT_CODE",
]


@dataclass
class UploadResult:
    session_id: str
    rows: int
    service: str
    period_start: str
    period_end: str
    preview: list[dict[str, Any]]
    filename: str
    file_type: str
    inserted_rows: int
    is_duplicate: bool = False


def _normalize_column_name(col: str) -> str:
    return (
        col.strip()
        .upper()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .replace(".", "_")
    )


def _pick_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for name in candidates:
        if name in df.columns:
            return name
    return None


def _parse_datetime(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", dayfirst=False, yearfirst=True)
    if parsed.notna().sum() == 0:
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=True)
    return parsed


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    text_value = str(value).strip()
    return "" if text_value.lower() in {"nan", "none", "nat"} else text_value


def _safe_float(value: Any, default: float | None = None) -> float | None:
    """Safely convert value to float, returning default on failure."""
    if value is None:
        return default
    s = _safe_str(value)
    if not s:
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _safe_int(value: Any, default: int | None = None) -> int | None:
    """Safely convert value to int, returning default on failure."""
    if value is None:
        return default
    s = _safe_str(value)
    if not s:
        return default
    try:
        return int(float(s))  # Parse as float first to handle decimals
    except (ValueError, TypeError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Safely convert value to bool."""
    if value is None:
        return default
    s = _safe_str(value)
    if not s:
        return default
    return s.lower() in {"true", "1", "yes", "y", "on", "t"}


def _normalize_stock_upload_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize stock uploads to one valid snapshot per month/product/warehouse."""
    work = df.copy()

    snapshot_date_created = False
    if "STOCK_START_OF_PERIOD" in work.columns:
        work["SNAPSHOT_DATE"] = _parse_datetime(work["STOCK_START_OF_PERIOD"])
        snapshot_date_created = True
    elif "YEAR_MONTH" in work.columns:
        ym_series = work["YEAR_MONTH"].astype(str).fillna("").str.strip()

        def _normalize_year_month(s: str) -> str:
            s2 = re.sub(r"[^0-9/\-]", "", s)
            if re.match(r"^\d{6}$", s2):
                return f"{s2[:4]}-{s2[4:6]}-01"
            m = re.match(r"^(\d{1,2})/(\d{4})$", s2)
            if m:
                mm, yyyy = m.group(1).zfill(2), m.group(2)
                return f"{yyyy}-{mm}-01"
            if re.match(r"^\d{4}-\d{2}(-\d{2})?$", s2):
                return (s2[:7] + "-01") if len(s2) >= 7 and len(s2) < 10 else s2[:10]
            return s2

        try:
            normalized = ym_series.map(_normalize_year_month)
            work["SNAPSHOT_DATE"] = _parse_datetime(normalized)
        except Exception:
            work["SNAPSHOT_DATE"] = _parse_datetime(work["YEAR_MONTH"].astype(str))
        snapshot_date_created = True

    if not snapshot_date_created:
        date_col = _pick_column(work, DATE_CANDIDATES)
        if not date_col:
            raise ValueError("No stock snapshot date column found")
        work["SNAPSHOT_DATE"] = _parse_datetime(work[date_col])

    qty_col = _pick_column(work, QTY_CANDIDATES)
    if not qty_col:
        raise ValueError("No stock quantity column found")

    product_col = _pick_column(work, PRODUCT_ID_CANDIDATES)
    if product_col is None:
        raise ValueError("No product identifier column found")

    warehouse_col = (
        "WAREHOUSE_CODE"
        if "WAREHOUSE_CODE" in work.columns
        else "WAREHOUSE"
        if "WAREHOUSE" in work.columns
        else None
    )

    work["ROW_ORDER"] = range(len(work))
    work["RAW_PRODUCT_KEY"] = work[product_col].astype(str).str.strip()
    work["WAREHOUSE_KEY"] = (
        work[warehouse_col].astype(str).str.strip() if warehouse_col else "UNKNOWN"
    )
    work["DEALER_KEY"] = (
        work["DEALER_ID"].astype(str).str.strip() if "DEALER_ID" in work.columns else ""
    )

    work["STOCK_QTY_VALUE"] = pd.to_numeric(work[qty_col], errors="coerce")
    if "CURRENT_STOCK_QTY" in work.columns:
        work["CURRENT_STOCK_QTY_VALUE"] = pd.to_numeric(work["CURRENT_STOCK_QTY"], errors="coerce")
    else:
        work["CURRENT_STOCK_QTY_VALUE"] = pd.Series([pd.NA] * len(work), index=work.index)

    work["CURRENT_STOCK_QTY_NORMALIZED"] = (
        work["CURRENT_STOCK_QTY_VALUE"].where(
            work["CURRENT_STOCK_QTY_VALUE"].notna(), work["STOCK_QTY_VALUE"]
        )
    )

    inconsistent_mask = (
        work["CURRENT_STOCK_QTY_VALUE"].notna()
        & work["STOCK_QTY_VALUE"].notna()
        & (
            (work["CURRENT_STOCK_QTY_VALUE"] < 0)
            | (work["STOCK_QTY_VALUE"] < 0)
            | (
                (work["CURRENT_STOCK_QTY_VALUE"] - work["STOCK_QTY_VALUE"]).abs()
                > (work[["CURRENT_STOCK_QTY_VALUE", "STOCK_QTY_VALUE"]].abs().max(axis=1).clip(lower=1.0) * 0.20)
            )
        )
    )

    dropped_inconsistent = int(inconsistent_mask.sum())
    if dropped_inconsistent:
        logging.getLogger(__name__).warning(
            "Dropped %s inconsistent stock rows before ingestion", dropped_inconsistent
        )

    work = work[~inconsistent_mask].copy()
    work = work[work["SNAPSHOT_DATE"].notna()].copy()
    work = work[work["RAW_PRODUCT_KEY"].astype(str).str.strip() != ""].copy()
    work = work[work["CURRENT_STOCK_QTY_NORMALIZED"].notna()].copy()
    work = work[work["CURRENT_STOCK_QTY_NORMALIZED"] >= 0].copy()

    if work.empty:
        return work

    work["SNAPSHOT_PERIOD"] = work["SNAPSHOT_DATE"].dt.to_period("M").dt.to_timestamp()
    work = work.sort_values(["SNAPSHOT_PERIOD", "SNAPSHOT_DATE", "ROW_ORDER"])

    dedupe_subset = ["SNAPSHOT_PERIOD", "RAW_PRODUCT_KEY", "WAREHOUSE_KEY", "DEALER_KEY"]
    work = work.drop_duplicates(subset=dedupe_subset, keep="last")

    return work.reset_index(drop=True)


def _normalize_5g_stock_upload_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize 5G dataset stock uploads (36M Tunisian governorate-level data).
    
    Input format: COD_PROD, QTE_STK, YEAR_MONTH (YYYY-MM), GOVERNORATE, ACTIVATIONS, QTE_RES,
                  QTE_INV, QTE_DEB_EXE, etc.
    Output: Snapshot per month/product/governorate with parsed dates and validated quantities.
    
    Row granularity: one row = one product × one month × one governorate
    Date parsing: YEAR_MONTH in YYYY-MM format → first day of month
    Deduplication key: (snapshot_date, product_id, governorate) → keep last chronologically
    """
    work = df.copy()
    
    # Parse YEAR_MONTH (YYYY-MM format) to first day of month
    if "YEAR_MONTH" not in work.columns:
        raise ValueError("5G dataset must contain YEAR_MONTH column in YYYY-MM format")
    
    ym_series = work["YEAR_MONTH"].astype(str).fillna("").str.strip()
    normalized_dates = []
    for ym in ym_series:
        try:
            # Expected: YYYY-MM
            if "-" in ym and len(ym) == 7:
                parts = ym.split("-")
                yyyy, mm = int(parts[0]), int(parts[1])
                normalized_dates.append(f"{yyyy:04d}-{mm:02d}-01")
            else:
                normalized_dates.append(None)
        except (ValueError, IndexError):
            normalized_dates.append(None)
    
    work["SNAPSHOT_DATE"] = pd.to_datetime(normalized_dates, errors="coerce")
    work = work[work["SNAPSHOT_DATE"].notna()].copy()
    
    if work.empty:
        return work
    
    # Extract and validate key quantities
    work["PRODUCT_ID"] = work["COD_PROD"].astype(str).str.strip()
    work["WAREHOUSE_KEY"] = work["GOVERNORATE"].astype(str).str.strip()  # Use GOVERNORATE as warehouse code
    
    # Main stock quantity: QTE_STK
    work["STOCK_QTY_VALUE"] = pd.to_numeric(work.get("QTE_STK", 0), errors="coerce").fillna(0).astype(int)
    work["STOCK_QTY_VALUE"] = work["STOCK_QTY_VALUE"].clip(lower=0)  # No negative stock
    
    # Additional inventory metrics from new columns (if present)
    # Helper: try multiple column names
    def get_column_or_default(df, candidates, default_val=0):
        for col_name in candidates:
            if col_name in df.columns:
                return pd.to_numeric(df[col_name], errors="coerce").fillna(default_val).astype(int).clip(lower=0)
        # All candidates missing: return series of defaults
        return pd.Series([default_val] * len(df), index=df.index, dtype=int)
    
    work["ACTIVATIONS_QTY"] = get_column_or_default(work, ["ACTIVATIONS_QTY", "ACTIVATIONS"], 0)
    work["SALES_QTY"] = get_column_or_default(work, ["SALES_QTY", "QTE_VTE"], 0)
    work["RESERVED_QTY"] = get_column_or_default(work, ["RESERVED_QTY", "QTE_RES"], 0)
    work["INVENTORY_QTY"] = get_column_or_default(work, ["INVENTORY_QTY", "QTE_INV"], 0)
    work["STOCK_OPENING_QTY"] = get_column_or_default(work, ["STOCK_OPENING_QTY", "QTE_DEB_EXE"], 0)
    
    # Price and classification fields
    work["SELLING_PRICE_TTC"] = (pd.to_numeric(work["SELLING_PRICE_TTC"], errors="coerce") if "SELLING_PRICE_TTC" in work.columns else (pd.to_numeric(work["PV_TTC"], errors="coerce") if "PV_TTC" in work.columns else pd.Series([pd.NA] * len(work), index=work.index)))
    work["PRODUCT_FAMILY"] = (
        work["COD_FAM"].astype(str).str.strip() if "COD_FAM" in work.columns else "UNKNOWN"
    )
    work["PRODUCT_TYPE"] = (
        work["PRODUCT_TYPE"].astype(str).str.strip() if "PRODUCT_TYPE" in work.columns else "UNKNOWN"
    )
    work["FLAG_5G"] = (
        work["FLAG_5G"].astype(str).str.strip().str.upper().eq("5G")
        if "FLAG_5G" in work.columns
        else True
    )
    work["FLAG_ACTIVE"] = (
        work["ACTIF"].astype(str).str.strip().str.upper().eq("O")
        if "ACTIF" in work.columns
        else True
    )
    work["DATA_SOURCE"] = (
        work["DATA_SOURCE"].astype(str).str.strip() if "DATA_SOURCE" in work.columns else "REAL"
    )
    
    # Optional product attributes
    work["PRODUCT_NAME"] = (
        work["DES_PROD"].astype(str).str.strip() if "DES_PROD" in work.columns else ""
    )
    work["PRODUCT_GROUP"] = (
        work["COD_GROUP"].astype(str).str.strip() if "COD_GROUP" in work.columns else ""
    )
    work["GAMME"] = work["GAMME"].astype(str).str.strip() if "GAMME" in work.columns else ""
    work["TYPE_PROD"] = (
        work["TYPE_PROD"].astype(str).str.strip() if "TYPE_PROD" in work.columns else ""
    )
    
    # Validate minimal requirements
    work = work[work["PRODUCT_ID"] != ""].copy()
    work = work[work["WAREHOUSE_KEY"] != ""].copy()
    work = work[work["SNAPSHOT_DATE"].notna()].copy()
    
    if work.empty:
        return work
    
    # Sort and deduplicate: keep last row per (snapshot_date, product_id, warehouse)
    work = work.sort_values(["SNAPSHOT_DATE", "PRODUCT_ID", "WAREHOUSE_KEY"])
    work = work.drop_duplicates(subset=["SNAPSHOT_DATE", "PRODUCT_ID", "WAREHOUSE_KEY"], keep="last")
    
    return work.reset_index(drop=True)


def _valid_msisdn(value: Any) -> bool:
    digits = "".join(ch for ch in _safe_str(value) if ch.isdigit())
    if not digits:
        return False
    if digits.startswith("216"):
        return len(digits) >= 11
    return len(digits) >= 8


def detect_service_type(record: dict[str, Any], forced_service: str | None = None) -> str:
    if forced_service:
        return forced_service.upper().strip()

    explicit = _safe_str(record.get("SERVICE_TYPE") or record.get("SERVICE") or record.get("TYPE_SERVICE"))
    if explicit:
        explicit_upper = explicit.upper()
        if explicit_upper in SERVICE_MAP:
            return explicit_upper

    text_blob = " ".join(
        [
            _safe_str(record.get("OFFRE")),
            _safe_str(record.get("PRODUCT_ID")),
            _safe_str(record.get("PRODUCT_NAME")),
            _safe_str(record.get("DESCRIPTION")),
            _safe_str(record.get("LIBELLE")),
        ]
    ).upper()

    for service, patterns in SERVICE_MAP.items():
        if any(pattern in text_blob for pattern in patterns):
            return service

    return "UNKNOWN"


def detect_file_type(df: pd.DataFrame) -> str:
    """Detect whether uploaded file is stock, promotion, or sales data.
    
    Supports both legacy stock format and new 5G dataset format:
    - Legacy: YEAR_MONTH, PRODUCT_FAMILY, STOCK_START_OF_PERIOD
    - 5G New: COD_PROD, QTE_STK, YEAR_MONTH, GOVERNORATE (36-month inventory dataset)
    """
    cols = set(df.columns)
    
    # 5G dataset detection (highest priority): check for all required 5G columns
    # All these columns indicate the new 5G stock forecast dataset
    five_g_signature = {"COD_PROD", "QTE_STK", "YEAR_MONTH", "GOVERNORATE"}
    if five_g_signature.issubset(cols):
        return "stock_5g"
    
    # Legacy stock detection: check for stock-specific signature
    stock_signature = {"YEAR_MONTH", "PRODUCT_FAMILY", "STOCK_START_OF_PERIOD"}
    if stock_signature.issubset(cols):
        return "stock"

    # Broader stock heuristics for files that do not include the exact legacy signature
    stock_date_cols = {"YEAR_MONTH", "STOCK_START_OF_PERIOD", "SNAPSHOT_DATE", "DATE"}
    stock_qty_cols = {"STOCK_QTY", "STOCK_QUANTITY", "CURRENT_STOCK_QTY", "QTE_STK", "CURRENT_STOCK"}
    stock_product_cols = {"PRODUCT_ID", "PRODUCT_FAMILY", "PRODUCT_NAME", "COD_PROD"}
    if cols.intersection(stock_date_cols) and cols.intersection(stock_qty_cols) and cols.intersection(stock_product_cols):
        return "stock"
    
    # Fallback to old heuristics
    if "STOCK_QTY" in cols or "STOCK_QUANTITY" in cols or "WAREHOUSE" in cols or "WAREHOUSE_CODE" in cols:
        return "stock"
    if "PROMO_CODE" in cols or "DISCOUNT_PCT" in cols or "PROMOTION" in cols:
        return "promotion"
    return "sales"


class ETLService:
    def __init__(self) -> None:
        self.landing_dir = Path(settings.DATA_LANDING_DIR)
        self.archive_dir = Path(settings.DATA_ARCHIVE_DIR)
        self.landing_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def read_tabular_file(self, file_path: Path) -> pd.DataFrame:
        suffix = file_path.suffix.lower()
        if suffix == ".csv":
            with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
                sample = handle.read(4096)
                handle.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"])
                    df = pd.read_csv(handle, sep=dialect.delimiter)
                except csv.Error:
                    df = pd.read_csv(handle, sep=None, engine="python")
        elif suffix in {".xlsx", ".xls"}:
            df = pd.read_excel(file_path)
        else:
            raise ValueError(f"Unsupported file extension: {suffix}")

        normalized = df.copy()
        normalized.columns = [_normalize_column_name(c) for c in normalized.columns]
        return normalized

    def process_upload(self, file_path: Path, db: Session, forced_service: str | None = None, replace_stock: bool = False) -> UploadResult:
        logger = logging.getLogger(__name__)
        with nullcontext():
            df = self.read_tabular_file(file_path)
            file_type = detect_file_type(df)

            # If stock file and replace_stock=True, truncate existing stock facts
            if file_type in ("stock", "stock_5g") and replace_stock:
                try:
                    db.execute(text("TRUNCATE TABLE mart.fact_stock RESTART IDENTITY CASCADE"))
                    db.commit()
                    logger.info("✓ Truncated mart.fact_stock before ingesting new stock file")
                except Exception as e:
                    logger.error(f"Failed to truncate stock table: {e}")

            existing = self._find_existing_upload(db=db, source_file=file_path.name)
            if existing and int(existing.get("rows", 0)) > 0 and not (file_type in ("stock", "stock_5g") and replace_stock):
                period_start = existing.get("period_start") or self._infer_period_start(df)
                period_end = existing.get("period_end") or self._infer_period_end(df)
                detected_service = (
                    (forced_service or "").upper().strip()
                    or _safe_str(existing.get("service") or "")
                    or self._infer_service_from_df(df)
                    or "UNKNOWN"
                )

                archived_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{file_path.name}"
                shutil.copy(file_path, self.archive_dir / archived_name)

                return UploadResult(
                    session_id=str(uuid.uuid4()),
                    rows=int(existing.get("rows", 0)),
                    service=detected_service,
                    period_start=period_start,
                    period_end=period_end,
                    preview=df.head(5).fillna("").to_dict(orient="records"),
                    filename=file_path.name,
                    file_type=file_type,
                    inserted_rows=0,
                    is_duplicate=True,
                )

            if file_type == "stock_5g":
                result = self._ingest_stock_5g(df, file_path, db, forced_service)
            elif file_type == "stock":
                result = self._ingest_stock(df, file_path, db, forced_service)
            else:
                result = self._ingest_sales(df, file_path, db, forced_service)

            archived_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{file_path.name}"
            shutil.copy(file_path, self.archive_dir / archived_name)

            return result

    @staticmethod
    def _find_existing_upload(db: Session, source_file: str) -> dict[str, Any] | None:
        sales = db.execute(
            text(
                """
                SELECT
                  COUNT(*)::int AS rows,
                  MIN(t.date)::text AS period_start,
                  MAX(t.date)::text AS period_end,
                  MIN(s.service_code) AS service
                FROM mart.fact_ventes v
                JOIN mart.dim_temps t ON v.date_id = t.date_id
                JOIN mart.dim_services s ON v.service_id = s.service_id
                WHERE v.source_file = :source_file
                """
            ),
            {"source_file": source_file},
        ).fetchone()

        if sales and int(sales[0] or 0) > 0:
            return {
                "rows": int(sales[0]),
                "period_start": _safe_str(sales[1]),
                "period_end": _safe_str(sales[2]),
                "service": _safe_str(sales[3]),
            }

        stock = db.execute(
            text(
                """
                SELECT
                  COUNT(*)::int AS rows,
                  MIN(snapshot_date)::date::text AS period_start,
                  MAX(snapshot_date)::date::text AS period_end
                FROM mart.fact_stock
                WHERE source_file = :source_file
                """
            ),
            {"source_file": source_file},
        ).fetchone()

        if stock and int(stock[0] or 0) > 0:
            return {
                "rows": int(stock[0]),
                "period_start": _safe_str(stock[1]),
                "period_end": _safe_str(stock[2]),
                "service": "",
            }

        return None

    @staticmethod
    def _infer_service_from_df(df: pd.DataFrame) -> str:
        if df.empty:
            return "UNKNOWN"
        sample = df.head(min(len(df), 200))
        detected = sample.apply(lambda row: detect_service_type(row.to_dict()), axis=1)
        detected = detected[detected != "UNKNOWN"]
        if detected.empty:
            return "UNKNOWN"
        return _safe_str(detected.mode().iloc[0]).upper() or "UNKNOWN"

    @staticmethod
    def _infer_period_start(df: pd.DataFrame) -> str:
        date_col = _pick_column(df, DATE_CANDIDATES)
        if not date_col:
            return ""
        parsed = _parse_datetime(df[date_col]).dropna()
        if parsed.empty:
            return ""
        return str(parsed.min().date())

    @staticmethod
    def _infer_period_end(df: pd.DataFrame) -> str:
        date_col = _pick_column(df, DATE_CANDIDATES)
        if not date_col:
            return ""
        parsed = _parse_datetime(df[date_col]).dropna()
        if parsed.empty:
            return ""
        return str(parsed.max().date())

    def _ingest_sales(
        self,
        df: pd.DataFrame,
        file_path: Path,
        db: Session,
        forced_service: str | None,
    ) -> UploadResult:
        date_col = _pick_column(df, DATE_CANDIDATES)
        id_col = _pick_column(df, ID_CANDIDATES)
        dealer_col = _pick_column(df, DEALER_CANDIDATES)

        if not date_col:
            raise ValueError("No date column found in uploaded sales file")
        if not id_col:
            raise ValueError("No MSISDN/CLIENT_ID column found in uploaded sales file")

        work = df.copy()
        work["CREATED_AT"] = _parse_datetime(work[date_col])
        work = work.dropna(subset=["CREATED_AT"])
        work["SERVICE_TYPE_DETECTED"] = work.apply(
            lambda row: detect_service_type(row.to_dict(), forced_service),
            axis=1,
        )
        work = work[work["SERVICE_TYPE_DETECTED"] != "UNKNOWN"]

        if id_col == "MSISDN":
            work = work[work[id_col].apply(_valid_msisdn)]

        if work.empty:
            raise ValueError("No valid rows after ETL validation")

        etl_batch_id = str(uuid.uuid4())
        inserted_rows = 0

        for _, row in work.iterrows():
            record = row.to_dict()
            service_code = _safe_str(record.get("SERVICE_TYPE_DETECTED")).upper()
            created_at = pd.to_datetime(record["CREATED_AT"])

            date_id = self._ensure_date_dimension(db, created_at)
            geo_id = self._ensure_geo_dimension(db, record)
            dealer_id = self._ensure_dealer_dimension(db, _safe_str(record.get(dealer_col)) if dealer_col else "", geo_id)
            service_id = self._ensure_service_dimension(db, service_code)
            offre_id = self._ensure_offer_dimension(db, record, service_id)
            promo_id = self._ensure_promo_dimension(db, record, service_id, geo_id)
            product_id = self._ensure_product_dimension(db, record, service_id)

            db.execute(
                text(
                    """
                    INSERT INTO mart.fact_ventes
                    (
                      date_id, dealer_id, geo_id, offre_id, service_id, product_id, promo_id,
                      msisdn, client_id, created_at, transaction_type, source_file, etl_batch_id
                    )
                    VALUES
                    (
                      :date_id, :dealer_id, :geo_id, :offre_id, :service_id, :product_id, :promo_id,
                      :msisdn, :client_id, :created_at, :transaction_type, :source_file, :etl_batch_id
                    )
                    ON CONFLICT (msisdn, created_at, service_id)
                    DO UPDATE SET
                      dealer_id = EXCLUDED.dealer_id,
                      geo_id = EXCLUDED.geo_id,
                      offre_id = EXCLUDED.offre_id,
                      promo_id = EXCLUDED.promo_id,
                      etl_batch_id = EXCLUDED.etl_batch_id
                    """
                ),
                {
                    "date_id": date_id,
                    "dealer_id": dealer_id,
                    "geo_id": geo_id,
                    "offre_id": offre_id,
                    "service_id": service_id,
                    "product_id": product_id,
                    "promo_id": promo_id,
                    "msisdn": _safe_str(record.get("MSISDN")) or None,
                    "client_id": _safe_str(record.get("CLIENT_ID")) or None,
                    "created_at": created_at,
                    "transaction_type": _safe_str(record.get("TRANSACTION_TYPE")) or "new_subscription",
                    "source_file": file_path.name,
                    "etl_batch_id": etl_batch_id,
                },
            )
            inserted_rows += 1

        db.commit()

        mode_service = work["SERVICE_TYPE_DETECTED"].mode()
        service = forced_service or (mode_service.iloc[0] if not mode_service.empty else "UNKNOWN")

        return UploadResult(
            session_id=str(uuid.uuid4()),
            rows=int(len(work)),
            service=service,
            period_start=str(work["CREATED_AT"].min().date()),
            period_end=str(work["CREATED_AT"].max().date()),
            preview=work.head(5).fillna("").to_dict(orient="records"),
            filename=file_path.name,
            file_type="sales",
            inserted_rows=inserted_rows,
            is_duplicate=False,
        )

    def _ingest_stock(
        self,
        df: pd.DataFrame,
        file_path: Path,
        db: Session,
        forced_service: str | None,
    ) -> UploadResult:
        original = df.copy()
        logger = logging.getLogger(__name__)
        work = _normalize_stock_upload_frame(df)
        if work.empty:
            # Provide a helpful error message including sample values from the original file
            sample_vals = ""
            if "YEAR_MONTH" in original.columns:
                sample_vals = ", ".join(original["YEAR_MONTH"].astype(str).head(5).tolist())
            elif "STOCK_START_OF_PERIOD" in original.columns:
                sample_vals = ", ".join(original["STOCK_START_OF_PERIOD"].astype(str).head(5).tolist())
            logger.error("No valid stock rows after date parsing. Sample date values: %s", sample_vals)
            raise ValueError(f"No valid stock rows after date parsing. Sample date values: {sample_vals}")

        etl_batch_id = str(uuid.uuid4())
        inserted_rows = 0

        for _, row in work.iterrows():
            record = row.to_dict()
            snapshot_date = pd.to_datetime(record["SNAPSHOT_DATE"])
            date_id = self._ensure_date_dimension(db, snapshot_date)
            service_code = detect_service_type(record, forced_service)
            if service_code == "UNKNOWN":
                service_code = "5G"
            service_id = self._ensure_service_dimension(db, service_code)
            product_id = self._ensure_product_dimension(record=record, db=db, service_id=service_id)

            qty = _safe_int(record.get("CURRENT_STOCK_QTY_NORMALIZED"), 0)
            warehouse_code = _safe_str(record.get("WAREHOUSE_KEY")) or "UNKNOWN"

            # Ensure dealer exists in dimensional table
            geo_id = self._ensure_geo_dimension(db, record)
            dealer_code = _safe_str(record.get("DEALER_ID")) or None
            dealer_id = None
            if dealer_code:
                dealer_id = self._ensure_dealer_dimension(db, dealer_code, geo_id)
            # Extract inventory and sales metrics from CSV
            min_threshold = _safe_int(record.get("MIN_STOCK_THRESHOLD"), 10)
            max_capacity = _safe_int(record.get("MAX_STOCK_THRESHOLD") or record.get("MAX_STOCK_CAPACITY"), 500)
            current_stock = qty
            available = _safe_int(record.get("AVAILABLE_QTY")) or current_stock
            reserved = _safe_int(record.get("RESERVED_QTY"), 0)
            inventory = _safe_int(record.get("INVENTORY_QTY")) or current_stock
            avg_monthly_sales = _safe_float(record.get("AVG_MONTHLY_SALES"))
            sell_through = _safe_float(record.get("SELL_THROUGH_RATE"))
            days_supply = _safe_int(record.get("DAYS_OF_SUPPLY"))
            
            db.execute(
                text(
                    """
                    INSERT INTO mart.fact_stock
                    (
                      date_id, product_id, geo_id, dealer_id,
                      stock_quantity, stock_min_threshold, stock_max_capacity,
                      current_stock_qty, inventory_qty, reserved_qty, available_qty,
                      sales_qty, stock_movement, avg_monthly_sales, sell_through_rate, days_of_supply,
                      warehouse_code, is_rupture, is_low_stock,
                      stock_vs_min, stock_vs_max, has_zero_stock, has_negative_stock,
                      understock_risk, overstock_risk,
                      snapshot_date, source_file, etl_batch_id, data_source,
                      last_updated
                    )
                    VALUES
                    (
                      :date_id, :product_id, :geo_id, :dealer_id,
                      :stock_quantity, :stock_min_threshold, :stock_max_capacity,
                      :current_stock_qty, :inventory_qty, :reserved_qty, :available_qty,
                      :sales_qty, :stock_movement, :avg_monthly_sales, :sell_through_rate, :days_of_supply,
                      :warehouse_code, :is_rupture, :is_low_stock,
                      :stock_vs_min, :stock_vs_max, :has_zero_stock, :has_negative_stock,
                      :understock_risk, :overstock_risk,
                      :snapshot_date, :source_file, :etl_batch_id, :data_source,
                      :last_updated
                    )
                    ON CONFLICT (date_id, product_id, warehouse_code)
                    DO UPDATE SET
                      stock_quantity = EXCLUDED.stock_quantity,
                      current_stock_qty = EXCLUDED.current_stock_qty,
                      available_qty = EXCLUDED.available_qty,
                      reserved_qty = EXCLUDED.reserved_qty,
                      inventory_qty = EXCLUDED.inventory_qty,
                      avg_monthly_sales = EXCLUDED.avg_monthly_sales,
                      sell_through_rate = EXCLUDED.sell_through_rate,
                      days_of_supply = EXCLUDED.days_of_supply,
                      is_rupture = EXCLUDED.is_rupture,
                      is_low_stock = EXCLUDED.is_low_stock,
                      understock_risk = EXCLUDED.understock_risk,
                      overstock_risk = EXCLUDED.overstock_risk,
                      etl_batch_id = EXCLUDED.etl_batch_id,
                      last_updated = EXCLUDED.last_updated
                    """
                ),
                {
                    "date_id": date_id,
                    "product_id": product_id,
                    "geo_id": geo_id,
                    "dealer_id": dealer_id,
                    "stock_quantity": qty,
                    "stock_min_threshold": min_threshold,
                    "stock_max_capacity": max_capacity,
                    "current_stock_qty": current_stock,
                    "inventory_qty": inventory,
                    "reserved_qty": reserved,
                    "available_qty": available,
                    "sales_qty": _safe_int(record.get("SALES_QTY")),
                    "stock_movement": _safe_int(record.get("STOCK_MOVEMENT")),
                    "avg_monthly_sales": avg_monthly_sales,
                    "sell_through_rate": sell_through,
                    "days_of_supply": days_supply,
                    "warehouse_code": warehouse_code,
                    "is_rupture": qty <= 0,
                    "is_low_stock": qty < min_threshold,
                    "stock_vs_min": current_stock - min_threshold,
                    "stock_vs_max": current_stock - max_capacity,
                    "has_zero_stock": current_stock <= 0,
                    "has_negative_stock": current_stock < 0,
                    "understock_risk": bool(record.get("UNDERSTOCK_RISK")) if _safe_str(record.get("UNDERSTOCK_RISK")) else (current_stock < min_threshold),
                    "overstock_risk": bool(record.get("OVERSTOCK_RISK")) if _safe_str(record.get("OVERSTOCK_RISK")) else (current_stock > max_capacity),
                    "snapshot_date": snapshot_date,
                    "source_file": file_path.name,
                    "etl_batch_id": etl_batch_id,
                    "data_source": _safe_str(record.get("DATA_SOURCE")) or "CSV_IMPORT",
                    "last_updated": pd.Timestamp(datetime.utcnow()),
                },
            )
            inserted_rows += 1

        db.commit()
        return UploadResult(
            session_id=str(uuid.uuid4()),
            rows=int(len(work)),
            service=forced_service or "MULTI",
            period_start=str(work["SNAPSHOT_DATE"].min().date()),
            period_end=str(work["SNAPSHOT_DATE"].max().date()),
            preview=work.head(5).fillna("").to_dict(orient="records"),
            filename=file_path.name,
            file_type="stock",
            inserted_rows=inserted_rows,
            is_duplicate=False,
        )

    def _ingest_stock_5g(
        self,
        df: pd.DataFrame,
        file_path: Path,
        db: Session,
        forced_service: str | None,
    ) -> UploadResult:
        """Ingest 5G stock dataset (36M Tunisian governorate-level data).
        
        Handles:
        - Column mapping from 5G CSV to mart.fact_stock and related dimensions
        - Governorate resolution with geo_id lookup or creation
        - Data source flagging (REAL vs SIMULATED)
        - Additional columns: activations_qty, stock_opening_qty, flag_5g, product_type
        """
        original = df.copy()
        logger = logging.getLogger(__name__)
        
        # Normalize 5G frame
        work = _normalize_5g_stock_upload_frame(df)
        if work.empty:
            sample_vals = ""
            if "YEAR_MONTH" in original.columns:
                sample_vals = ", ".join(original["YEAR_MONTH"].astype(str).head(5).tolist())
            logger.error("No valid 5G stock rows after date parsing. Sample date values: %s", sample_vals)
            raise ValueError(f"No valid 5G stock rows after date parsing. Sample date values: {sample_vals}")

        etl_batch_id = str(uuid.uuid4())
        inserted_rows = 0
        failed_rows = 0

        for idx, row in work.iterrows():
            try:
                record = row.to_dict()
                snapshot_date = pd.to_datetime(record["SNAPSHOT_DATE"])
                date_id = self._ensure_date_dimension(db, snapshot_date)
                
                # Service code for 5G data is always 5G (or from forced_service)
                service_code = forced_service or "5G"
                service_id = self._ensure_service_dimension(db, service_code)
                
                # Product ID from COD_PROD
                product_id = _safe_str(record.get("PRODUCT_ID"))
                if not product_id:
                    continue
                
                # Ensure product dimension with 5G-specific fields
                try:
                    self._ensure_product_dimension_5g(db, record, service_id)
                except Exception as e:
                    logger.error(f"Row {idx}: Failed to upsert product dimension for '{product_id}': {e}")
                    db.rollback()
                    db.execute(text("SELECT 1"))
                    failed_rows += 1
                    continue

                # Resolve governorate to geo_id
                governorate = _safe_str(record.get("WAREHOUSE_KEY"))  # This is the GOVERNORATE value
                if not governorate:
                    logger.warning(f"Row {idx}: Missing governorate/WAREHOUSE_KEY, skipping")
                    failed_rows += 1
                    continue
                
                try:
                    geo_id = self._resolve_geo_id_5g(db, governorate)
                except Exception as e:
                    logger.error(f"Row {idx}: Failed to resolve geo_id for governorate '{governorate}': {e}")
                    # Rollback the failed geo resolution attempt and continue
                    db.rollback()
                    # Re-establish transaction by executing a dummy query
                    db.execute(text("SELECT 1"))
                    failed_rows += 1
                    continue
                
                # Extract quantities from 5G columns
                stock_qty = _safe_int(record.get("STOCK_QTY_VALUE"), 0)
                activations_qty = _safe_int(record.get("ACTIVATIONS_QTY"), 0)
                sales_qty = _safe_int(record.get("SALES_QTY"), 0)
                reserved_qty = _safe_int(record.get("RESERVED_QTY"), 0)
                inventory_qty = _safe_int(record.get("INVENTORY_QTY"), 0)
                stock_opening = _safe_int(record.get("STOCK_OPENING_QTY"), 0)
                
                # Price and metadata
                price = _safe_float(record.get("SELLING_PRICE_TTC"))
                product_type = _safe_str(record.get("PRODUCT_TYPE"))
                flag_5g = record.get("FLAG_5G", True)  # Boolean
                data_source = _safe_str(record.get("DATA_SOURCE", "REAL"))
                
                # Insert into fact_stock with 5G-specific columns
                db.execute(
                    text(
                        """
                        INSERT INTO mart.fact_stock
                        (
                          date_id, product_id, geo_id,
                          stock_quantity, current_stock_qty, inventory_qty, reserved_qty,
                          sales_qty, activations_qty, stock_opening_qty,
                          warehouse_code, flag_5g, product_type, data_source,
                          snapshot_date, source_file, etl_batch_id, last_updated
                        )
                        VALUES
                        (
                          :date_id, :product_id, :geo_id,
                          :stock_quantity, :current_stock_qty, :inventory_qty, :reserved_qty,
                          :sales_qty, :activations_qty, :stock_opening_qty,
                          :warehouse_code, :flag_5g, :product_type, :data_source,
                          :snapshot_date, :source_file, :etl_batch_id, :last_updated
                        )
                        ON CONFLICT (date_id, product_id, warehouse_code)
                        DO UPDATE SET
                          stock_quantity = EXCLUDED.stock_quantity,
                          current_stock_qty = EXCLUDED.current_stock_qty,
                          inventory_qty = EXCLUDED.inventory_qty,
                          reserved_qty = EXCLUDED.reserved_qty,
                          sales_qty = EXCLUDED.sales_qty,
                          activations_qty = EXCLUDED.activations_qty,
                          stock_opening_qty = EXCLUDED.stock_opening_qty,
                          flag_5g = EXCLUDED.flag_5g,
                          product_type = EXCLUDED.product_type,
                          data_source = EXCLUDED.data_source,
                          etl_batch_id = EXCLUDED.etl_batch_id,
                          last_updated = EXCLUDED.last_updated
                        """
                    ),
                    {
                        "date_id": date_id,
                        "product_id": product_id,
                        "geo_id": geo_id,
                        "stock_quantity": stock_qty,
                        "current_stock_qty": stock_qty,
                        "inventory_qty": inventory_qty,
                        "reserved_qty": reserved_qty,
                        "sales_qty": sales_qty,
                        "activations_qty": activations_qty,
                        "stock_opening_qty": stock_opening,
                        "warehouse_code": governorate,
                        "flag_5g": flag_5g,
                        "product_type": product_type,
                        "data_source": data_source,
                        "snapshot_date": snapshot_date,
                        "source_file": file_path.name,
                        "etl_batch_id": etl_batch_id,
                        "last_updated": pd.Timestamp(datetime.utcnow()),
                    },
                )
                inserted_rows += 1
            except Exception as e:
                logger.error(f"Row {idx}: Error processing 5G stock record: {e}")
                failed_rows += 1
                try:
                    db.rollback()
                    # Re-establish transaction
                    db.execute(text("SELECT 1"))
                except:
                    pass

        try:
            db.commit()
        except Exception as e:
            logger.error(f"Failed to commit 5G stock ingestion: {e}")
            db.rollback()
            raise ValueError(f"Failed to commit transaction: {e}. Successfully inserted {inserted_rows} rows before failure.")
        return UploadResult(
            session_id=str(uuid.uuid4()),
            rows=int(len(work)),
            service=forced_service or "5G",
            period_start=str(work["SNAPSHOT_DATE"].min().date()),
            period_end=str(work["SNAPSHOT_DATE"].max().date()),
            preview=work.head(5).fillna("").to_dict(orient="records"),
            filename=file_path.name,
            file_type="stock_5g",
            inserted_rows=inserted_rows,
            is_duplicate=False,
        )

    def _resolve_geo_id_5g(self, db: Session, governorate: str) -> int:
        """Resolve governorate name to geo_id, creating a new geo record if needed.
        
        The 5G dataset uses clean Tunisian governorate names (e.g., 'Tunis', 'Sfax').
        This function looks up the governorate in dim_geographie and creates a new
        entry if it doesn't exist, ensuring every governorate has a valid geo_id.
        """
        gov_clean = governorate.strip()
        if not gov_clean:
            gov_clean = "UNKNOWN"
        
        # Try to find existing governorate (case-insensitive)
        row = db.execute(
            text(
                """
                SELECT geo_id FROM mart.dim_geographie
                WHERE LOWER(TRIM(COALESCE(governorate, ''))) = LOWER(:gov)
                LIMIT 1
                """
            ),
            {"gov": gov_clean},
        ).fetchone()
        
        if row:
            return int(row[0])
        
        # Governorate not found; create a new one
        db.execute(
            text(
                """
                INSERT INTO mart.dim_geographie 
                (governorate, governorate_normalized)
                VALUES (:gov, :gov_normalized)
                """
            ),
            {
                "gov": gov_clean,
                "gov_normalized": gov_clean.lower(),
            },
        )
        db.flush()  # Ensure the row is created before selecting it
        
        # Now fetch the newly created geo_id
        row = db.execute(
            text(
                """
                SELECT geo_id FROM mart.dim_geographie
                WHERE LOWER(TRIM(governorate)) = LOWER(:gov)
                LIMIT 1
                """
            ),
            {"gov": gov_clean},
        ).fetchone()
        
        if not row:
            raise ValueError(f"Failed to resolve or create geo_id for governorate: {gov_clean}")
        
        return int(row[0])

    @staticmethod
    def _ensure_product_dimension_5g(db: Session, record: dict[str, Any], service_id: int) -> None:
        """Ensure product exists in dim_products with 5G-specific attributes.
        
        Enriches the product dimension with:
        - type_prod: SUBSCRIPTION | CPE_HARDWARE | SMARTPHONE_HW
        - cod_group: product group code
        - flag_5g: boolean indicating 5G-flagged product
        """
        product_id = _safe_str(record.get("PRODUCT_ID"))
        if not product_id:
            return
        
        product_name = _safe_str(record.get("PRODUCT_NAME")) or product_id
        product_family = _safe_str(record.get("PRODUCT_FAMILY")) or "UNKNOWN"
        product_type = _safe_str(record.get("PRODUCT_TYPE")) or "UNKNOWN"
        cod_group = _safe_str(record.get("PRODUCT_GROUP")) or ""
        flag_5g = record.get("FLAG_5G", True)
        is_active = record.get("FLAG_ACTIVE", True)
        price = _safe_float(record.get("SELLING_PRICE_TTC"))
        
        db.execute(
            text(
                """
                INSERT INTO mart.dim_products
                (product_id, product_name, product_family, service_id,
                 type_prod, cod_group, flag_5g, is_active, price)
                VALUES
                (:product_id, :product_name, :product_family, :service_id,
                 :type_prod, :cod_group, :flag_5g, :is_active, :price)
                ON CONFLICT (product_id) DO UPDATE SET
                  product_name = COALESCE(EXCLUDED.product_name, mart.dim_products.product_name),
                  product_family = COALESCE(EXCLUDED.product_family, mart.dim_products.product_family),
                  type_prod = COALESCE(EXCLUDED.type_prod, mart.dim_products.type_prod),
                  cod_group = COALESCE(EXCLUDED.cod_group, mart.dim_products.cod_group),
                  flag_5g = COALESCE(EXCLUDED.flag_5g, mart.dim_products.flag_5g),
                  price = COALESCE(EXCLUDED.price, mart.dim_products.price)
                """
            ),
            {
                "product_id": product_id,
                "product_name": product_name,
                "product_family": product_family,
                "service_id": service_id,
                "type_prod": product_type,
                "cod_group": cod_group,
                "flag_5g": flag_5g,
                "is_active": is_active,
                "price": price,
            },
        )

    @staticmethod
    def _ensure_date_dimension(db: Session, dt: datetime) -> int:
        row = db.execute(text("SELECT mart.get_or_create_date_id(:input_date)"), {"input_date": dt.date()}).fetchone()
        return int(row[0])

    @staticmethod
    def _ensure_geo_dimension(db: Session, record: dict[str, Any]) -> int | None:
        city = _safe_str(record.get("CITY")) or None
        governorate = _safe_str(record.get("GOVERNORATE")) or None
        delegation = _safe_str(record.get("DELEGATION")) or _safe_str(record.get("DELEGATION_NAME")) or None
        if not city and not governorate:
            return None

        row = db.execute(
            text(
                """
                SELECT mart.get_or_create_geo_id(
                    :city, :governorate, :delegation, :locality, :postal_code, :latitude, :longitude
                )
                """
            ),
            {
                "city": city,
                "governorate": governorate,
                "delegation": delegation,
                "locality": _safe_str(record.get("LOCALITY")) or _safe_str(record.get("LOCALITY_NAME")) or None,
                "postal_code": _safe_str(record.get("POSTAL_CODE")) or None,
                "latitude": float(record.get("LATITUDE")) if _safe_str(record.get("LATITUDE")) else None,
                "longitude": float(record.get("LONGITUDE")) if _safe_str(record.get("LONGITUDE")) else None,
            },
        ).fetchone()
        return int(row[0]) if row else None

    @staticmethod
    def _ensure_service_dimension(db: Session, service_code: str) -> int:
        code = service_code.upper().strip()
        row = db.execute(text("SELECT service_id FROM mart.dim_services WHERE service_code = :code"), {"code": code}).fetchone()
        if row:
            return int(row[0])

        db.execute(
            text(
                """
                INSERT INTO mart.dim_services (service_code, service_name, service_category, is_active)
                VALUES (:code, :name, 'connectivity', true)
                ON CONFLICT (service_code) DO NOTHING
                """
            ),
            {"code": code, "name": code},
        )
        row = db.execute(text("SELECT service_id FROM mart.dim_services WHERE service_code = :code"), {"code": code}).fetchone()
        if not row:
            raise ValueError(f"Unable to resolve service_id for {code}")
        return int(row[0])

    @staticmethod
    def _ensure_offer_dimension(db: Session, record: dict[str, Any], service_id: int) -> int | None:
        offer_code = _safe_str(record.get("OFFRE") or record.get("OFFRE_CODE") or record.get("PLAN_CODE"))
        if not offer_code:
            return None

        db.execute(
            text(
                """
                INSERT INTO mart.dim_offres (offre_code, offre_name, service_id, debit, price, category, is_active)
                VALUES (:offre_code, :offre_name, :service_id, :debit, :price, :category, true)
                ON CONFLICT (offre_code) DO NOTHING
                """
            ),
            {
                "offre_code": offer_code,
                "offre_name": _safe_str(record.get("OFFRE_NAME")) or offer_code,
                "service_id": service_id,
                "debit": _safe_str(record.get("DEBIT")) or None,
                "price": float(record.get("PRIX") or 0) if _safe_str(record.get("PRIX")) else None,
                "category": _safe_str(record.get("CATEGORY")) or "standard",
            },
        )
        row = db.execute(text("SELECT offre_id FROM mart.dim_offres WHERE offre_code=:offre_code"), {"offre_code": offer_code}).fetchone()
        return int(row[0]) if row else None

    @staticmethod
    def _ensure_product_dimension(db: Session, record: dict[str, Any], service_id: int) -> str | None:
        """Extract product info from OFFRE/DEBIT or explicit product fields."""
        # Priority: OFFRE > DEBIT > COD_PROD > PRODUCT_ID > SKU > ITEM_CODE
        # This ensures offers are used, but also accepts stock data identifiers like COD_PROD
        offre = _safe_str(record.get("OFFRE") or "")
        debit = _safe_str(record.get("DEBIT") or "")
        product_id = offre or debit or _safe_str(record.get("COD_PROD") or record.get("PRODUCT_ID") or record.get("SKU") or record.get("ITEM_CODE") or "")
        
        if not product_id:
            return None
        
        # Derive product category from OFFRE/DEBIT/explicit field
        category = _safe_str(record.get("PRODUCT_CATEGORY") or "")
        if not category:
            # Auto-classify by offer/service type
            text_blob = f"{offre} {debit}".upper()
            if any(x in text_blob for x in ["PREMIUM", "PRO", "BUSINESS", "ENTREPRISE"]):
                category = "Premium"
            elif any(x in text_blob for x in ["500", "1000", "2000", "HAUT", "HIGH"]):
                category = "High-Speed"
            elif any(x in text_blob for x in ["100", "200", "300", "STANDARD", "REGULAR"]):
                category = "Standard"
            else:
                category = "Service"
        
        # Insert/update product dimension row with enriched stock fields
        try:
            db.execute(
                text(
                    """
                    INSERT INTO mart.dim_products 
                    (product_id, product_name, product_category, service_id, price, is_active,
                     sku, cod_prod, product_line, cost_price_ht, selling_price_ttc, vat_rate,
                     flag_fibre, is_sellable, is_deliverable, is_eol, product_family)
                    VALUES (:product_id, :product_name, :product_category, :service_id, :price, true,
                            :sku, :cod_prod, :product_line, :cost_price_ht, :selling_price_ttc, :vat_rate,
                            :flag_fibre, :is_sellable, :is_deliverable, :is_eol, :product_family)
                    ON CONFLICT (product_id) DO UPDATE SET
                      product_name = COALESCE(EXCLUDED.product_name, mart.dim_products.product_name),
                      cost_price_ht = COALESCE(EXCLUDED.cost_price_ht, mart.dim_products.cost_price_ht),
                      selling_price_ttc = COALESCE(EXCLUDED.selling_price_ttc, mart.dim_products.selling_price_ttc),
                      vat_rate = COALESCE(EXCLUDED.vat_rate, mart.dim_products.vat_rate),
                      flag_fibre = COALESCE(EXCLUDED.flag_fibre, mart.dim_products.flag_fibre),
                      is_sellable = COALESCE(EXCLUDED.is_sellable, mart.dim_products.is_sellable),
                      is_deliverable = COALESCE(EXCLUDED.is_deliverable, mart.dim_products.is_deliverable),
                      is_eol = COALESCE(EXCLUDED.is_eol, mart.dim_products.is_eol),
                      product_family = COALESCE(EXCLUDED.product_family, mart.dim_products.product_family)
                    """
                ),
                {
                    "product_id": product_id,
                    "product_name": _safe_str(record.get("PRODUCT_NAME")) or product_id,
                    "product_category": category,
                    "service_id": service_id,
                    "price": float(record.get("SELLING_PRICE_TTC") or record.get("PRODUCT_PRICE") or 0) if _safe_str(record.get("SELLING_PRICE_TTC") or record.get("PRODUCT_PRICE")) else None,
                    "sku": _safe_str(record.get("SKU") or record.get("COD_PROD")) or None,
                    "cod_prod": _safe_str(record.get("COD_PROD")) or None,
                    "product_line": _safe_str(record.get("PRODUCT_LINE")) or None,
                    "cost_price_ht": _safe_float(record.get("COST_PRICE_HT")),
                    "selling_price_ttc": _safe_float(record.get("SELLING_PRICE_TTC")),
                    "vat_rate": _safe_float(record.get("VAT_RATE")),
                    "flag_fibre": _safe_bool(record.get("FLAG_FIBRE")),
                    "is_sellable": _safe_bool(record.get("IS_SELLABLE"), True),
                    "is_deliverable": _safe_bool(record.get("IS_DELIVERABLE"), True),
                    "is_eol": _safe_bool(record.get("IS_EOL")),
                    "product_family": _safe_str(record.get("PRODUCT_FAMILY")) or None,
                },
            )
        except Exception as e:
            # If insertion fails, log but still return product_id so fact table references it
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to insert/update product dimension for {product_id}: {e}")
        
        return product_id

    @staticmethod
    def _ensure_dealer_dimension(db: Session, dealer_id: str, geo_id: int | None) -> str | None:
        clean = dealer_id.strip()
        if not clean:
            return None

        db.execute(
            text(
                """
                INSERT INTO mart.dim_dealers (dealer_id, dealer_name, dealer_type, activation_date, is_active, geo_id)
                VALUES (:dealer_id, :dealer_name, :dealer_type, CURRENT_DATE, true, :geo_id)
                ON CONFLICT (dealer_id)
                DO UPDATE SET
                  dealer_name = COALESCE(EXCLUDED.dealer_name, mart.dim_dealers.dealer_name),
                  geo_id = COALESCE(EXCLUDED.geo_id, mart.dim_dealers.geo_id)
                """
            ),
            {
                "dealer_id": clean,
                "dealer_name": clean,
                "dealer_type": "retail",
                "geo_id": geo_id,
            },
        )
        return clean

    @staticmethod
    def _ensure_promo_dimension(
        db: Session,
        record: dict[str, Any],
        service_id: int,
        geo_id: int | None,
    ) -> int | None:
        promo_code = _safe_str(record.get("PROMO_CODE") or record.get("PROMOTION_CODE") or record.get("PROMO"))
        if not promo_code:
            return None

        start_date = pd.to_datetime(record.get("PROMO_START") or datetime.utcnow().date(), errors="coerce")
        end_date = pd.to_datetime(record.get("PROMO_END") or datetime.utcnow().date(), errors="coerce")
        if pd.isna(start_date):
            start_date = pd.Timestamp(datetime.utcnow().date())
        if pd.isna(end_date):
            end_date = start_date

        db.execute(
            text(
                """
                INSERT INTO mart.dim_promotions
                (
                    promo_code, promo_name, promo_type, discount_pct,
                    date_debut, date_fin, service_id, geo_id, description
                )
                VALUES
                (
                    :promo_code, :promo_name, :promo_type, :discount_pct,
                    :date_debut, :date_fin, :service_id, :geo_id, :description
                )
                ON CONFLICT (promo_code) DO NOTHING
                """
            ),
            {
                "promo_code": promo_code,
                "promo_name": _safe_str(record.get("PROMO_NAME")) or promo_code,
                "promo_type": _safe_str(record.get("PROMO_TYPE")) or "discount",
                "discount_pct": float(record.get("DISCOUNT_PCT") or 0) if _safe_str(record.get("DISCOUNT_PCT")) else None,
                "date_debut": start_date.date(),
                "date_fin": end_date.date(),
                "service_id": service_id,
                "geo_id": geo_id,
                "description": _safe_str(record.get("PROMO_DESC")) or None,
            },
        )
        row = db.execute(text("SELECT promo_id FROM mart.dim_promotions WHERE promo_code=:promo_code"), {"promo_code": promo_code}).fetchone()
        return int(row[0]) if row else None


etl_service = ETLService()


def process_upload(file_path: Path, db: Session, forced_service: str | None = None, replace_stock: bool = False) -> UploadResult:
    return etl_service.process_upload(file_path, db, forced_service, replace_stock)
