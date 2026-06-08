# =============================================================
# 智慧營建管理系統 - 簡易版 (Smart Construction Manager - Lite)
# 課程：電腦輔助工程  期末專案
# 技術棧：Streamlit + SQLite3 + Pandas + Matplotlib
# 版本 3.0：多專案管理（projects 資料表 + 專案選擇介面）
# =============================================================

import streamlit as st
import sqlite3
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import glob
import os
from datetime import datetime, date, timedelta

# ------------------------------------------------------------
# Matplotlib 中文字體設定（跨平台）
# Windows: Microsoft JhengHei / SimHei
# macOS  : PingFang TC / Heiti TC
# Linux / Streamlit Cloud: Noto Sans CJK（需 packages.txt 安裝）
# ------------------------------------------------------------
def _setup_chinese_font():
    matplotlib.rcParams["axes.unicode_minus"] = False

    candidates = [
        "Microsoft JhengHei",   # Windows 繁體
        "Microsoft YaHei",      # Windows 簡體
        "SimHei",               # Windows 黑體
        "PingFang TC",          # macOS 繁體
        "Heiti TC",             # macOS 黑體
        "Noto Sans CJK TC",     # Linux / Streamlit Cloud
        "Noto Sans CJK SC",     # Linux 簡體
        "Noto Sans CJK JP",     # Linux 日文（含漢字）
        "Noto Sans CJK",        # Linux 通用
        "WenQuanYi Micro Hei",  # Linux 文泉驛
        "Droid Sans Fallback",  # 備援
    ]

    available = {f.name for f in fm.fontManager.ttflist}

    for font in candidates:
        if font in available:
            matplotlib.rcParams["font.family"] = [font, "sans-serif"]
            return

    # 若快取中無任何 CJK 字體，清除快取後重建再試一次
    cjk_kw = {"CJK", "Hei", "Ming", "Noto", "WenQuanYi", "JhengHei", "YaHei", "PingFang"}
    if not any(any(kw in f.name for kw in cjk_kw) for f in fm.fontManager.ttflist):
        try:
            for cache in glob.glob(os.path.join(matplotlib.get_cachedir(), "fontlist*.json")):
                os.remove(cache)
            fm.fontManager = fm.FontManager()
            available = {f.name for f in fm.fontManager.ttflist}
            for font in candidates:
                if font in available:
                    matplotlib.rcParams["font.family"] = [font, "sans-serif"]
                    return
        except Exception:
            pass

    # 最後手段：模糊搜尋字體清單
    for f in fm.fontManager.ttflist:
        if any(kw in f.name for kw in ["CJK", "Hei", "Ming", "WenQuanYi"]):
            matplotlib.rcParams["font.family"] = [f.name, "sans-serif"]
            return

_setup_chinese_font()

DB_NAME = "construction.db"


# ==============================================================
# 類別一：ConstructionRecord — 施工紀錄資料載體
# （v2 起移除硬編碼的 cement/steel，材料由 record_materials 表管理）
# ==============================================================
class ConstructionRecord:
    """
    封裝單筆施工日誌的基本欄位。
    材料消耗明細改以 record_materials 關聯表儲存，
    此類別僅保留日期與出勤人數兩個核心屬性。
    """

    def __init__(self, date: str, workers: int, record_id: int = None):
        self.record_id = record_id  # 資料庫主鍵（新增時可省略）
        self.date = date            # 施工日期，格式 YYYY-MM-DD
        self.workers = workers      # 當日出勤人數

    def __repr__(self) -> str:
        return (
            f"ConstructionRecord(id={self.record_id}, "
            f"date={self.date}, workers={self.workers})"
        )


# ==============================================================
# 類別二：ProjectManager — 資料庫控制器
# ==============================================================
class ProjectManager:
    """
    負責所有 SQLite 操作的控制器。
    管理範圍：使用者驗證、材料類型目錄、施工紀錄 CRUD。

    資料庫 Schema（v2）：
        users              ─ 使用者帳號（不變）
        construction_records ─ 施工日誌主表（移除 cement/steel 欄位）
        materials          ─ 材料類型目錄（新增）
        record_materials   ─ 紀錄與材料的多對多關聯（新增）
    """

    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self._init_database()
        self._reorder_record_ids()  # 啟動時修復任何舊有 ID 缺口

    # ----------------------------------------------------------
    # 私有：取得資料庫連線
    # ----------------------------------------------------------
    def _get_connection(self) -> sqlite3.Connection:
        """
        建立 SQLite 連線，並啟用外鍵約束（支援 CASCADE DELETE）。
        row_factory 讓查詢結果可用欄位名稱存取。
        """
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")  # 每次連線都需重新啟用
        return conn

    # ----------------------------------------------------------
    # 私有：初始化與遷移資料庫
    # ----------------------------------------------------------
    def _init_database(self):
        """
        程式啟動時自動執行：
        1. 建立/保留所有資料表（v3：新增 projects 資料表）
        2. v2→v3 migration：重建 construction_records 以加入 project_id
           並將 UNIQUE(record_date) 改為 UNIQUE(project_id, record_date)
        3. v1→v2 migration：舊版 cement/steel 欄位轉入 record_materials
        4. 植入預設帳號
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = OFF")

            # ── 使用者資料表（不變）──────────────────────────────────
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    role     TEXT NOT NULL CHECK(role IN ('admin', 'user'))
                )
            """)

            # ── projects（v3 新增）──────────────────────────────────
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name TEXT UNIQUE NOT NULL,
                    description  TEXT NOT NULL DEFAULT '',
                    created_date TEXT NOT NULL
                )
            """)

            # ── 偵測 construction_records 目前的 schema 狀態 ─────────
            rec_exists = bool(cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='construction_records'"
            ).fetchone())
            if rec_exists:
                cursor.execute("PRAGMA table_info(construction_records)")
                rec_cols = {r["name"] for r in cursor.fetchall()}
                is_v1     = "cement_used" in rec_cols
                needs_v3  = "project_id"  not in rec_cols
            else:
                rec_cols, is_v1, needs_v3 = set(), False, True

            # ── materials（全域材料目錄，schema 不變）────────────────
            mat_just_created = not bool(cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='materials'"
            ).fetchone())
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS materials (
                    material_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    material_name TEXT UNIQUE NOT NULL,
                    unit          TEXT NOT NULL DEFAULT '噸'
                )
            """)

            # ── record_materials（不變）──────────────────────────────
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS record_materials (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id   INTEGER NOT NULL,
                    material_id INTEGER NOT NULL,
                    quantity    REAL    DEFAULT 0,
                    FOREIGN KEY (record_id)
                        REFERENCES construction_records(record_id) ON DELETE CASCADE,
                    FOREIGN KEY (material_id)
                        REFERENCES materials(material_id) ON DELETE CASCADE,
                    UNIQUE(record_id, material_id)
                )
            """)

            # ── v3 migration：加入 project_id 並修改 UNIQUE 約束 ─────
            if needs_v3:
                today = date.today().isoformat()
                cursor.execute(
                    "INSERT OR IGNORE INTO projects "
                    "(project_name, description, created_date) "
                    "VALUES ('預設專案', '系統自動建立的初始專案', ?)",
                    (today,),
                )
                pid_row  = cursor.execute(
                    "SELECT project_id FROM projects "
                    "ORDER BY project_id ASC LIMIT 1"
                ).fetchone()
                dpid = pid_row[0] if pid_row else 1

                if rec_exists:
                    # SQLite 不支援 DROP CONSTRAINT，需重建資料表
                    cursor.execute("""
                        CREATE TABLE construction_records_v3 (
                            record_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                            project_id    INTEGER NOT NULL,
                            record_date   TEXT    NOT NULL,
                            workers_count INTEGER NOT NULL,
                            FOREIGN KEY (project_id)
                                REFERENCES projects(project_id) ON DELETE CASCADE,
                            UNIQUE(project_id, record_date)
                        )
                    """)
                    cursor.execute(f"""
                        INSERT OR IGNORE INTO construction_records_v3
                            (record_id, project_id, record_date, workers_count)
                        SELECT record_id, {dpid}, record_date, workers_count
                        FROM construction_records
                    """)
                    cursor.execute("DROP TABLE construction_records")
                    cursor.execute(
                        "ALTER TABLE construction_records_v3 "
                        "RENAME TO construction_records"
                    )
                else:
                    cursor.execute("""
                        CREATE TABLE construction_records (
                            record_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                            project_id    INTEGER NOT NULL,
                            record_date   TEXT    NOT NULL,
                            workers_count INTEGER NOT NULL,
                            FOREIGN KEY (project_id)
                                REFERENCES projects(project_id) ON DELETE CASCADE,
                            UNIQUE(project_id, record_date)
                        )
                    """)
            else:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS construction_records (
                        record_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id    INTEGER NOT NULL,
                        record_date   TEXT    NOT NULL,
                        workers_count INTEGER NOT NULL,
                        FOREIGN KEY (project_id)
                            REFERENCES projects(project_id) ON DELETE CASCADE,
                        UNIQUE(project_id, record_date)
                    )
                """)

            # ── v1→v2 migration：舊版固定材料欄轉入 record_materials ──
            if is_v1 and mat_just_created:
                cursor.execute(
                    "INSERT OR IGNORE INTO materials (material_name, unit) VALUES ('水泥', '噸')"
                )
                cursor.execute(
                    "INSERT OR IGNORE INTO materials (material_name, unit) VALUES ('鋼筋', '噸')"
                )
                cement_id = cursor.execute(
                    "SELECT material_id FROM materials WHERE material_name='水泥'"
                ).fetchone()[0]
                steel_id = cursor.execute(
                    "SELECT material_id FROM materials WHERE material_name='鋼筋'"
                ).fetchone()[0]
                cursor.execute("""
                    INSERT OR IGNORE INTO record_materials (record_id, material_id, quantity)
                    SELECT record_id, ?, cement_used
                    FROM construction_records WHERE cement_used > 0
                """, (cement_id,))
                cursor.execute("""
                    INSERT OR IGNORE INTO record_materials (record_id, material_id, quantity)
                    SELECT record_id, ?, steel_used
                    FROM construction_records WHERE steel_used > 0
                """, (steel_id,))

            # ── 預設帳號 ─────────────────────────────────────────────
            cursor.executemany(
                "INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)",
                [("admin", "admin123", "admin"), ("site1", "site123", "user")],
            )
            cursor.execute("PRAGMA foreign_keys = ON")
            conn.commit()

    # ----------------------------------------------------------
    # 公開：使用者驗證
    # ----------------------------------------------------------
    def authenticate(self, username: str, password: str) -> dict | None:
        """
        核對帳號密碼。
        成功回傳 {'user_id', 'username', 'role'}，失敗回傳 None。
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT user_id, username, role FROM users "
                "WHERE username=? AND password=?",
                (username, password),
            ).fetchone()
            return dict(row) if row else None

    # ----------------------------------------------------------
    # 公開：材料類型 CRUD
    # ----------------------------------------------------------
    def add_material(self, name: str, unit: str = "噸") -> tuple[bool, str]:
        """
        新增材料類型至 materials 目錄。
        若名稱重複（UNIQUE 約束），回傳失敗訊息。
        """
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO materials (material_name, unit) VALUES (?, ?)",
                    (name.strip(), unit.strip() or "噸"),
                )
                conn.commit()
                return True, f"✅ 材料「{name}」已建立。"
        except sqlite3.IntegrityError:
            return False, f"⚠️ 材料「{name}」已存在，無需重複建立。"
        except Exception as exc:
            return False, f"❌ 錯誤：{exc}"

    def get_all_materials(self) -> pd.DataFrame:
        """回傳所有材料類型（按 material_id 升序）"""
        with self._get_connection() as conn:
            return pd.read_sql_query(
                "SELECT material_id, material_name, unit "
                "FROM materials ORDER BY material_id ASC",
                conn,
            )

    def delete_material(self, material_id: int) -> tuple[bool, str]:
        """
        刪除材料類型。
        因 record_materials 設有 ON DELETE CASCADE，
        相關施工紀錄中該材料的消耗資料會同步刪除。
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT material_name FROM materials WHERE material_id=?",
                    (material_id,),
                ).fetchone()
                if not row:
                    return False, f"⚠️ 找不到材料 ID {material_id}。"
                name = row["material_name"]
                cursor.execute(
                    "DELETE FROM materials WHERE material_id=?", (material_id,)
                )
                conn.commit()
                return True, f"✅ 材料「{name}」(ID {material_id}) 已刪除。"
        except Exception as exc:
            return False, f"❌ 刪除失敗：{exc}"

    # ----------------------------------------------------------
    # 公開：施工紀錄 CRUD
    # ----------------------------------------------------------
    def add_record(
        self, record: ConstructionRecord, material_quantities: dict, project_id: int
    ) -> tuple[bool, str]:
        """新增施工紀錄至指定專案，同時寫入各材料的消耗量。"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO construction_records "
                    "(project_id, record_date, workers_count) VALUES (?, ?, ?)",
                    (project_id, record.date, record.workers),
                )
                new_id = cursor.lastrowid
                for mat_id, qty in material_quantities.items():
                    if qty > 0:
                        cursor.execute(
                            "INSERT OR IGNORE INTO record_materials "
                            "(record_id, material_id, quantity) VALUES (?, ?, ?)",
                            (new_id, int(mat_id), float(qty)),
                        )
                conn.commit()
                return True, f"✅ {record.date} 的施工日誌新增成功！"
        except sqlite3.IntegrityError:
            return False, f"⚠️ {record.date} 的施工紀錄已存在，無法重複新增。"
        except Exception as exc:
            return False, f"❌ 資料庫錯誤：{exc}"

    def get_record_by_date(self, query_date: str, project_id: int) -> dict | None:
        """依日期與專案查詢單筆施工紀錄，含所有材料消耗明細。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT * FROM construction_records "
                "WHERE record_date=? AND project_id=?",
                (query_date, project_id),
            ).fetchone()
            if not row:
                return None
            record = ConstructionRecord(
                record_id=row["record_id"],
                date=row["record_date"],
                workers=row["workers_count"],
            )
            materials = [
                dict(r) for r in cursor.execute(
                    """SELECT m.material_name, m.unit, rm.quantity
                       FROM record_materials rm
                       JOIN materials m ON rm.material_id = m.material_id
                       WHERE rm.record_id = ?
                       ORDER BY m.material_id ASC""",
                    (row["record_id"],),
                ).fetchall()
            ]
            return {"record": record, "materials": materials}

    def get_all_records(self, project_id: int) -> pd.DataFrame:
        """讀取指定專案的所有施工紀錄基本欄位。"""
        with self._get_connection() as conn:
            return pd.read_sql_query(
                "SELECT record_id, record_date, workers_count "
                "FROM construction_records WHERE project_id=? "
                "ORDER BY record_date ASC",
                conn, params=(project_id,),
            )

    def get_all_records_with_materials(self, project_id: int) -> pd.DataFrame:
        """讀取指定專案的所有紀錄，材料消耗以 Pivot 格式展開為獨立欄位。"""
        with self._get_connection() as conn:
            records_df = pd.read_sql_query(
                "SELECT record_id, record_date, workers_count "
                "FROM construction_records WHERE project_id=? "
                "ORDER BY record_date ASC",
                conn, params=(project_id,),
            )
            if records_df.empty:
                return records_df
            mat_df = pd.read_sql_query(
                """SELECT rm.record_id, m.material_name, rm.quantity
                   FROM record_materials rm
                   JOIN materials m ON rm.material_id = m.material_id
                   WHERE rm.record_id IN (
                       SELECT record_id FROM construction_records WHERE project_id=?
                   )""",
                conn, params=(project_id,),
            )
        if mat_df.empty:
            return records_df
        pivot = mat_df.pivot_table(
            index="record_id", columns="material_name",
            values="quantity", aggfunc="sum", fill_value=0,
        )
        pivot.columns.name = None
        return records_df.merge(pivot.reset_index(), on="record_id", how="left").fillna(0)

    def get_records_by_date_range(
        self, start_date: str, end_date: str, project_id: int
    ) -> pd.DataFrame:
        """查詢指定專案與日期範圍內的施工紀錄，含材料消耗（Pivot 格式）。"""
        with self._get_connection() as conn:
            records_df = pd.read_sql_query(
                "SELECT record_id, record_date, workers_count "
                "FROM construction_records "
                "WHERE project_id=? AND record_date BETWEEN ? AND ? "
                "ORDER BY record_date ASC",
                conn, params=(project_id, start_date, end_date),
            )
            if records_df.empty:
                return records_df
            record_ids   = records_df["record_id"].tolist()
            placeholders = ",".join("?" * len(record_ids))
            mat_df = pd.read_sql_query(
                f"""SELECT rm.record_id, m.material_name, rm.quantity
                    FROM record_materials rm
                    JOIN materials m ON rm.material_id = m.material_id
                    WHERE rm.record_id IN ({placeholders})""",
                conn, params=record_ids,
            )
        if mat_df.empty:
            return records_df
        pivot = mat_df.pivot_table(
            index="record_id", columns="material_name",
            values="quantity", aggfunc="sum", fill_value=0,
        )
        pivot.columns.name = None
        return records_df.merge(pivot.reset_index(), on="record_id", how="left").fillna(0)

    def _reorder_record_ids(self) -> None:
        """
        依施工日期重新排序 record_id（1, 2, 3, ...）。
        採用「負數暫存 ID」兩段式更新，避免主鍵 UNIQUE 衝突：
          第一輪：舊 ID → 對應負數 temp ID
          第二輪：負數 temp ID → 正數最終 ID
        同步更新 record_materials 子表與 sqlite_sequence 計數器。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = OFF")

            rows = cursor.execute(
                "SELECT record_id FROM construction_records ORDER BY record_date ASC"
            ).fetchall()

            if not rows:
                cursor.execute("PRAGMA foreign_keys = ON")
                return

            # 第一輪：需要變動的 ID 先改為負數暫存值
            for new_id, row in enumerate(rows, start=1):
                old_id = row["record_id"]
                if old_id != new_id:
                    cursor.execute(
                        "UPDATE record_materials SET record_id = ? WHERE record_id = ?",
                        (-new_id, old_id),
                    )
                    cursor.execute(
                        "UPDATE construction_records SET record_id = ? WHERE record_id = ?",
                        (-new_id, old_id),
                    )

            # 第二輪：負數暫存值改回最終正數 ID
            for new_id in range(1, len(rows) + 1):
                cursor.execute(
                    "UPDATE record_materials SET record_id = ? WHERE record_id = ?",
                    (new_id, -new_id),
                )
                cursor.execute(
                    "UPDATE construction_records SET record_id = ? WHERE record_id = ?",
                    (new_id, -new_id),
                )

            # 重設 AUTOINCREMENT 序列，確保下一筆新增 ID 從正確值開始
            max_id = len(rows)
            updated = cursor.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'construction_records'",
                (max_id,),
            ).rowcount
            if updated == 0:
                cursor.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('construction_records', ?)",
                    (max_id,),
                )

            cursor.execute("PRAGMA foreign_keys = ON")
            conn.commit()

    def reorder_record_ids(self) -> tuple[bool, str]:
        """公開介面：手動觸發 ID 重新排序，回傳操作結果。"""
        try:
            with self._get_connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM construction_records"
                ).fetchone()[0]
            self._reorder_record_ids()
            return True, f"✅ 已完成重新排序，共 {count} 筆紀錄依施工日期重新編號。"
        except Exception as exc:
            return False, f"❌ 重新排序失敗：{exc}"

    def delete_record(self, record_id: int) -> tuple[bool, str]:
        """刪除施工紀錄，CASCADE 自動清除 record_materials，刪後重排 ID。"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM construction_records WHERE record_id=?", (record_id,)
                )
                if cursor.rowcount > 0:
                    conn.commit()
                    self._reorder_record_ids()
                    return True, f"✅ 紀錄 ID {record_id} 已成功刪除，所有紀錄 ID 已依施工日期重新排序。"
                return False, f"⚠️ 找不到 record_id = {record_id}。"
        except Exception as exc:
            return False, f"❌ 刪除失敗：{exc}"

    # ----------------------------------------------------------
    # 公開：專案 CRUD（v3 新增）
    # ----------------------------------------------------------
    def add_project(self, name: str, description: str = "") -> tuple[bool, str]:
        """新增專案；名稱重複時拒絕。"""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO projects (project_name, description, created_date) "
                    "VALUES (?, ?, ?)",
                    (name.strip(), description.strip(), date.today().isoformat()),
                )
                conn.commit()
                return True, f"✅ 專案「{name}」已建立。"
        except sqlite3.IntegrityError:
            return False, f"⚠️ 專案名稱「{name}」已存在。"
        except Exception as exc:
            return False, f"❌ 錯誤：{exc}"

    def get_all_projects(self) -> pd.DataFrame:
        """回傳所有專案（按 project_id 升序）。"""
        with self._get_connection() as conn:
            return pd.read_sql_query(
                "SELECT project_id, project_name, description, created_date "
                "FROM projects ORDER BY project_id ASC",
                conn,
            )

    def get_project(self, project_id: int) -> dict | None:
        """依 ID 取得單一專案資訊。"""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT project_id, project_name, description, created_date "
                "FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_project(
        self, project_id: int, name: str, description: str
    ) -> tuple[bool, str]:
        """修改專案名稱與說明。"""
        try:
            with self._get_connection() as conn:
                affected = conn.execute(
                    "UPDATE projects SET project_name=?, description=? "
                    "WHERE project_id=?",
                    (name.strip(), description.strip(), project_id),
                ).rowcount
                conn.commit()
                if affected:
                    return True, f"✅ 專案已更新為「{name}」。"
                return False, f"⚠️ 找不到專案 ID {project_id}。"
        except sqlite3.IntegrityError:
            return False, f"⚠️ 專案名稱「{name}」已被其他專案使用。"
        except Exception as exc:
            return False, f"❌ 錯誤：{exc}"

    def delete_project(self, project_id: int) -> tuple[bool, str]:
        """刪除專案；所有紀錄因 CASCADE 同步刪除，材料目錄不受影響。"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT project_name FROM projects WHERE project_id=?",
                    (project_id,)
                ).fetchone()
                if not row:
                    return False, f"⚠️ 找不到專案 ID {project_id}。"
                name = row["project_name"]
                cursor.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
                conn.commit()
            self._reorder_record_ids()
            return True, f"✅ 專案「{name}」及其所有施工紀錄已刪除。"
        except Exception as exc:
            return False, f"❌ 刪除失敗：{exc}"


# ==============================================================
# 全域資源快取：整個 App 生命週期只建立一個 ProjectManager
# ==============================================================
@st.cache_resource
def get_manager() -> ProjectManager:
    """
    @st.cache_resource 確保 Streamlit 每次重新渲染時不會重複初始化資料庫。
    """
    return ProjectManager()


# ==============================================================
# UI：登入畫面
# ==============================================================
def render_login():
    """渲染登入表單，成功後寫入 session_state 並切換至主畫面"""
    st.title("🏗️ 智慧營建管理系統")
    st.caption("Smart Construction Manager — Lite | 電腦輔助工程 期末專案")
    st.divider()

    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.subheader("🔐 請先登入")
        with st.form("login_form"):
            username = st.text_input("帳號", placeholder="輸入帳號")
            password = st.text_input("密碼", type="password", placeholder="輸入密碼")
            submitted = st.form_submit_button(
                "登入", use_container_width=True, type="primary"
            )

        if submitted:
            user_info = get_manager().authenticate(username.strip(), password)
            if user_info:
                st.session_state["logged_in"] = True
                st.session_state["user"] = user_info
                st.rerun()
            else:
                st.error("帳號或密碼錯誤，請重新輸入。")

        with st.expander("💡 測試帳號提示"):
            st.code("管理員 ：admin  / admin123\n現場人員：site1  / site123")


# ==============================================================
# UI：共用側邊欄底部（使用者資訊 + 登出）
# ==============================================================
def render_sidebar_footer():
    """在側邊欄底部顯示登入者資訊、目前專案（user 角色）與登出按鈕。"""
    st.sidebar.divider()
    user = st.session_state.get("user", {})
    role_label = "👑 管理員" if user.get("role") == "admin" else "👷 現場人員"
    st.sidebar.markdown(f"**{role_label}**：{user.get('username', '')}")

    proj = st.session_state.get("current_project")
    if proj and user.get("role") == "user":
        st.sidebar.markdown(f"📁 **{proj['project_name']}**")
        if proj.get("description"):
            st.sidebar.caption(proj["description"])
        if st.sidebar.button("🔄 切換專案", use_container_width=True, key="switch_proj_btn"):
            st.session_state["current_project"] = None
            st.session_state.pop("pending_materials", None)
            st.rerun()

    if st.sidebar.button("🚪 登出", use_container_width=True):
        st.session_state.clear()
        st.rerun()


# ==============================================================
# UI：專案選擇畫面（user 登入後、進入主介面前）
# ==============================================================
def render_project_selector():
    """列出所有可用專案，讓現場人員選擇或即時建立新專案。"""
    manager = get_manager()
    st.title("🏗️ 智慧營建管理系統")
    st.subheader("選擇施工專案")
    st.caption("請選擇要操作的專案，或在下方建立一個新專案。")
    render_sidebar_footer()

    projects_df = manager.get_all_projects()

    col_list, col_new = st.columns([3, 2])

    with col_list:
        st.markdown("### 📂 現有專案")
        if projects_df.empty:
            st.info("ℹ️ 目前尚無任何專案，請在右側建立第一個專案。")
        else:
            for _, row in projects_df.iterrows():
                with st.container(border=True):
                    c1, c2 = st.columns([3, 1])
                    with c1:
                        st.markdown(f"**{row['project_name']}**")
                        if row["description"]:
                            st.caption(row["description"])
                        st.caption(f"建立日期：{row['created_date']}")
                    with c2:
                        if st.button(
                            "進入", key=f"sel_{int(row['project_id'])}",
                            type="primary", use_container_width=True,
                        ):
                            st.session_state["current_project"] = {
                                "project_id":   int(row["project_id"]),
                                "project_name": row["project_name"],
                                "description":  row["description"],
                            }
                            st.rerun()

    with col_new:
        st.markdown("### ➕ 建立新專案")
        with st.form("new_proj_form", clear_on_submit=True):
            p_name = st.text_input("專案名稱", placeholder="例：台北捷運 C310 標")
            p_desc = st.text_area("說明（選填）",
                                  placeholder="例：地下段隧道工程", height=80)
            if st.form_submit_button("建立專案", type="primary",
                                     use_container_width=True):
                if p_name.strip():
                    ok, msg = manager.add_project(p_name.strip(), p_desc.strip())
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.warning(msg)
                else:
                    st.warning("⚠️ 請輸入專案名稱。")


# ==============================================================
# UI：前台 — 現場人員介面（role == 'user'）
# ==============================================================
def render_user_interface():
    """
    現場人員主介面。登入後先選擇專案，再進入功能頁：
    - 新增日誌、歷史查詢、視覺化報表（均限定於當前專案）
    """
    manager = get_manager()

    # ── 專案選擇 ─────────────────────────────────────────────────
    if not st.session_state.get("current_project"):
        render_project_selector()
        return

    project_id   = st.session_state["current_project"]["project_id"]
    project_name = st.session_state["current_project"]["project_name"]

    st.sidebar.title("📋 現場人員選單")
    st.sidebar.caption(f"📁 {project_name}")
    page = st.sidebar.radio(
        "功能頁",
        ["📝 新增日誌", "🔍 歷史查詢", "📊 視覺化報表"],
        label_visibility="collapsed",
    )
    render_sidebar_footer()

    # ----------------------------------------------------------------
    # 頁面：新增日誌
    # ----------------------------------------------------------------
    if page == "📝 新增日誌":
        st.title(f"📝 新增施工日誌｜{project_name}")
        st.caption(
            "每日只能新增一筆紀錄。若所需材料不在清單中，"
            "請先展開下方「建立新材料類型」區塊新增後再填表。"
        )

        # === 區塊 A：建立新材料類型（獨立於主表單，可即時 rerun）===
        # mat_expander_open=False 讓成功後呼叫 st.rerun() 時能自動收合 expander
        with st.expander(
            "➕ 建立新材料類型（清單缺少時在此新增）",
            expanded=st.session_state.get("mat_expander_open", False),
        ):
            nc1, nc2, nc3 = st.columns([2, 1, 1])
            with nc1:
                new_name = st.text_input(
                    "材料名稱",
                    placeholder="例：混凝土、砂石、防水漆",
                    key="new_mat_name",
                    label_visibility="visible",
                )
            with nc2:
                new_unit_sel = st.selectbox(
                    "單位",
                    ["噸", "包", "立方米", "公升", "公斤", "片", "條", "桶", "自訂"],
                    key="new_mat_unit",
                )
                if new_unit_sel == "自訂":
                    new_unit_custom = st.text_input(
                        "自訂單位名稱",
                        placeholder="例：立方公尺、件",
                        key="new_mat_unit_custom",
                    )
                    new_unit = new_unit_custom.strip()
                else:
                    new_unit = new_unit_sel
            with nc3:
                st.write("")
                st.write("")  # 對齊按鈕高度
                if st.button("✅ 建立材料", key="create_mat_btn", use_container_width=True):
                    if not new_name.strip():
                        st.warning("⚠️ 請輸入材料名稱。")
                    elif not new_unit:
                        st.warning("⚠️ 請輸入自訂單位名稱。")
                    else:
                        ok, msg = manager.add_material(new_name.strip(), new_unit)
                        if ok:
                            st.session_state["mat_expander_open"] = False
                            st.toast(msg, icon="✅")
                            st.rerun()  # 收合 expander，新材料立即出現在下方表單
                        else:
                            st.warning(msg)

        st.divider()

        # === 區塊 B：施工基本資訊 ===
        c1, c2 = st.columns(2)
        with c1:
            selected_date = st.date_input("施工日期 📅", value=date.today(), key="log_date")
        with c2:
            workers = st.number_input(
                "出勤人數 👷", min_value=0, max_value=9999, step=1, value=10, key="log_workers"
            )

        st.subheader("🪨 材料消耗量")

        # 初始化暫存的材料清單（存於 session_state，跨 rerun 保留）
        # 結構：{material_id (int): {"name": str, "unit": str, "qty": float}}
        if "pending_materials" not in st.session_state:
            st.session_state["pending_materials"] = {}

        materials_df = manager.get_all_materials()

        if materials_df.empty:
            st.info("⚠️ 尚未建立任何材料類型，請先使用上方「建立新材料類型」功能新增材料後再填寫。")
        else:
            # ---- 下拉選單區：選材料 + 輸入數量 + 加入清單 ----
            # 建立「顯示標籤 → (id, name, unit)」對應字典
            mat_options = {
                f"{row['material_name']}（{row['unit']}）": (
                    int(row["material_id"]), row["material_name"], row["unit"]
                )
                for _, row in materials_df.iterrows()
            }

            sel1, sel2, sel3 = st.columns([3, 2, 1])
            with sel1:
                chosen_label = st.selectbox(
                    "選擇材料",
                    list(mat_options.keys()),
                    key="mat_dropdown",
                )
            with sel2:
                qty_val = st.number_input(
                    "消耗量",
                    min_value=0.01,
                    step=0.1,
                    format="%.2f",
                    value=1.0,
                    key="mat_qty",
                )
            with sel3:
                st.write("")
                st.write("")  # 對齊按鈕高度
                if st.button("➕ 加入", key="add_to_list", use_container_width=True):
                    mat_id, mat_name, mat_unit = mat_options[chosen_label]
                    # 若相同材料已在清單中則更新數量
                    action = "更新" if mat_id in st.session_state["pending_materials"] else "新增"
                    st.session_state["pending_materials"][mat_id] = {
                        "name": mat_name,
                        "unit": mat_unit,
                        "qty":  qty_val,
                    }
                    st.toast(f"{action}：{mat_name} {qty_val:.2f} {mat_unit}")
                    st.rerun()

        # ---- 已選材料清單 ----
        if st.session_state.get("pending_materials"):
            st.caption("本次日誌已選材料（送出後寫入資料庫）：")

            # 表頭
            h1, h2, h3 = st.columns([3, 2, 1])
            h1.markdown("**材料名稱**")
            h2.markdown("**消耗量**")
            h3.markdown("")

            for mat_id, info in list(st.session_state["pending_materials"].items()):
                r1, r2, r3 = st.columns([3, 2, 1])
                r1.write(f"🔹 {info['name']}")
                r2.write(f"{info['qty']:.2f} {info['unit']}")
                if r3.button("❌", key=f"rm_{mat_id}", help=f"移除 {info['name']}"):
                    del st.session_state["pending_materials"][mat_id]
                    st.rerun()
        else:
            st.caption("尚未加入任何材料（若無需記錄材料，可直接送出）。")

        st.divider()

        if st.button("📥 送出施工日誌", type="primary", use_container_width=True, key="submit_log"):
            date_str = selected_date.strftime("%Y-%m-%d")
            record = ConstructionRecord(date=date_str, workers=int(workers))
            material_quantities = {
                mat_id: info["qty"]
                for mat_id, info in st.session_state.get("pending_materials", {}).items()
            }
            success, msg = manager.add_record(record, material_quantities, project_id)
            if success:
                st.session_state["pending_materials"] = {}  # 送出成功後清空暫存清單
                st.success(msg)
                st.balloons()
            else:
                st.warning(msg)

    # ----------------------------------------------------------------
    # 頁面：歷史查詢
    # ----------------------------------------------------------------
    elif page == "🔍 歷史查詢":
        st.title(f"🔍 歷史紀錄查詢｜{project_name}")

        query_mode = st.radio(
            "查詢模式",
            ["📅 單日查詢", "📆 時間範圍查詢"],
            horizontal=True,
            label_visibility="collapsed",
        )

        if query_mode == "📅 單日查詢":
            st.caption("選擇日期以查詢該日的施工資料與材料消耗明細。")
            query_date = st.date_input("查詢日期 📅", value=date.today(), key="single_query_date")

            if st.button("🔎 開始查詢", type="primary", key="single_query_btn"):
                date_str = query_date.strftime("%Y-%m-%d")
                result = manager.get_record_by_date(date_str, project_id)

                if result:
                    rec  = result["record"]
                    mats = result["materials"]

                    st.success(f"✅ 找到 **{date_str}** 的施工紀錄")
                    c1, c2 = st.columns(2)
                    c1.metric("紀錄 ID",  rec.record_id)
                    c2.metric("出勤人數", f"{rec.workers} 人")

                    st.subheader("🪨 材料消耗明細")
                    if mats:
                        mat_table = pd.DataFrame(mats).rename(
                            columns={
                                "material_name": "材料名稱",
                                "unit":          "單位",
                                "quantity":      "消耗量",
                            }
                        )
                        st.dataframe(mat_table, use_container_width=True, hide_index=True)
                    else:
                        st.info("本日未記錄任何材料消耗。")
                else:
                    st.info(f"ℹ️ **{date_str}** 尚無施工紀錄。")

        else:  # 時間範圍查詢
            st.caption("選擇起訖日期以查詢該時間段內的所有施工紀錄與材料消耗彙整。")
            rc1, rc2 = st.columns(2)
            with rc1:
                range_start = st.date_input(
                    "起始日期 📅",
                    value=date.today() - timedelta(days=30),
                    key="range_start",
                )
            with rc2:
                range_end = st.date_input(
                    "結束日期 📅",
                    value=date.today(),
                    key="range_end",
                )

            if st.button("🔎 開始查詢", type="primary", key="range_query_btn"):
                if range_start > range_end:
                    st.warning("⚠️ 起始日期不可晚於結束日期，請重新選擇。")
                else:
                    start_str = range_start.strftime("%Y-%m-%d")
                    end_str   = range_end.strftime("%Y-%m-%d")
                    results_df = manager.get_records_by_date_range(start_str, end_str, project_id)

                    if results_df.empty:
                        st.info(f"ℹ️ **{start_str}** 至 **{end_str}** 期間無施工紀錄。")
                    else:
                        st.success(
                            f"✅ 找到 **{len(results_df)}** 筆紀錄"
                            f"（{start_str} 至 {end_str}）"
                        )
                        rename_map = {
                            "record_id":     "紀錄 ID",
                            "record_date":   "施工日期",
                            "workers_count": "出勤人數",
                        }
                        st.dataframe(
                            results_df.rename(columns=rename_map),
                            use_container_width=True,
                            hide_index=True,
                        )

    # ----------------------------------------------------------------
    # 頁面：視覺化報表
    # ----------------------------------------------------------------
    elif page == "📊 視覺化報表":
        st.title(f"📊 視覺化報表｜{project_name}")

        df = manager.get_all_records_with_materials(project_id)

        if df.empty:
            st.warning("⚠️ 資料庫目前無紀錄，請先新增施工日誌後再查看報表。")
            return

        st.caption(
            f"資料範圍：{df['record_date'].iloc[0]} 至 {df['record_date'].iloc[-1]}，"
            f"共 {len(df)} 筆。"
        )

        # ---- 圖一：出勤人數趨勢折線圖 ----
        st.subheader("出勤人數趨勢")
        fig1, ax1 = plt.subplots(figsize=(10, 4))
        ax1.plot(
            df["record_date"],
            df["workers_count"],
            marker="o",
            linewidth=2,
            markersize=5,
            color="#1976D2",
        )
        ax1.fill_between(
            df["record_date"], df["workers_count"], alpha=0.15, color="#1976D2"
        )
        ax1.set_xlabel("日期")
        ax1.set_ylabel("出勤人數（人）")
        ax1.set_title("每日出勤人數趨勢")
        ax1.tick_params(axis="x", rotation=45)
        ax1.grid(axis="y", linestyle="--", alpha=0.4)
        fig1.tight_layout()
        st.pyplot(fig1)
        plt.close(fig1)

        # ---- 圖二：動態材料消耗堆疊長條圖 ----
        # 識別材料欄位（排除固定的基本欄位）
        fixed_cols = {"record_id", "record_date", "workers_count"}
        mat_cols = [c for c in df.columns if c not in fixed_cols]

        if mat_cols:
            st.divider()
            st.subheader("建材消耗趨勢（堆疊）")
            colors = plt.cm.Set2.colors  # 自動循環配色
            x = range(len(df))

            fig2, ax2 = plt.subplots(figsize=(10, 5))
            bottom = [0.0] * len(df)

            for idx, mat in enumerate(mat_cols):
                color = colors[idx % len(colors)]
                ax2.bar(
                    x, df[mat], bottom=bottom,
                    label=mat, color=color, alpha=0.85,
                )
                bottom = [b + v for b, v in zip(bottom, df[mat])]

            ax2.set_xlabel("日期")
            ax2.set_ylabel("消耗量")
            ax2.set_title("建材消耗趨勢（依日期堆疊）")
            ax2.set_xticks(list(x))
            ax2.set_xticklabels(df["record_date"], rotation=45, ha="right")
            ax2.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), title="材料種類")
            ax2.grid(axis="y", linestyle="--", alpha=0.4)
            fig2.tight_layout()
            st.pyplot(fig2)
            plt.close(fig2)
        else:
            st.info("ℹ️ 尚無材料消耗資料，請先建立材料類型並在施工日誌中填入消耗量。")


# ==============================================================
# UI：後台 — 管理員介面（role == 'admin'）
# ==============================================================
def render_admin_interface():
    """
    管理員主介面，側邊欄四個功能頁：
    - 🏗️ 專案管理：建立/編輯/刪除專案
    - 🗂️ 資料管理：依專案檢視、刪除紀錄、重排 ID
    - 🧱 材料管理：新增/刪除全域材料類型
    - 📤 資料匯出：依專案下載 CSV
    """
    manager = get_manager()

    st.sidebar.title("⚙️ 管理員選單")
    page = st.sidebar.radio(
        "功能頁",
        ["🏗️ 專案管理", "🗂️ 資料管理", "🧱 材料管理", "📤 資料匯出"],
        label_visibility="collapsed",
    )

    # ── 非「專案管理」頁在側邊欄顯示專案選擇器 ─────────────────
    admin_project_id = None
    if page != "🏗️ 專案管理":
        projects_df = manager.get_all_projects()
        if not projects_df.empty:
            proj_map = {
                row["project_name"]: int(row["project_id"])
                for _, row in projects_df.iterrows()
            }
            proj_names = list(proj_map.keys())
            saved = st.session_state.get("admin_proj_name")
            default_idx = proj_names.index(saved) if saved in proj_names else 0
            sel_name = st.sidebar.selectbox(
                "📁 選擇專案", proj_names, index=default_idx, key="admin_proj_sel"
            )
            st.session_state["admin_proj_name"] = sel_name
            admin_project_id = proj_map[sel_name]

    render_sidebar_footer()

    # ----------------------------------------------------------------
    # 頁面：🏗️ 專案管理
    # ----------------------------------------------------------------
    if page == "🏗️ 專案管理":
        st.title("🏗️ 專案管理")
        st.caption(
            "在此建立、編輯或刪除施工專案。"
            "刪除專案後，該專案的所有施工紀錄將一同刪除；材料目錄（全域共用）不受影響。"
        )

        projects_df = manager.get_all_projects()

        if not projects_df.empty:
            st.markdown(f"目前共有 **{len(projects_df)}** 個專案：")
            for _, row in projects_df.iterrows():
                pid  = int(row["project_id"])
                with st.expander(
                    f"📁 {row['project_name']}　　ID: {pid}　建立：{row['created_date']}"
                ):
                    with st.form(f"edit_proj_{pid}", clear_on_submit=False):
                        new_name = st.text_input("專案名稱", value=row["project_name"],
                                                 key=f"pname_{pid}")
                        new_desc = st.text_area("專案說明", value=row["description"],
                                                height=80, key=f"pdesc_{pid}")
                        ec1, ec2 = st.columns(2)
                        save_clicked = ec1.form_submit_button(
                            "💾 儲存修改", type="primary", use_container_width=True
                        )
                        del_clicked  = ec2.form_submit_button(
                            "🗑️ 刪除此專案", use_container_width=True
                        )
                    if save_clicked:
                        ok, msg = manager.update_project(pid, new_name, new_desc)
                        (st.success if ok else st.warning)(msg)
                        if ok:
                            st.rerun()
                    if del_clicked:
                        ok, msg = manager.delete_project(pid)
                        (st.success if ok else st.warning)(msg)
                        if ok:
                            st.session_state.pop("admin_proj_name", None)
                            st.rerun()
        else:
            st.info("ℹ️ 目前尚無任何專案。")

        st.divider()
        st.subheader("➕ 建立新專案")
        with st.form("add_proj_form", clear_on_submit=True):
            p_name = st.text_input("專案名稱", placeholder="例：高雄輕軌 CR2 段")
            p_desc = st.text_area("說明（選填）",
                                  placeholder="例：輕軌地面段軌道鋪設工程", height=80)
            if st.form_submit_button("建立", type="primary", use_container_width=True):
                if p_name.strip():
                    ok, msg = manager.add_project(p_name.strip(), p_desc.strip())
                    (st.success if ok else st.warning)(msg)
                    if ok:
                        st.rerun()
                else:
                    st.warning("⚠️ 請輸入專案名稱。")

    elif admin_project_id is None:
        st.warning("⚠️ 尚無任何專案，請先至「🏗️ 專案管理」頁面建立專案。")

    # ----------------------------------------------------------------
    # 頁面：🗂️ 資料管理
    # ----------------------------------------------------------------
    elif page == "🗂️ 資料管理":
        proj_name = st.session_state.get("admin_proj_name", "")
        st.title(f"🗂️ 施工資料管理｜{proj_name}")

        df = manager.get_all_records_with_materials(admin_project_id)

        if df.empty:
            st.info("ℹ️ 此專案尚無任何施工紀錄。")
        else:
            st.markdown(f"共有 **{len(df)}** 筆施工紀錄：")
            st.dataframe(
                df.rename(columns={
                    "record_id": "紀錄 ID", "record_date": "施工日期",
                    "workers_count": "出勤人數",
                }),
                use_container_width=True, hide_index=True,
            )

        st.divider()
        st.subheader("🗑️ 刪除施工紀錄")
        st.caption("輸入 Record ID 後確認刪除。相關材料消耗資料將一同刪除，此操作不可復原。")
        with st.form("delete_record_form"):
            del_id = st.number_input("Record ID", min_value=1, step=1, format="%d")
            if st.form_submit_button("⚠️ 確認刪除紀錄", type="primary"):
                ok, msg = manager.delete_record(int(del_id))
                (st.success if ok else st.warning)(msg)
                if ok:
                    st.rerun()

        st.divider()
        st.subheader("🔢 重新整理紀錄 ID")
        st.caption("將所有紀錄 ID 依施工日期由小到大重新排序（1, 2, 3, ...）。")
        if st.button("🔄 立即重新排序所有紀錄 ID", key="reorder_ids_btn"):
            ok, msg = manager.reorder_record_ids()
            (st.success if ok else st.warning)(msg)
            if ok:
                st.rerun()

    # ----------------------------------------------------------------
    # 頁面：🧱 材料管理（全域共用目錄）
    # ----------------------------------------------------------------
    elif page == "🧱 材料管理":
        st.title("🧱 材料類型管理（全域目錄）")
        st.caption(
            "材料目錄為全專案共用。在此新增或刪除材料類型後，"
            "所有專案的施工日誌均可使用（或刪除時同步移除相關消耗紀錄）。"
        )

        materials_df = manager.get_all_materials()
        if materials_df.empty:
            st.info("ℹ️ 目前尚未建立任何材料類型。")
        else:
            st.markdown(f"目前共有 **{len(materials_df)}** 種材料：")
            st.dataframe(
                materials_df.rename(columns={
                    "material_id": "材料 ID", "material_name": "材料名稱", "unit": "單位",
                }),
                use_container_width=True, hide_index=True,
            )

        st.divider()
        col_add, col_del = st.columns(2)

        with col_add:
            st.subheader("➕ 新增材料類型")
            with st.form("add_material_form", clear_on_submit=True):
                mat_name = st.text_input("材料名稱", placeholder="例：混凝土、防水塗料")
                mat_unit_sel = st.selectbox(
                    "計量單位",
                    ["噸", "包", "立方米", "公升", "公斤", "片", "條", "桶"],
                )
                mat_unit_custom = st.text_input(
                    "自訂單位（填寫後優先採用，留空則使用上方選項）",
                    placeholder="例：立方公尺、件、公分",
                )
                if st.form_submit_button("新增", type="primary", use_container_width=True):
                    mat_unit = mat_unit_custom.strip() if mat_unit_custom.strip() else mat_unit_sel
                    if mat_name.strip():
                        ok, msg = manager.add_material(mat_name.strip(), mat_unit)
                        (st.success if ok else st.warning)(msg)
                        if ok:
                            st.rerun()
                    else:
                        st.warning("⚠️ 材料名稱不可空白。")

        with col_del:
            st.subheader("🗑️ 刪除材料類型")
            st.caption("⚠️ 刪除後，所有專案中該材料的消耗紀錄將一同移除。")
            with st.form("delete_material_form"):
                del_mat_id = st.number_input("材料 ID", min_value=1, step=1, format="%d")
                if st.form_submit_button(
                    "⚠️ 確認刪除材料", type="primary", use_container_width=True
                ):
                    ok, msg = manager.delete_material(int(del_mat_id))
                    (st.success if ok else st.warning)(msg)
                    if ok:
                        st.rerun()

    # ----------------------------------------------------------------
    # 頁面：📤 資料匯出
    # ----------------------------------------------------------------
    elif page == "📤 資料匯出":
        proj_name = st.session_state.get("admin_proj_name", "")
        st.title(f"📤 資料匯出｜{proj_name}")

        df = manager.get_all_records_with_materials(admin_project_id)

        if df.empty:
            st.warning("⚠️ 此專案目前無資料可匯出。")
            return

        rename_map = {
            "record_id": "紀錄 ID", "record_date": "施工日期",
            "workers_count": "出勤人數",
        }
        export_mode = st.radio(
            "匯出模式", ["📦 全部匯出", "📋 單筆匯出"],
            horizontal=True, label_visibility="collapsed",
        )
        st.divider()

        if export_mode == "📦 全部匯出":
            st.caption("匯出此專案所有施工紀錄，含動態材料欄位，格式為 CSV。")
            st.markdown(f"預覽（共 **{len(df)}** 筆）：")
            st.dataframe(df.rename(columns=rename_map),
                         use_container_width=True, hide_index=True)
            safe_name = proj_name.replace(" ", "_")
            filename = (
                f"construction_{safe_name}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
            st.download_button(
                label="📥 下載全部 CSV",
                data=df.to_csv(index=False, encoding="utf-8-sig"),
                file_name=filename, mime="text/csv",
                type="primary", use_container_width=True,
            )
            st.success(f"準備下載：`{filename}`，共 {len(df)} 筆紀錄。")

        else:
            st.caption("從下拉選單選擇單一紀錄後下載。")
            options = {
                f"ID {int(row['record_id'])}  —  {row['record_date']}": int(row["record_id"])
                for _, row in df.iterrows()
            }
            chosen_label = st.selectbox("選擇施工紀錄", list(options.keys()),
                                        key="export_select")
            chosen_id  = options[chosen_label]
            single_df  = df[df["record_id"] == chosen_id].reset_index(drop=True)
            st.markdown("預覽：")
            st.dataframe(single_df.rename(columns=rename_map),
                         use_container_width=True, hide_index=True)
            record_date = single_df["record_date"].iloc[0].replace("-", "")
            filename    = f"construction_record_{record_date}.csv"
            st.download_button(
                label=f"📥 下載此筆 CSV（{single_df['record_date'].iloc[0]}）",
                data=single_df.to_csv(index=False, encoding="utf-8-sig"),
                file_name=filename, mime="text/csv",
                type="primary", use_container_width=True,
            )


# ==============================================================
# 主程式入口：依 session_state 決定渲染哪個畫面
# ==============================================================
def main():
    st.set_page_config(
        page_title="智慧營建管理系統",
        page_icon="🏗️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False
    if "current_project" not in st.session_state:
        st.session_state["current_project"] = None

    if not st.session_state["logged_in"]:
        render_login()
    else:
        role = st.session_state["user"]["role"]
        if role == "admin":
            render_admin_interface()
        else:
            render_user_interface()


if __name__ == "__main__":
    main()
