"""SQLite 持久化层：商品表与流水表。

余额（products.quantity）与流水（movements）在同一个事务内更新，
借助 UNIQUE 主键保证 SKU 不重复、CHECK 约束保证余额非负。
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku      TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0)
);
CREATE TABLE IF NOT EXISTS movements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sku      TEXT NOT NULL REFERENCES products(sku),
    type     TEXT NOT NULL CHECK (type IN ('receive', 'issue')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    balance  INTEGER NOT NULL CHECK (balance >= 0)
);
"""


class DatabaseError(Exception):
    """数据库无法打开或读写。"""


class ProductExistsError(Exception):
    """SKU 已登记。"""


class InsufficientStockError(Exception):
    """出库数量超过当前余额。"""


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

        商品不存在返回 None；出库超过余额抛 InsufficientStockError。
        """
        product = self.get_product(sku)
        if product is None:
            return None
        if mtype == "issue" and qty > product["quantity"]:
            raise InsufficientStockError(
                f"出库数量 {qty} 超过当前余额 {product['quantity']}"
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
                cur = self.conn.execute(
                    "INSERT INTO movements (sku, type, quantity, balance) "
                    "VALUES (?, ?, ?, ?)",
                    (sku, mtype, qty, balance),
                )
                movement_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            # CHECK 兜底：余额被扣成负数（如并发场景）也按超量处理，事务已回滚。
            raise InsufficientStockError(
                f"出库数量 {qty} 超过当前余额 {product['quantity']}"
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

    def list_movements(self, sku, mtype=None, after_id=None):
        """返回该商品按 id 升序的流水；mtype 非空时只返回对应类型，
        after_id 非空时只返回 id 严格大于该值的流水，两个条件可同时生效。

        筛选只影响返回的行，保留原始 id 与 balance，不重新编号或重算余额。
        """
        sql = (
            "SELECT id, type, quantity, balance FROM movements "
            "WHERE sku = ?"
        )
        params = [sku]
        if mtype is not None:
            sql += " AND type = ?"
            params.append(mtype)
        if after_id is not None:
            sql += " AND id > ?"
            params.append(after_id)
        sql += " ORDER BY id ASC"
        try:
            cur = self.conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取数据库失败: {exc}") from exc
        return [
            {"id": row[0], "type": row[1], "quantity": row[2], "balance": row[3]}
            for row in cur.fetchall()
        ]
