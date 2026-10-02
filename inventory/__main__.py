"""命令行入口：python -m inventory --db <文件路径> <子命令> ...

子命令：
  add     --sku SKU --name NAME   登记商品，初始数量为零
  receive --sku SKU --qty N       入库，增加数量并记录一条流水
  issue   --sku SKU --qty N       出库，扣减数量并记录一条流水
  show    --sku SKU               查询商品名称、当前数量和完整流水
"""

import argparse
import json
import sqlite3
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku      TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    quantity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sku      TEXT NOT NULL,
    type     TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    balance  INTEGER NOT NULL
);
"""


def fail(message, code):
    """打印错误到标准错误并以指定退出码结束，标准输出保持为空。"""
    print(message, file=sys.stderr)
    sys.exit(code)


def parse_qty(raw):
    """数量只接受大于零的整数，其余输入一律拒绝。"""
    text = raw.strip()
    if not text or not text.isascii() or not text.isdigit():
        fail("无效数量：%r（只接受大于零的整数）" % raw, 2)
    value = int(text)
    if value <= 0:
        fail("无效数量：%r（只接受大于零的整数）" % raw, 2)
    return value


def trimmed(value, label):
    text = value.strip()
    if not text:
        fail("%s去掉两端空白后不能为空" % label, 2)
    return text


def find_product(conn, sku):
    row = conn.execute(
        "SELECT sku, name, quantity FROM products WHERE sku = ?", (sku,)
    ).fetchone()
    if row is None:
        fail("商品不存在：%s" % sku, 2)
    return row


def emit(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_add(conn, args):
    sku = trimmed(args.sku, "SKU")
    name = trimmed(args.name, "名称")
    try:
        conn.execute(
            "INSERT INTO products (sku, name, quantity) VALUES (?, ?, 0)",
            (sku, name),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        fail("商品已登记：%s" % sku, 2)
    emit({"sku": sku, "name": name, "quantity": 0})


def cmd_move(conn, args, move_type):
    sku = trimmed(args.sku, "SKU")
    qty = parse_qty(args.qty)
    _, name, balance = find_product(conn, sku)
    if move_type == "receive":
        new_balance = balance + qty
    else:
        if qty > balance:
            fail("出库数量 %d 超过当前余额 %d" % (qty, balance), 2)
        new_balance = balance - qty
    try:
        # 余额更新与流水记录在同一事务中，同时生效或同时不生效。
        with conn:
            conn.execute(
                "UPDATE products SET quantity = ? WHERE sku = ?",
                (new_balance, sku),
            )
            conn.execute(
                "INSERT INTO movements (sku, type, quantity, balance)"
                " VALUES (?, ?, ?, ?)",
                (sku, move_type, qty, new_balance),
            )
    except sqlite3.Error as exc:
        fail("数据库写入失败：%s" % exc, 1)
    emit({"sku": sku, "name": name, "quantity": new_balance})


def cmd_show(conn, args):
    sku = trimmed(args.sku, "SKU")
    _, name, quantity = find_product(conn, sku)
    rows = conn.execute(
        "SELECT id, type, quantity, balance FROM movements"
        " WHERE sku = ? ORDER BY id ASC",
        (sku,),
    ).fetchall()
    movements = [
        {"id": row[0], "type": row[1], "quantity": row[2], "balance": row[3]}
        for row in rows
    ]
    emit(
        {
            "sku": sku,
            "name": name,
            "quantity": quantity,
            "movements": movements,
        }
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="inventory", description="本地库存台账")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="登记商品")
    p_add.add_argument("--sku", required=True)
    p_add.add_argument("--name", required=True)

    p_receive = sub.add_parser("receive", help="入库")
    p_receive.add_argument("--sku", required=True)
    p_receive.add_argument("--qty", required=True)

    p_issue = sub.add_parser("issue", help="出库")
    p_issue.add_argument("--sku", required=True)
    p_issue.add_argument("--qty", required=True)

    p_show = sub.add_parser("show", help="查询商品与流水")
    p_show.add_argument("--sku", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        conn = sqlite3.connect(args.db)
        conn.executescript(SCHEMA)
    except sqlite3.Error as exc:
        fail("无法打开或初始化数据库 %s：%s" % (args.db, exc), 1)
    try:
        if args.command == "add":
            cmd_add(conn, args)
        elif args.command == "receive":
            cmd_move(conn, args, "receive")
        elif args.command == "issue":
            cmd_move(conn, args, "issue")
        else:
            cmd_show(conn, args)
    except sqlite3.Error as exc:
        fail("数据库读写失败：%s" % exc, 1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
