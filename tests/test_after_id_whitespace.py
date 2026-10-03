"""--after-id 完整参数值校验的回归测试：拒绝任何位置的空白。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证：

- --after-id 完整参数值参与校验，不先去掉空白：含空格、制表符、回车或
  实际换行（无论空白位于开头、中间还是结尾）一律以退出码 2 拒绝，
  标准输出为空，标准错误包含 --after-id 与“不能含空白”的原因，
  不输出异常堆栈；
- 数字 1 后附单个实际换行符（U+000A）、全零文本后附换行、数千个前导零
  后附换行采用同一拒绝结果（此前 ^...$ 锚点的 $ 容忍末尾一个换行，
  导致 "1\\n" 被当作下界 1 接受）；
- 拒绝时已有商品名称、库存数量和流水保持不变；指定数据库文件尚不存在时
  不创建该文件；即使 SKU 为空或不存在，也先报告下界参数错误；与合法的
  --type、--limit 同用时空白下界仍被拒绝；
- 合法下界 0、0001、9223372036854775807、5000 个前导零后接 1 的既有
  语义保持不变，原有数字字符（含全角数字等 Unicode 十进制数字）接受
  范围不收窄；缺值、空字符串、负数、小数、正号、非数字、超过上限的
  下界继续按既有参数错误处理。

从项目根目录执行：

    python -m unittest discover
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SKU = "DEMO-1"
NAME = "演示螺母"
MISSING_SKU = "NOT-EXIST"

MAX_ID_TEXT = "9223372036854775807"

# 远超 Python 3.11+ 默认整数字符串转换位数上限（4300）的前导零个数。
LEADING_ZEROS = "0" * 5000
LONG_ONE = LEADING_ZEROS + "1"


class InventoryCLITestCase(unittest.TestCase):
    """每个用例使用独立的临时数据库，互不依赖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")

    def run_cli(self, *args, db_path=None):
        """运行一条 CLI 命令，返回 (退出码, stdout 文本, stderr 文本)。"""
        proc = subprocess.run(
            [
                sys.executable, "-m", "inventory",
                "--db", self.db_path if db_path is None else db_path,
                *args,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run_ok(self, *args):
        """运行应成功的命令，返回解析后的 JSON 对象。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"命令 {args[:4]}... 应成功，stderr: {err}")
        payload = json.loads(out)  # stdout 必须是单个可解析的 JSON 对象
        self.assertIsInstance(payload, dict)
        return payload

    def assert_whitespace_error(self, code, out, err):
        """空白下界的统一参数错误约定。"""
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--after-id", err)
        self.assertIn("不能含空白", err)
        self.assertNotIn("Traceback", err, "参数拒绝不应输出异常堆栈")

    def seed_demo(self):
        """登记 DEMO-1（演示螺母），入库 10、出库 3，流水编号为 1、2。"""
        self.assertEqual(
            self.run_ok("add", "--sku", SKU, "--name", NAME),
            {"sku": SKU, "name": NAME, "quantity": 0},
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "3")["quantity"], 7
        )

    def show(self, after_id=...):
        args = ["show", "--sku", SKU]
        if after_id is not ...:
            args += ["--after-id", after_id]
        return self.run_ok(*args)


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestAfterIdWhitespaceAcceptance(InventoryCLITestCase):
    """验收主场景：合法查询成功，数字后附实际换行被拒绝，数据不变。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_legal_after_id_one_returns_only_second_movement(self):
        # --after-id 1：只返回编号严格大于 1 的流水（编号 2：出库 3，余额 7）。
        page = self.show(after_id="1")
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
        )
        # 查询不改变当前数量。
        self.assertEqual(self.show()["quantity"], 7)

    def test_digit_one_with_literal_newline_rejected_then_full_query_intact(self):
        # 数字 1 后附单个实际换行符 U+000A：按空白参数错误拒绝，
        # 而不是被当作下界 1 接受。
        code, out, err = self.run_cli(
            "show", "--sku", SKU, "--after-id", "1\n"
        )
        self.assert_whitespace_error(code, out, err)
        # 拒绝后再次完整查询，仍是原来的两条流水，当前数量仍为 7。
        page = self.show()
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )


class TestAfterIdWhitespaceRejected(InventoryCLITestCase):
    """任何位置的任何空白均以同一参数错误约定拒绝。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()
        self.before = self.show()
        self.assertEqual(
            movement_tuples(self.before["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_whitespace_in_every_position_rejected(self):
        # 空格、制表符、回车、换行；位于开头、结尾或中间。
        bad_values = (
            "1\n", "\n1", "1\n2",       # 换行：结尾/开头/中间（结尾为本次修复边界）
            "1 ", " 1", "1 2",          # 空格：结尾/开头/中间
            "1\t", "\t1", "1\t2",       # 制表符：结尾/开头/中间
            "1\r", "\r1", "1\r2",       # 回车：结尾/开头/中间
            " 1 \t\r\n",                # 多种空白混用
        )
        for bad in bad_values:
            with self.subTest(after_id=bad):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assert_whitespace_error(code, out, err)
                # 每次拒绝后商品与流水均无变化。
                self.assertEqual(self.show(), self.before)

    def test_all_zero_text_with_newline_rejected(self):
        # 全零文本后附换行：与 "1\n" 采用同一拒绝结果，而不是等价于合法下界 0。
        for bad in ("0\n", "000\n", "000\r", "0\t"):
            with self.subTest(after_id=bad):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assert_whitespace_error(code, out, err)
                self.assertEqual(self.show(), self.before)

    def test_thousands_of_leading_zeros_with_newline_rejected(self):
        # 数千个前导零后附换行，以及前导零后接 1 再附换行：同样拒绝，
        # 不因文本很长或先经过前导零处理而漏过末尾换行。
        for bad in (
            LONG_ONE + "\n",       # 5000 个前导零后接 1，再附换行
            LEADING_ZEROS + "\n",  # 5000 个 0 后附换行
            "\n" + LONG_ONE,       # 换行位于开头
            LEADING_ZEROS[:2500] + " " + LEADING_ZEROS[:2500] + "1",
        ):
            with self.subTest(length=len(bad)):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assert_whitespace_error(code, out, err)
                self.assertEqual(self.show(), self.before)

    def test_rejection_with_type_and_limit_still_rejected(self):
        # 空白下界与合法的 --type、--limit 同用：参数错误优先，不进行查询。
        for extra in (
            ["--type", "issue"],
            ["--limit", "5"],
            ["--type", "issue", "--limit", "1"],
        ):
            with self.subTest(extra=tuple(extra)):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, *extra, "--after-id", "1\n"
                )
                self.assert_whitespace_error(code, out, err)
                self.assertEqual(self.show(), self.before)

    def test_rejection_with_missing_or_blank_sku_reported_first(self):
        # SKU 不存在：先报告 --after-id 参数错误，不进入商品查询。
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--after-id", "1\n"
        )
        self.assert_whitespace_error(code, out, err)
        self.assertNotIn("商品不存在", err)
        # SKU 去掉两端空白后为空：仍先报告下界参数错误，不报告 SKU 错误。
        code, out, err = self.run_cli(
            "show", "--sku", "   ", "--after-id", "0\n"
        )
        self.assert_whitespace_error(code, out, err)
        self.assertNotIn("不能为空", err)

    def test_rejection_does_not_create_database_file(self):
        # 参数校验发生在打开数据库之前：文件尚不存在时不得创建。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("1\n", LEADING_ZEROS + "1\n", "000\n", "\t1", "1 2"):
            with self.subTest(after_id=bad[:8]):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad, db_path=missing_db
                )
                self.assert_whitespace_error(code, out, err)
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )


class TestAfterIdValidFormsUnchanged(InventoryCLITestCase):
    """合法文本与既有非法短文本的语义在修复后保持不变。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_valid_bounds_keep_meaning(self):
        # 0：从最早流水开始，返回全部两条流水。
        self.assertEqual(
            movement_tuples(self.show(after_id="0")["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        # 前导零不改变数值：0001 与 1 等价。
        self.assertEqual(
            movement_tuples(self.show(after_id="0001")["movements"]),
            [(2, "issue", 3, 7)],
        )
        # 64 位有符号整数上界合法，其后没有流水。
        self.assertEqual(self.show(after_id=MAX_ID_TEXT)["movements"], [])
        # 5000 个前导零后接 1 与普通写法 1 等价。
        self.assertEqual(self.show(after_id=LONG_ONE), self.show(after_id="1"))
        # 5000 个 0 与下界 0 等价。
        self.assertEqual(
            self.show(after_id=LEADING_ZEROS), self.show(after_id="0")
        )

    def test_unicode_digit_range_not_narrowed(self):
        # 原有数字字符接受范围不收窄：全角数字 １（U+FF11）与 ASCII 1 等价。
        page = self.show(after_id="１")
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
        )
        # 但全角数字旁附实际换行仍属空白，必须拒绝。
        code, out, err = self.run_cli(
            "show", "--sku", SKU, "--after-id", "１\n"
        )
        self.assert_whitespace_error(code, out, err)

    def test_legacy_invalid_forms_still_rejected(self):
        # 缺值以外的既有非法形式继续按参数错误处理。
        for bad in (
            "",        # 空字符串
            "-1",      # 负数
            "1.5",     # 小数
            "+1",      # 带正号
            "abc",     # 非数字
            "0x1",     # 非十进制
            " 1",      # 含空格（原本就拒绝）
            "9223372036854775808",  # 超出 64 位上界
        ):
            with self.subTest(after_id=bad):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--after-id", err)
                self.assertNotIn("Traceback", err)
        # 缺值：argparse 以退出码 2 拒绝，stdout 为空。
        code, out, err = self.run_cli("show", "--sku", SKU, "--after-id")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--after-id", err)


if __name__ == "__main__":
    unittest.main()
