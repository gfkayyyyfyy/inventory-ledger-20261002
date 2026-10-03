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
import unicodedata

from .storage import (
    DatabaseError,
    InsufficientStockError,
    InventoryDB,
    MAX_QTY,
    ProductExistsError,
    StockOverflowError,
)

# --after-id 专用：\d 按 Unicode 语义接受各文字的十进制数字字符；
# \A...\Z 锚定整个文本，不允许开头、结尾或中间出现任何空白
# （$ 会容忍末尾一个换行，故不能用）。
INTEGER_RE = re.compile(r"\A\d+\Z")


def decimal_digits(text):
    """把 Unicode 十进制数字文本逐字符转写为 ASCII 数字（0 至 9）。

    Python 的 int() 与正则 \\d 都按 Unicode 语义接受各文字的十进制数字
    （如全角 U+FF10..FF19、阿拉伯印度 U+0660..U+0669、U+06F0..U+06F9
    的扩展阿拉伯印度数字等），但 int() 还容忍空白与下划线，逐字符转换可以
    精确限定只接受十进制数字字符；遇到任一非数字字符返回 None。
    """
    digits = []
    for ch in text:
        try:
            value = unicodedata.decimal(ch)
        except ValueError:
            return None
        digits.append(str(value))
    return "".join(digits)


# --limit 专用：[0-9] 只匹配 ASCII 数字，拒绝全角数字（２，U+FF12）与
# 阿拉伯印度数字（٢，U+0662）等 Unicode 数字字符；\A...\Z 锚定整个文本，
# 不允许开头、结尾或中间出现任何空白（$ 会容忍末尾一个换行，故不能用）。
LIMIT_DIGITS_RE = re.compile(r"\A[0-9]+\Z")

# SQLite INTEGER 主键的最大值（64 位有符号整数上界）。
MAX_AFTER_ID = 9223372036854775807

# show --limit 单次最多返回的流水条数。
MAX_LIMIT = 1000


class ArgumentParser(argparse.ArgumentParser):
    """让参数错误以简洁中文提示输出到 stderr，退出码仍为 2。"""

    def error(self, message):
        self.exit(2, f"错误: {message}\n")


def positive_int(value):
    """只接受 1 至 MAX_QTY 的十进制正整数，允许前导零（不改变数量含义）。

    十进制数字以 Unicode 十进制数字字符为准：ASCII 0-9、全角数字
    （U+FF10 等）、阿拉伯印度数字（U+0660 等）及其他文字的十进制
    数字字符都按同一数值处理。零（含 "0"、"000"、"０"、"٠"、
    "0０٠" 等任意零字符组合）、负数、小数、非数字按“数量无效”拒绝；
    超过 MAX_QTY 时提示中包含 --qty 与允许的上限。两类错误都由
    argparse 以退出码 2 拒绝，且发生在打开数据库之前。
    """
    text = str(value)
    # 逐字符按 Unicode 十进制数值转写，等价于 int() 能接受的数字集合，
    # 但显式拒绝 int() 同样容忍的空白与下划线，确保“纯十进制数字文本”。
    digits = decimal_digits(text)
    if digits is None:
        raise argparse.ArgumentTypeError(
            f"数量必须是大于零的整数，收到: {value!r}"
        )
    # 只比较数值而不把整段文本交给 int()：Python 3.11 起默认对超过 4300
    # 个数字的整数字符串转换抛出 ValueError（且不要求用户调整解释器设置）。
    # 前导零（任何文字的十进制零字符）不改变数值，先去掉再判断，因此
    # 5000 个 0 后接 10 与普通参数 10 完全等价，参数文本长度不受限制。
    digits = digits.lstrip("0")
    if not digits:
        # 整段都是零字符：实际数值为零，按数量无效拒绝。
        raise argparse.ArgumentTypeError(
            f"数量必须是大于零的整数，收到: {value!r}"
        )
    # 有效数字比上限的位数还多，数值必然超过上限，无需转换即可拒绝。
    if len(digits) > len(str(MAX_QTY)):
        raise argparse.ArgumentTypeError(
            f"--qty 不能超过单次数量上限 {MAX_QTY}，收到: {value!r}"
        )
    number = int(digits)  # 位数不超过上限位数，转换不受默认位数限制影响
    if number > MAX_QTY:
        raise argparse.ArgumentTypeError(
            f"--qty 不能超过单次数量上限 {MAX_QTY}，收到: {value!r}"
        )
    return number


def after_id(value):
    """--after-id：0 至 2^63-1 的十进制非负整数，允许前导零。

    完整参数值参与校验，不做任何去空白：含空格、制表符、回车、换行
    （无论在开头、结尾还是中间）一律拒绝。缺值、空字符串、负数、小数、
    正号、非数字或超出范围时同样由 argparse 拒绝（退出码 2，提示中
    包含参数名与原因）。
    """
    text = str(value)
    if not text:
        raise argparse.ArgumentTypeError("--after-id 不能为空")
    if not INTEGER_RE.match(text):
        raise argparse.ArgumentTypeError(
            f"--after-id 必须是十进制非负整数，不能含空白或其他非数字字符，"
            f"收到: {value!r}"
        )
    # 只比较数值而不把整段文本交给 int()：Python 3.11 起默认对超过 4300
    # 个数字的整数字符串转换抛出 ValueError（且不要求用户调整解释器设置）。
    # 前导零不改变数值，先去掉再判断，因此 5000 个 0 后接 1 与普通参数 1
    # 完全等价，5000 个 0 本身等价于 0，参数文本长度不受限制。
    digits = text.lstrip("0")
    if not digits:
        # 整段都是前导零：数值为零，是合法下界（0 表示从最早流水开始）。
        return 0
    # 有效数字比上限的位数还多，数值必然超过上限（20 位十进制数至少为
    # 10^19），无需转换即可拒绝。
    if len(digits) > len(str(MAX_AFTER_ID)):
        raise argparse.ArgumentTypeError(
            f"--after-id 不能超过 {MAX_AFTER_ID}，收到: {value!r}"
        )
    # 位数不超过上限位数（19 位），转换不受默认位数限制影响，再精确比较。
    number = int(digits)
    if number > MAX_AFTER_ID:
        raise argparse.ArgumentTypeError(
            f"--after-id 不能超过 {MAX_AFTER_ID}，收到: {value!r}"
        )
    return number


def limit_count(value):
    """--limit：1 至 1000 的十进制正整数，只允许 ASCII 数字 0 至 9，允许前导零。

    完整参数值参与校验，不做任何去空白或数字字符转换：含空格、制表符、
    回车、换行（无论在开头、结尾还是中间）一律拒绝；全角数字、阿拉伯印度
    数字等非 ASCII 数字字符及其与 ASCII 数字混写也一律拒绝。
    缺值、空字符串、零、负数、小数、正号、非数字或超过 1000 时
    由 argparse 拒绝（退出码 2，提示中包含 --limit 与拒绝原因）。
    前导零不改变数量含义（0002 与 2 等价）。
    """
    text = str(value)
    if not text:
        raise argparse.ArgumentTypeError("--limit 不能为空")
    if not LIMIT_DIGITS_RE.match(text):
        raise argparse.ArgumentTypeError(
            f"--limit 必须是只含 ASCII 数字 0 至 9 的十进制正整数，"
            f"不能含空白或其他数字字符，收到: {value!r}"
        )
    # 只比较数值而不把整段文本交给 int()：Python 3.11 起默认对超过 4300
    # 个数字的整数字符串转换抛出 ValueError（且不要求用户调整解释器设置）。
    # 文本已由上面的正则保证只含 ASCII 数字，去掉前导零后按位数与逐字比较
    # 即可判断 1 至 1000 的范围，参数文本长度不受限制；前导零不改变数值，
    # 因此 5000 个 0 后接 1 与普通参数 1 完全等价。
    digits = text.lstrip("0")
    if not digits:
        raise argparse.ArgumentTypeError(
            f"--limit 必须是 1 至 {MAX_LIMIT} 的正整数，收到: {value!r}"
        )
    upper = str(MAX_LIMIT)
    if len(digits) > len(upper) or (len(digits) == len(upper) and digits > upper):
        raise argparse.ArgumentTypeError(
            f"--limit 不能超过单次返回条数上限 {MAX_LIMIT}，收到: {value!r}"
        )
    return int(digits)


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
    p_show.add_argument(
        "--type",
        choices=("receive", "issue"),
        help="按流水类型筛选：receive（入库）或 issue（出库）；缺省返回全部流水",
    )
    p_show.add_argument(
        "--after-id",
        type=after_id,
        default=None,
        metavar="ID",
        help=(
            "只返回该 SKU 中编号严格大于 ID 的流水（0 表示从最早流水开始）；"
            "接受 0 至 9223372036854775807 的十进制整数，允许前导零，不能含空白"
        ),
    )
    p_show.add_argument(
        "--limit",
        type=limit_count,
        default=None,
        metavar="N",
        help=(
            "在 --sku/--type/--after-id 筛选后，按原始 id 升序只返回前 N 条流水；"
            "接受 1 至 1000、仅由 ASCII 数字 0 至 9 组成的非空文本，允许前导零；"
            "缺省返回全部匹配流水"
        ),
    )
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
            product["movements"] = db.list_movements(
                sku, args.type, args.after_id, args.limit
            )
            emit(product)
            return 0

        mtype = args.command  # receive 或 issue
        try:
            result = db.move(sku, mtype, args.qty)
        except (InsufficientStockError, StockOverflowError) as exc:
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
