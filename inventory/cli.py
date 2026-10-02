"""命令行入口：add / receive / issue / show 四个子命令。

约定：
- 参数与业务规则错误 -> 退出码 2，原因写入 stderr，stdout 为空；
- 数据库无法打开或读写 -> 退出码 1；
- 成功 -> 退出码 0，stdout 输出单个 JSON 对象。
"""

import argparse
import json
import re
import sys

from .storage import (
    DatabaseError,
    InsufficientStockError,
    InventoryDB,
    ProductExistsError,
)

INTEGER_RE = re.compile(r"^\d+$")


class ArgumentParser(argparse.ArgumentParser):
    """让参数错误以简洁中文提示输出到 stderr，退出码仍为 2。"""

    def error(self, message):
        self.exit(2, f"错误: {message}\n")


def positive_int(value):
    """只接受由数字组成且大于零的整数，拒绝小数、负数、零和非数字。"""
    text = str(value)
    if not INTEGER_RE.match(text) or int(text) <= 0:
        raise argparse.ArgumentTypeError(
            f"数量必须是大于零的整数，收到: {value!r}"
        )
    return int(text)


def clean_text(value, field):
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field}去掉两端空白后不能为空")
    return text


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_parser():
    parser = ArgumentParser(prog="inventory", description="本地库存台账")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="登记商品，初始数量为零")
    p_add.add_argument("--sku", required=True, help="商品 SKU（区分大小写）")
    p_add.add_argument("--name", required=True, help="商品名称")

    for cmd in ("receive", "issue"):
        p = sub.add_parser(cmd, help="入库" if cmd == "receive" else "出库")
        p.add_argument("--sku", required=True, help="商品 SKU（区分大小写）")
        p.add_argument("--qty", required=True, type=positive_int, help="正整数数量")

    p_show = sub.add_parser("show", help="查询商品信息与完整流水")
    p_show.add_argument("--sku", required=True, help="商品 SKU（区分大小写）")
    return parser


def run(argv):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        db = InventoryDB(args.db)
    except DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        if args.command == "add":
            try:
                sku = clean_text(args.sku, "SKU")
                name = clean_text(args.name, "名称")
            except ValueError as exc:
                print(f"错误: {exc}", file=sys.stderr)
                return 2
            try:
                product = db.add_product(sku, name)
            except ProductExistsError as exc:
                print(f"错误: {exc}", file=sys.stderr)
                return 2
            emit(product)
            return 0

        # receive / issue / show 都需要先校验 SKU 并查找商品。
        try:
            sku = clean_text(args.sku, "SKU")
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2

        if args.command == "show":
            product = db.get_product(sku)
            if product is None:
                print(f"错误: 商品不存在: {sku}", file=sys.stderr)
                return 2
            product["movements"] = db.list_movements(sku)
            emit(product)
            return 0

        mtype = args.command  # receive 或 issue
        try:
            result = db.move(sku, mtype, args.qty)
        except InsufficientStockError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        if result is None:
            print(f"错误: 商品不存在: {sku}", file=sys.stderr)
            return 2
        emit(result["product"])
        return 0
    except DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()


def main():
    sys.exit(run(sys.argv[1:]))
