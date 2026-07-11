# Fibre Forecast System — Technical Diagrams

---

## UC1 — Sales Forecasting and Result Explanation

```mermaid
flowchart LR
    A["CSV Upload"] --> B["Parse and validate"]
    B --> C["Clean: duplicates, nulls, outliers"]
    C --> D["mart.fact_ventes"]
    D --> E["Train all models in parallel<br/>SARIMA / Prophet / XGBoost / LSTM<br/>LinearRegression / ensemble / Chronos"]
    E --> F["Cross-validation<br/>WAPE - RMSE - SMAPE"]
    F --> G["Best model selected"]
    G --> H["MLflow: scores and artifacts"]
    G --> I["generate_forecast<br/>point estimate + CI, 1-24 months"]
    I --> J["Top-3 model results"]
    J --> K["Ollama bge-m3: embed context"]
    K --> L["Milvus dense search"]
    K --> M["PostgreSQL FTS French"]
    L --> N["RRF Fusion"]
    M --> N
    N --> O["Context: top-3 forecasts + RAG chunks"]
    J --> O
    O --> P["Ollama llama3.1:8b<br/>Why these values? Which drivers?"]
    P --> Q["Chart and Narrative returned to UI"]
    J --> Q
```

---

## UC2 — Anomaly Detection and Explanation

```mermaid
flowchart LR
    A["User: select service and dates"] --> B["mart.fact_ventes + dim_services"]
    B --> C["IQR method: flag if outside 1.5x IQR"]
    B --> D["Z-score: flag if z greater than 3"]
    C --> E["Anomaly confirmed if BOTH agree"]
    D --> E
    E --> F["Anomaly list: id, date, product, value"]
    F --> G["Check cache: anomaly_explanations"]
    G -->|"HIT"| H["Return cached explanation"]
    G -->|"MISS"| I["Ollama bge-m3: embed anomaly context"]
    I --> J["Milvus dense search top-5"]
    I --> K["PostgreSQL FTS French top-5"]
    J --> L["RRF Fusion"]
    K --> L
    L --> M["Context: metadata + RAG chunks + baseline"]
    M --> N["Ollama llama3.1:8b<br/>Root cause, factors, action"]
    N --> O["Persist to anomaly_explanations"]
    O --> P["Explanation returned to UI"]
    H --> P
```

---

## UC3 — Stock and Inventory Forecasting

```mermaid
flowchart LR
    A["User: select service type and scope"] --> B["INNER JOIN dim_services<br/>FIBRE / 5G / DATA_BUNDLE / VOD"]
    B --> C["mart.fact_stock + dim_products"]
    C --> D["Monthly stock history per SKU per region"]
    D --> E["Train all models in parallel<br/>SARIMA / Prophet / XGBoost / LSTM / Chronos"]
    E --> F["Cross-validation: WAPE - RMSE - SMAPE"]
    F --> G["Best model per SKU cached by service type"]
    G --> H["MLflow: model registry"]
    G --> I["generate_inventory_forecast<br/>point + CI, per SKU per region"]
    I --> J["Forecast chart returned to UI"]
```

---

## UC4 — Stock Recommendations

```mermaid
flowchart LR
    A["User: product and region list"] --> B["load_thresholds_for_products<br/>bulk fetch from sku_thresholds"]
    B --> C["sku_thresholds: min and max per SKU"]
    B --> D["mart.fact_stock: demand stats"]
    C --> E["Override logic<br/>request value beats DB beats computed"]
    D --> E
    E --> F["Safety stock = avg demand x lead time x factor"]
    F --> G["Reorder point = safety stock + avg demand x lead time"]
    G --> H["Action: REORDER / TRANSFER / HOLD / REDUCE"]
    H --> I["Transfer analysis: find donor regions"]
    I --> R["Recommendations returned to UI"]
    I --> J["Ollama bge-m3: embed context<br/>only on rag endpoint"]
    J --> K["Milvus dense search"]
    J --> L["PostgreSQL FTS French"]
    K --> M["RRF Fusion"]
    L --> M
    M --> N["Ollama llama3.1:8b: Why this action?"]
    N --> R
```

---

## UC5 — Promo What-If Simulation

```mermaid
flowchart LR
    A["User: product, channel, discount, dates"] --> B["mart.fact_stock: current stock"]
    A --> C["mart.fact_promotions: similar campaigns"]
    A --> D["mart.promo_elasticity: price-response"]
    B --> E["Uplift = base demand x elasticity x channel factor"]
    C --> E
    D --> E
    E --> F["Does current stock cover projected uplift?"]
    F -->|"yes"| G["Simulation result: uplift and revenue"]
    F -->|"no"| H["Stock rupture warning added"]
    H --> G
    G --> I["Save to whatif_scenarios"]
    G --> J["Mirror to fact_promotions for future lookups"]
```

---

## UC6 — Forecast Q and A Chatbot

```mermaid
flowchart LR
    A["User types a question"] --> B["Intent detection: keyword scan<br/>stock / vente / prevision / procedure"]
    B -->|"stock or vente"| C["mart.fact_stock and fact_ventes<br/>live KPI snapshot injected"]
    A --> D["Ollama bge-m3: embed question"]
    D --> E["Milvus cosine search top-8"]
    D --> F["PostgreSQL FTS French top-8"]
    E --> G["RRF Fusion: normalized score"]
    F --> G
    G -->|"confidence too low"| H["Return: Insufficient context"]
    G -->|"confidence ok"| I["Context: top-3 chunks + live KPIs"]
    C --> I
    I --> J["Ollama llama3.1:8b: grounded answer"]
    J --> K["Answer and sources returned to UI"]
    H --> K
```

---

## UC7 — Knowledge Base Management

```mermaid
flowchart LR
    A["Document: PDF / DOCX / TXT"] --> B["Text extraction<br/>pypdf / python-docx / direct read"]
    B --> C["Chunking via str.split<br/>320 tokens window, 64 overlap<br/>Unicode-safe for French accents"]
    C --> D["Ollama bge-m3: 1024-dim vector per chunk"]
    D --> E["Milvus IVF_FLAT cosine index<br/>enables dense search"]
    D --> F["PostgreSQL rag_chunks<br/>text + tsvector, French FTS"]
    F --> G["In-memory cosine store<br/>hydrated from PostgreSQL at startup"]
    E --> H["GET knowledge status: doc and chunk count"]
    F --> H
```

---

## UC8 — Per-SKU Threshold Management

```mermaid
flowchart LR
    A["Analyst: set thresholds via UI"] --> B["PUT: set min and max for SKU and region"]
    A --> C["GET: list all stored thresholds"]
    A --> D["DELETE: remove override"]
    B --> E["public.sku_thresholds<br/>PK: product_id + governorate"]
    C --> E
    D --> E
    F["App startup: ensure_sku_thresholds_table<br/>CREATE TABLE IF NOT EXISTS"] --> E
    E --> G["load_thresholds_for_products<br/>bulk SELECT at recommendation time"]
    G -->|"request has value"| H["Use request value: highest priority"]
    G -->|"DB has value"| I["Use stored DB threshold"]
    G -->|"nothing stored"| J["Use computed default"]
    H --> K["StockRecommendationEngine"]
    I --> K
    J --> K
    K --> L["Recommendation with correct thresholds"]
```
