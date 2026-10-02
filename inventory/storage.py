"""SQLite 持久化层：商品表与流水表。

余额（products.quantity）与流水（movements）在同一个事务内更新，
借助 UNIQUE 主键保证 SKU 不重复、CHECK 约束保证余额在 0 至 2^63-1 之间。

所有数量与余额均为 64 位有符号整数范围内的精确整数。SQLite 在整数运算
超过 2^63-1 时会悄悄退化为 REAL（浮点）而丢精度，因此入库的余额上界在
Python 层用精确整数预先判定，绝不把超界值交给 SQLite 计算。
"""

import sqlite3

# 单次出入库数量与库存余额共用的上界：64 位有符号整数最大值。
MAX_QTY = 9223372036854775807

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku      TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0 AND quantity <= %d)
);
CREATE TABLE IF NOT EXISTS movements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sku      TEXT NOT NULL REFERENCES products(sku),
    type     TEXT NOT NULL CHECK (type IN ('receive', 'issue')),
    quantity INTEGER NOT NULL CHECK (quantity > 0 AND quantity <= %d),
    balance  INTEGER NOT NULL CHECK (balance >= 0 AND balance <= %d)
);
""" % (MAX_QTY, MAX_QTY, MAX_QTY)


class DatabaseError(Exception):
    """数据库无法打开或读写。"""


class ProductExistsError(Exception):
    """SKU 已登记。"""


class InsufficientStockError(Exception):
    """出库数量超过当前余额。"""


class StockOverflowError(Exception):
    """入库后余额超过库存上界 2^63-1。"""


class InventoryDB:
    def __init__(self, path):
        try:
            self.conn = sqlite3.connect(path)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        except sqlite3.Error as exc:
            raise DatabaseError(f"无法打开或初始化数据库 {path}: {exc}") from exc

    def close(self):
        self.conn.close()

    def add_product(self, sku, name):
        """登记新商品，初始数量为零。"""
        try:
            self.conn.execute(
                "INSERT INTO products (sku, name, quantity) VALUES (?, ?, 0)",
                (sku, name),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ProductExistsError(f"商品已存在: {sku}") from exc
        except sqlite3.Error as exc:
            self.conn.rollback()
            raise DatabaseError(f"写入数据库失败: {exc}") from exc
        return {"sku": sku, "name": name, "quantity": 0}

    def get_product(self, sku):
        try:
            cur = self.conn.execute(
                "SELECT sku, name, quantity FROM products WHERE sku = ?",
                (sku,),
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取数据库失败: {exc}") from exc
        row = cur.fetchone()
        if row is None:
            return None
        return {"sku": row[0], "name": row[1], "quantity": row[2]}

    def move(self, sku, mtype, qty):
        """入库/出库：更新余额并插入流水，二者在同一事务内同时生效。

        商品不存在返回 None；出库超过余额抛 InsufficientStockError；
        入库后余额超过 MAX_QTY 抛 StockOverflowError。
        所有拒绝都发生在写入之前，余额与流水均不改变。
        """
        # 防御性校验：正常入口已由 CLI 的 positive_int 保证，此处确保
        # 直接调用本层时也不会把超界整数交给 SQLite（绑定会抛 OverflowError，
        # 而 SQL 运算超界会退化成 REAL 丢精度）。
        if not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            raise ValueError(f"数量必须是 1 至 {MAX_QTY} 的整数，收到: {qty!r}")
        product = self.get_product(sku)
        if product is None:
            return None
        current = product["quantity"]
        if mtype == "receive":
            # 用 Python 任意精度整数预判，避免 SQLite 把溢出算成 REAL。
            if current + qty > MAX_QTY:
                raise StockOverflowError(
                    f"入库后库存余额不能超过上限 {MAX_QTY}："
                    f"当前余额 {current}，本次入库 {qty}"
                )
        elif qty > current:
            raise InsufficientStockError(
                f"出库数量 {qty} 超过当前余额 {current}"
            )
        delta = qty if mtype == "receive" else -qty
        try:
            # with 块在无异常时提交、有异常时回滚，余额与流水同生共死。
            with self.conn:
                self.conn.execute(
                    "UPDATE products SET quantity = quantity + ? WHERE sku = ?",
                    (delta, sku),
                )
                balance = self.conn.execute(
                    "SELECT quantity FROM products WHERE sku = ?", (sku,)
                ).fetchone()[0]
                # 写入后再核验一次类型与范围：任何 REAL/超界都视为失败并回滚。
                if not isinstance(balance, int) or not 0 <= balance <= MAX_QTY:
                    raise sqlite3.IntegrityError(
                        f"余额必须是 0 至 {MAX_QTY} 的整数，得到: {balance!r}"
                    )
                cur = self.conn.execute(
                    "INSERT INTO movements (sku, type, quantity, balance) "
                    "VALUES (?, ?, ?, ?)",
                    (sku, mtype, qty, balance),
                )
                movement_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            # CHECK/并发兜底：事务已回滚，按入库超界或出库超量给出对应错误。
            if mtype == "receive":
                raise StockOverflowError(
                    f"入库后库存余额不能超过上限 {MAX_QTY}："
                    f"当前余额 {current}，本次入库 {qty}"
                ) from exc
            raise InsufficientStockError(
                f"出库数量 {qty} 超过当前余额 {current}"
            ) from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"写入数据库失败: {exc}") from exc
        return {
            "product": {"sku": sku, "name": product["name"], "quantity": balance},
            "movement": {
                "id": movement_id,
                "type": mtype,
                "quantity": qty,
                "balance": balance,
            },
        }

    def list_movements(self, sku, mtype=None, after_id=None, limit=None):
        """返回该商品按 id 升序的流水；可按类型、编号下界筛选并限定条数。

        - mtype 非空时只返回对应类型；
        - after_id 非空时只返回 id 严格大于该值的流水，下界无需真实存在，
          也无需属于当前 SKU；
        - limit 非空时在上述筛选与 id 升序排序之后只取前 limit 条，
          其他商品的流水不占名额。
        筛选只影响返回的行，保留原始 id 与 balance，不重新编号或重算余额。
        """
        # 防御性校验：正常入口已由 CLI 的 limit_count 保证，此处确保
        # 直接调用本层时也不会把非法值交给 SQL 的 LIMIT。
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise ValueError(f"limit 必须是正整数，收到: {limit!r}")
        sql = (
            "SELECT id, type, quantity, balance FROM movements "
            "WHERE sku = ?"
        )
        params = [sku]
        if after_id is not None:
            sql += " AND id > ?"
            params.append(after_id)
        if mtype is not None:
            sql += " AND type = ?"
            params.append(mtype)
        sql += " ORDER BY id ASC"
        if limit is not None:
            # LIMIT 在 WHERE 筛选与排序之后生效，截断的只是当前 SKU
            # 已筛选的结果，其他商品的行不参与计数。
            sql += " LIMIT ?"
            params.append(limit)
        try:
            cur = self.conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取数据库失败: {exc}") from exc
        return [
            {"id": row[0], "type": row[1], "quantity": row[2], "balance": row[3]}
            for row in cur.fetchall()
        ]
