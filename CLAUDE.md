# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案簡介

電腦輔助工程課程期末專案：**智慧營建管理系統 - 簡易版 (Smart Construction Manager - Lite)**。
單一檔案 `app.py`，使用 Streamlit 作為網頁框架，SQLite3 作為本地資料庫。

## 專案檔案結構

```
電腦輔助工程/
├── app.py                  主程式（唯一程式碼檔案）
├── start.bat               雙擊即可啟動應用程式（Windows）
├── requirements.txt        Python 依賴套件清單
├── construction.db         SQLite 資料庫（首次啟動自動產生）
└── CLAUDE.md               本文件
```

系統設定檔（使用者層級，非專案目錄內）：
```
C:\Users\<使用者>\.streamlit\credentials.toml   略過 Streamlit Email 提示
```

## 啟動方式

### 一般使用（雙擊啟動）

直接在檔案總管中**雙擊 `start.bat`**，瀏覽器會自動開啟 `http://localhost:8501`。

### 開發 / 指令列啟動

```bash
# 安裝依賴（首次或更新套件時執行）
pip install -r requirements.txt

# 啟動應用程式
streamlit run app.py
```

> **注意**：`~/.streamlit/credentials.toml` 已設定 `email = ""`，啟動時不會出現 Email 輸入提示。

## 測試帳號

| 帳號  | 密碼     | 角色  | 說明 |
|-------|----------|-------|------|
| admin | admin123 | admin | 後台管理員，可管理資料與材料類型 |
| site1 | site123  | user  | 現場人員，可新增日誌與查詢紀錄 |

## 功能總覽

### 現場人員介面（role == user）

**📝 新增日誌**
- 展開「建立新材料類型」區塊可即時新增材料，成功後顯示 toast 通知並自動收合
- 填寫施工日期（日期選擇器）與出勤人數
- 動態選擇材料種類與消耗量，可加入多筆或移除
- 送出日誌後清空暫存清單，同日期只能有一筆紀錄（防重複）

**🔍 歷史查詢**
- **單日查詢**：選擇特定日期，顯示當日出勤人數與所有材料消耗明細
- **時間範圍查詢**：選擇起訖日期，以表格顯示該時間段內所有紀錄（含材料消耗彙整欄位）

**📊 視覺化報表**
- 出勤人數趨勢折線圖（含填色面積）
- 建材消耗堆疊長條圖（材料類型動態對應欄位）

---

### 管理員介面（role == admin）

**🗂️ 全域資料管理**
- 以表格檢視所有施工紀錄（含動態材料消耗欄位）
- 依 Record ID 刪除施工紀錄（相關材料消耗資料 CASCADE 同步刪除，刪後自動重排 ID）
- 一鍵重新整理所有紀錄 ID（修復舊有跳號缺口，依施工日期重新排序）

**🧱 材料類型管理**
- 檢視現有材料清單（ID、名稱、單位）
- 新增材料類型（名稱 + 計量單位，名稱重複會拒絕）
- 刪除材料類型（相關施工紀錄中的消耗資料 CASCADE 同步移除）

**📤 資料匯出**
- **全部匯出**：下載含所有紀錄與動態材料欄的 CSV 檔
- **單筆匯出**：下拉選擇特定紀錄，預覽後下載該單筆 CSV 檔
- 檔案編碼為 `utf-8-sig`（含 BOM），可直接以 Excel 開啟且中文正常顯示

---

## 程式架構

### 類別設計 (OOP)

```
ConstructionRecord                  <- 資料載體 (Data Object)
    屬性: record_id, date, workers
    （材料消耗由 record_materials 關聯表管理，不存於此類別）

ProjectManager                      <- 資料庫控制器 (Controller)
    ── 初始化 ──
    __init__()                      初始化資料庫並修復既有 ID 缺口
    _init_database()                建立資料表、schema 遷移、植入預設帳號
    _get_connection()               建立 SQLite 連線（啟用外鍵約束）

    ── 使用者 ──
    authenticate(username, password)            驗證帳號密碼，回傳使用者資訊或 None

    ── 材料類型目錄 ──
    add_material(name, unit)                    新增材料類型（UNIQUE 防重複）
    get_all_materials()                         讀取全部材料 → pd.DataFrame
    delete_material(material_id)                刪除材料（CASCADE 清除消耗紀錄）

    ── 施工紀錄 CRUD ──
    add_record(record, material_quantities)     新增日誌 + 材料消耗量
    get_record_by_date(date)                    依日期查詢單筆（含材料明細）
    get_all_records()                           讀取全部基本欄位 → pd.DataFrame
    get_all_records_with_materials()            讀取全部 + 材料 Pivot → pd.DataFrame
    get_records_by_date_range(start, end)       依日期範圍查詢 + 材料 Pivot

    ── 紀錄 ID 管理 ──
    _reorder_record_ids()           私有：依施工日期重排所有 record_id（兩段式負數暫存法）
    reorder_record_ids()            公開：手動觸發重排，回傳 (bool, str)
    delete_record(record_id)        刪除紀錄（CASCADE）+ 自動重排 ID
```

### Streamlit 畫面流程

```
session_state["logged_in"]
    False → render_login()

    True, role=="user"  → render_user_interface()
        📝 新增日誌
            ├─ 建立新材料類型（expander，成功後 toast 通知並自動收合）
            ├─ 填寫施工日期 / 出勤人數
            ├─ 動態加入 / 移除材料消耗量
            └─ 送出日誌

        🔍 歷史查詢
            ├─ 📅 單日查詢（含材料消耗明細）
            └─ 📆 時間範圍查詢（含材料消耗彙整表）

        📊 視覺化報表
            ├─ 出勤人數趨勢折線圖
            └─ 建材消耗堆疊長條圖（動態材料欄）

    True, role=="admin" → render_admin_interface()
        🗂️ 全域資料管理
            ├─ 檢視所有施工紀錄（含動態材料欄）
            ├─ 依 record_id 刪除紀錄（刪後自動重排 ID）
            └─ 一鍵重新整理所有紀錄 ID

        🧱 材料類型管理
            ├─ 檢視現有材料清單
            ├─ 新增材料（名稱 + 計量單位）
            └─ 刪除材料（CASCADE 同步移除相關消耗資料）

        📤 資料匯出
            ├─ 📦 全部匯出（所有紀錄 CSV）
            └─ 📋 單筆匯出（下拉選擇特定紀錄後下載）
```

`get_manager()` 以 `@st.cache_resource` 快取，確保整個 session 只建立一個 `ProjectManager` 實例。程式碼更新後需清除快取（Streamlit 選單 → Clear cache）或重啟伺服器。

### 資料庫 Schema（v2）

```sql
users (
    user_id  INTEGER PK AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT,
    role     TEXT  -- 'admin' | 'user'
)

construction_records (
    record_id     INTEGER PK AUTOINCREMENT,
    record_date   TEXT UNIQUE,   -- 格式 YYYY-MM-DD
    workers_count INTEGER
)

materials (
    material_id   INTEGER PK AUTOINCREMENT,
    material_name TEXT UNIQUE,
    unit          TEXT DEFAULT '噸'
)

record_materials (
    id          INTEGER PK AUTOINCREMENT,
    record_id   INTEGER → construction_records ON DELETE CASCADE,
    material_id INTEGER → materials            ON DELETE CASCADE,
    quantity    REAL,
    UNIQUE(record_id, material_id)
)
```

舊版 schema（含 `cement_used` / `steel_used` 欄位）會在首次啟動時自動遷移至新版，歷史資料不會遺失。

### 紀錄 ID 重排機制

刪除施工紀錄後（或手動觸發），`_reorder_record_ids()` 會：
1. 關閉外鍵約束（`PRAGMA foreign_keys = OFF`）
2. 依 `record_date` 升序取得所有 `record_id`
3. **第一輪**：需更動的舊 ID → 負數暫存 ID（避免 UNIQUE 衝突）
4. **第二輪**：負數暫存 ID → 正數最終 ID（1, 2, 3, ...）
5. 同步更新 `record_materials` 子表與 `sqlite_sequence` 計數器
6. 應用程式啟動時（`__init__`）亦自動執行一次，修復既有缺口

## 注意事項

- **啟動方式**：雙擊 `start.bat` 即可，無需手動開啟終端機。
- **Matplotlib 中文字體**：程式啟動時嘗試套用 `Microsoft JhengHei`；若環境無此字體，圖表標籤自動退回英文，不會報錯。
- **資料庫位置**：`construction.db` 產生在執行 `streamlit run` 的工作目錄，若移動執行位置會建立新的空資料庫。
- **密碼儲存**：目前使用明碼，僅供課程示範用途。
- **CSV 編碼**：匯出使用 `utf-8-sig`（含 BOM），確保 Excel 開啟時中文欄位正常顯示。
- **快取行為**：`@st.cache_resource` 於程式碼更新後需清除快取或重啟才會載入新版 `ProjectManager`。
