"""
IP 使用统计

功能：
- 记录每个 IP 的使用情况
- 统计 IP 归属地
- 统计请求接口和页数
- 生成 IP 使用报告
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 数据库路径（从配置导入）
from system.config.settings import DATABASE_PATH as DB_PATH


class IPStatsManager:
    """IP 使用统计管理器"""

    def __init__(self):
        """初始化统计管理器"""
        self.db_path = DB_PATH
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # IP 使用记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ip_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                port INTEGER NOT NULL,
                full_ip TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                collector_name TEXT NOT NULL,
                page INTEGER NOT NULL,
                status TEXT NOT NULL,  -- success / failed
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # IP 详情表（归属地）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ip_details (
                ip TEXT PRIMARY KEY,
                country TEXT,
                province TEXT,
                city TEXT,
                isp TEXT,  -- 运营商
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ip ON ip_usage(ip)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_date ON ip_usage(trade_date)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_collector ON ip_usage(collector_name)"
        )

        conn.commit()
        conn.close()

    def record_usage(
        self,
        ip: str,
        port: int,
        trade_date: str,
        collector_name: str,
        page: int,
        status: str,
        error: str = None,
    ):
        """
        记录 IP 使用情况

        Args:
            ip: IP 地址
            port: 端口
            trade_date: 交易日期
            collector_name: 采集器名称
            page: 页码
            status: 状态（success/failed）
            error: 错误信息
        """
        full_ip = f"{ip}:{port}"
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 本地时间

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO ip_usage (ip, port, full_ip, trade_date, collector_name, page, status, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                ip,
                port,
                full_ip,
                trade_date,
                collector_name,
                page,
                status,
                error,
                created_at,
            ),
        )

        conn.commit()
        conn.close()

    def update_ip_detail(
        self,
        ip: str,
        country: str = None,
        province: str = None,
        city: str = None,
        isp: str = None,
    ):
        """
        更新 IP 详情（归属地）

        Args:
            ip: IP 地址
            country: 国家
            province: 省份
            city: 城市
            isp: 运营商
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT OR REPLACE INTO ip_details (ip, country, province, city, isp, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (ip, country, province, city, isp, datetime.now()),
        )

        conn.commit()
        conn.close()

    def get_ip_usage(
        self, trade_date: str = None, collector_name: str = None, ip: str = None
    ) -> List[Dict]:
        """
        获取 IP 使用记录

        Args:
            trade_date: 交易日期（可选）
            collector_name: 采集器名称（可选）
            ip: IP 地址（可选）

        Returns:
            使用记录列表
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 构建查询
        query = "SELECT * FROM ip_usage WHERE 1=1"
        params = []

        if trade_date:
            query += " AND trade_date = ?"
            params.append(trade_date)

        if collector_name:
            query += " AND collector_name = ?"
            params.append(collector_name)

        if ip:
            query += " AND ip = ?"
            params.append(ip)

        query += " ORDER BY created_at DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        conn.close()

        return [dict(row) for row in rows]

    def get_ip_stats(self, trade_date: str = None) -> Dict:
        """
        获取 IP 统计汇总

        Args:
            trade_date: 交易日期（可选）

        Returns:
            统计汇总
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 按 IP 分组统计
        query = """
            SELECT
                ip,
                COUNT(*) as total_requests,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                COUNT(DISTINCT collector_name) as collectors_used,
                COUNT(DISTINCT trade_date) as days_used
            FROM ip_usage
            WHERE 1=1
        """
        params = []

        if trade_date:
            query += " AND trade_date = ?"
            params.append(trade_date)

        query += " GROUP BY ip ORDER BY total_requests DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        conn.close()

        return [dict(row) for row in rows]

    def get_collector_stats(self, trade_date: str = None) -> Dict:
        """
        获取采集器统计

        Args:
            trade_date: 交易日期（可选）

        Returns:
            统计汇总
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = """
            SELECT
                collector_name,
                COUNT(*) as total_pages,
                COUNT(DISTINCT ip) as unique_ips,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count
            FROM ip_usage
            WHERE 1=1
        """
        params = []

        if trade_date:
            query += " AND trade_date = ?"
            params.append(trade_date)

        query += " GROUP BY collector_name"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        conn.close()

        return [dict(row) for row in rows]

    def get_daily_report(self, date: str) -> Dict:
        """
        获取每日报告

        Args:
            date: 日期（YYYY-MM-DD）

        Returns:
            每日报告
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 基本信息
        cursor.execute(
            """
            SELECT
                COUNT(DISTINCT ip) as unique_ips,
                COUNT(*) as total_requests,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count
            FROM ip_usage
            WHERE trade_date = ?
        """,
            (date,),
        )
        basic = dict(cursor.fetchone())

        # 按采集器统计
        cursor.execute(
            """
            SELECT collector_name, COUNT(*) as pages
            FROM ip_usage
            WHERE trade_date = ?
            GROUP BY collector_name
        """,
            (date,),
        )
        collectors = [dict(row) for row in cursor.fetchall()]

        # 成功率 Top IP
        cursor.execute(
            """
            SELECT
                ip,
                COUNT(*) as total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success
            FROM ip_usage
            WHERE trade_date = ?
            GROUP BY ip
            HAVING total >= 3
            ORDER BY CAST(success AS FLOAT) / total DESC
            LIMIT 10
        """,
            (date,),
        )
        top_ips = [dict(row) for row in cursor.fetchall()]

        conn.close()

        return {
            "date": date,
            "basic": basic,
            "collectors": collectors,
            "top_ips": top_ips,
        }

    def export_to_json(self, trade_date: str = None, output_file: str = None) -> str:
        """
        导出为 JSON 文件

        Args:
            trade_date: 交易日期（可选）
            output_file: 输出文件路径（可选）

        Returns:
            输出文件路径
        """
        if output_file is None:
            from system.config.settings import STORAGE_DIR

            if trade_date:
                output_file = str(STORAGE_DIR / f"ip_stats_{trade_date}.json")
            else:
                output_file = str(STORAGE_DIR / "ip_stats_all.json")

        # 获取所有数据
        usage_records = self.get_ip_usage(trade_date=trade_date)
        ip_stats = self.get_ip_stats(trade_date=trade_date)
        collector_stats = self.get_collector_stats(trade_date=trade_date)

        # 构建导出数据
        export_data = {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": trade_date or "all",
            "usage_records": usage_records,
            "ip_stats": ip_stats,
            "collector_stats": collector_stats,
        }

        # 写入文件
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)

        return output_file

    def clear_old_data(self, days: int = 30):
        """
        清理旧数据

        Args:
            days: 保留最近多少天的数据
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        cursor.execute("DELETE FROM ip_usage WHERE trade_date < ?", (cutoff_date,))
        deleted = cursor.rowcount

        conn.commit()
        conn.close()

        return deleted


# ==================== 快捷函数 ====================

_stats_instance = None


def _get_stats():
    global _stats_instance
    if _stats_instance is None:
        _stats_instance = IPStatsManager()
    return _stats_instance


def record_ip_usage(
    ip: str,
    port: int,
    trade_date: str,
    collector_name: str,
    page: int,
    status: str,
    error: str = None,
):
    """快捷记录 IP 使用"""
    _get_stats().record_usage(ip, port, trade_date, collector_name, page, status, error)


def get_ip_detail(ip: str) -> Optional[Dict]:
    """
    获取 IP 详情（归属地）

    TODO: 接入 IP 归属地查询 API
    """
    conn = sqlite3.connect(_get_stats().db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM ip_details WHERE ip = ?", (ip,))
    row = cursor.fetchone()

    conn.close()

    if row:
        return dict(row)
    return None
