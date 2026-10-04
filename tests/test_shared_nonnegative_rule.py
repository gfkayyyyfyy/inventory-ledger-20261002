"""--after-id 与 --threshold 共用校验规则的重构回归测试。

两个入口在 inventory/cli.py 中维护同一套非负整数规则，本测试验证重构后
行为完全兼容：

- 直接调用校验入口：二者对相同文本给出相同整数（ASCII、全角、阿拉伯印度
  数字及混写同值，前导零不改变结果，5000 个零等价于 0、其后接 1 等价于 1，
  上限 9223372036854775807 成功），非法文本均抛
  argparse.ArgumentTypeError，且错误信息只含各自参数名，不串用另一参数名；
- 命令行验收：DEMO-1 入库 10 再出库 3 后，show --after-id 0001 与全角 １
  查询结果相同（仅返回出库流水，当前数量与流水余额均为 7）；
  low-stock --threshold 0007 与全角 ７ 结果相同（包含数量为 7 的该商品）；
- 拒绝边界：上限加一、5000 个九、空字符串、正负号、小数、下划线、非十进制
  数字字符及任意位置空白（含数字后仅一个换行）均以退出码 2 拒绝，stdout
  为空，stderr 含对应参数名与原因、无堆栈，且不创建尚不存在的数据库文件；
- show 省略 --after-id 仍返回全部流水；low-stock 省略 --threshold 仍按
  参数错误拒绝；流水筛选与 --type、--limit 的组合语义不变，重复查询结果
  与数据库内容一致。

从项目根目录执行：

    python -m unittest discover
"""

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from inventory.cli import after_id, threshold  # noqa: E402

SKU = "DEMO-1"
NAME = "演示螺母"

MAX_VALUE = 9223372036854775807
MAX_TEXT = "9223372036854775807"
OVER_MAX_TEXT = "9223372036854775808"

# 远超 Python 3.11+ 默认整数字符串转换位数上限（4300）的前导零个数。
LEADING_ZEROS = "0" * 5000
LONG_ZERO = LEADING_ZEROS
LONG_ONE = LEADING_ZEROS + "1"
LONG_MAX = LEADING_ZEROS + MAX_TEXT
LONG_NINES = "9" * 5000

# (参数名, 校验入口)：共同规则在两个入口上必须表现一致。
ENTRY_POINTS = (("--after-id", after_id), ("--threshold", threshold))


class SharedRuleDirectCallTest(unittest.TestCase):
    """直接调用校验入口：同值文本同结果，返回 int，拒绝边界一致。"""

    def test_equivalent_texts_return_same_integer(self):
        cases = {
            "0": 0,
            "0007": 7,
            "0001": 1,
            "７": 7,           # 全角 U+FF17
            "１": 1,           # 全角 U+FF11
            "٧": 7,           # 阿拉伯印度 U+0667
            "۷": 7,           # 扩展阿拉伯印度 U+06F7
            "0０٧": 7,         # ASCII、全角、阿拉伯印度零字符混写
            "٠٠٧": 7,          # 阿拉伯印度前导零
            MAX_TEXT: MAX_VALUE,
            LONG_ZERO: 0,
            LONG_ONE: 1,
            LEADING_ZEROS + "０١": 1,  # 长零后接非 ASCII 的 1
            LONG_MAX: MAX_VALUE,
        }
        for text, number in cases.items():
            for option, parse in ENTRY_POINTS:
                with self.subTest(option=option, text=text[:12]):
                    result = parse(text)
                    self.assertIsInstance(result, int)
                    self.assertEqual(result, number)

    def test_both_entries_agree_on_all_inputs(self):
        # 同一文本在两个入口的返回值永远相同。
        for text in ("0", "0001", "１", "٥", "0007", MAX_TEXT, LONG_ZERO,
                     LONG_ONE, LONG_MAX):
            self.assertEqual(after_id(text), threshold(text), text)

    def test_rejection_boundaries_raise_argument_type_error(self):
        bad_values = (
            "",                    # 空字符串
            "-1",                  # 负号
            "+1",                  # 正号
            "1.5", "1.0",          # 小数
            "1_0", "1_",           # 下划线
            "abc", "0x1", "1a",    # 非十进制数字字符
            " 1", "1 ", "1 2",     # 空白：开头/结尾/中间
            "1\n", "0001\n",       # 数字后仅一个换行
            "\n", "1\t", "1\r",    # 其他空白
            "１\n",                # 全角数字后附换行
            OVER_MAX_TEXT,         # 上限加一
            LONG_NINES,            # 5000 个九
            LONG_ONE[:-1] + "x",   # 5000 个零后接非数字
        )
        for option, parse in ENTRY_POINTS:
            other = "--threshold" if option == "--after-id" else "--after-id"
            for bad in bad_values:
                with self.subTest(option=option, bad=bad[:12]):
                    with self.assertRaises(argparse.ArgumentTypeError) as ctx:
                        parse(bad)
                    message = str(ctx.exception)
                    # 错误信息带各自参数名与原因，绝不串用另一参数名。
                    self.assertIn(option, message)
                    self.assertNotIn(other, message)

    def test_empty_value_message_distinct_per_entry(self):
        # 空字符串的提示原本就按参数名区分。
        with self.assertRaises(argparse.ArgumentTypeError) as ctx:
            after_id("")
        self.assertEqual(str(ctx.exception), "--after-id 不能为空")
        with self.assertRaises(argparse.ArgumentTypeError) as ctx:
            threshold("")
        self.assertEqual(str(ctx.exception), "--threshold 不能为空")

    def test_non_integer_objects_rejected_without_passthrough(self):
        # 非字符串输入同样走完整规则：None 转成 "None" 后按非数字拒绝，
        # 不允许绕过文本校验。
        for option, parse in ENTRY_POINTS:
            with self.subTest(option=option):
                with self.assertRaises(argparse.ArgumentTypeError) as ctx:
                    parse(None)
                self.assertIn(option, str(ctx.exception))


class InventoryCLITestCase(unittest.TestCase):
    """每个用例使用独立的临时数据库，互不依赖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")

    def run_cli(self, *args, db_path=None):
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
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"命令 {args[:4]}... 应成功，stderr: {err}")
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        return payload

    def assert_param_error(self, code, out, err, option):
        other = "--threshold" if option == "--after-id" else "--after-id"
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn(option, err)
        self.assertNotIn(other, err, "错误信息不能串用另一参数名")
        self.assertNotIn("Traceback", err, "参数拒绝不应输出异常堆栈")

    def seed_demo(self):
        """登记 DEMO-1，入库 10、出库 3：流水编号 1（余额 10）、2（余额 7）。"""
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

    def show(self, after_id=..., mtype=None, limit=None):
        args = ["show", "--sku", SKU]
        if after_id is not ...:
            args += ["--after-id", after_id]
        if mtype is not None:
            args += ["--type", mtype]
        if limit is not None:
            args += ["--limit", str(limit)]
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"show 应成功，stderr: {err}")
        return json.loads(out)

    def low_stock(self, threshold=...):
        args = ["low-stock"]
        if threshold is not ...:
            args += ["--threshold", threshold]
        code, out, err = self.run_cli(*args)
        return code, out, err


def movement_tuples(movements):
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class CompatibilityAcceptanceTest(InventoryCLITestCase):
    """用户验收场景：同值文本查询结果相同，余额与数量均为 7。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_after_id_leading_zeros_equals_full_width_one(self):
        # show --sku DEMO-1 --after-id 0001 与全角 １ 结果相同：
        # 仅返回第二条出库流水，当前数量与该流水余额均为 7。
        expected = [(2, "issue", 3, 7)]
        for text in ("0001", "１"):
            with self.subTest(after_id=text):
                page = self.show(after_id=text)
                self.assertEqual(page["sku"], SKU)
                self.assertEqual(page["name"], NAME)
                self.assertEqual(page["quantity"], 7)
                self.assertEqual(movement_tuples(page["movements"]), expected)
        self.assertEqual(self.show(after_id="0001"), self.show(after_id="１"))
        # 与 ASCII 短写法、阿拉伯印度数字、5000 个零后接 1 也完全一致。
        self.assertEqual(
            self.show(after_id="0001"),
            self.show(after_id="1"),
        )
        self.assertEqual(
            self.show(after_id="١"), self.show(after_id="0001")
        )
        self.assertEqual(
            self.show(after_id=LONG_ONE), self.show(after_id="0001")
        )

    def test_threshold_leading_zeros_equals_full_width_seven(self):
        # low-stock --threshold 0007 与全角 ７ 结果相同：
        # 包含 DEMO-1 且数量为 7。
        results = []
        for text in ("0007", "７"):
            with self.subTest(threshold=text):
                code, out, err = self.low_stock(text)
                self.assertEqual(code, 0, err)
                payload = json.loads(out)
                self.assertEqual(
                    payload["products"],
                    [{"sku": SKU, "name": NAME, "quantity": 7}],
                )
                results.append(payload)
        self.assertEqual(results[0], results[1])
        # 与 ASCII 7、阿拉伯印度 ٧、5000 个零后接 7 同值。
        for text in ("7", "٧", LEADING_ZEROS + "7"):
            code, out, err = self.low_stock(text)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), results[0])

    def test_repeated_queries_identical_and_database_unchanged(self):
        first_show = self.show(after_id="0001")
        second_show = self.show(after_id="１")
        self.assertEqual(first_show, second_show)
        code, out1, _ = self.low_stock("0007")
        self.assertEqual(code, 0)
        code, out2, _ = self.low_stock("７")
        self.assertEqual(code, 0)
        self.assertEqual(out1, out2)
        # 数据库内容仍是两条原始流水，数量 7。
        full = self.show()
        self.assertEqual(full["quantity"], 7)
        self.assertEqual(
            movement_tuples(full["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_omit_after_id_returns_all_movements(self):
        # show 省略下界仍返回全部匹配流水；与下界 0、5000 个零等价。
        omitted = self.show()
        self.assertEqual(
            movement_tuples(omitted["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        self.assertEqual(omitted, self.show(after_id="0"))
        self.assertEqual(omitted, self.show(after_id=LONG_ZERO))

    def test_after_id_combination_with_type_and_limit_unchanged(self):
        # 长/Unicode 下界与 --type、--limit 的组合语义与短下界一致。
        for bound in ("1", "0001", "１", LONG_ONE):
            with self.subTest(bound=bound[:8]):
                page = self.show(after_id=bound, mtype="issue", limit=1)
                self.assertEqual(page["quantity"], 7)
                self.assertEqual(
                    movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
                )
        # 与 --type receive 取交集后无匹配：空数组，数量照常返回。
        no_match = self.show(after_id="0001", mtype="receive")
        self.assertEqual(no_match["quantity"], 7)
        self.assertEqual(no_match["movements"], [])


class SharedRuleRejectionCLITest(InventoryCLITestCase):
    """两个入口的拒绝边界在命令行上一致：退出码 2、stdout 空、参数名不串。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_after_id_rejections_via_cli(self):
        for bad in (
            "", OVER_MAX_TEXT, LONG_NINES, "-1", "+1", "1.5", "1_0",
            "abc", " 1", "1\n", "１\n", LONG_ONE + "\n",
        ):
            with self.subTest(bad=bad[:12]):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assert_param_error(code, out, err, "--after-id")
                # 拒绝不改变数据。
                self.assertEqual(self.show()["quantity"], 7)

    def test_threshold_rejections_via_cli(self):
        for bad in (
            "", OVER_MAX_TEXT, LONG_NINES, "-1", "+5", "5.0", "5_0",
            "abc", " 5", "5\n", "７\n",
        ):
            with self.subTest(bad=bad[:12]):
                code, out, err = self.run_cli("low-stock", "--threshold", bad)
                self.assert_param_error(code, out, err, "--threshold")
                # 拒绝不改变数据：阈值 7 仍能查到 DEMO-1。
                code, out, err = self.low_stock("7")
                self.assertEqual(code, 0)
                self.assertEqual(
                    json.loads(out)["products"][0]["quantity"], 7
                )

    def test_range_error_mentions_limit_and_reason(self):
        # 越界提示含上限数值与“不能超过”，且参数名正确。
        code, out, err = self.run_cli(
            "show", "--sku", SKU, "--after-id", OVER_MAX_TEXT
        )
        self.assert_param_error(code, out, err, "--after-id")
        self.assertIn(MAX_TEXT, err)
        self.assertIn("不能超过", err)
        code, out, err = self.run_cli(
            "low-stock", "--threshold", LONG_NINES
        )
        self.assert_param_error(code, out, err, "--threshold")
        self.assertIn(MAX_TEXT, err)
        self.assertIn("不能超过", err)

    def test_whitespace_error_message_kept(self):
        code, out, err = self.run_cli(
            "show", "--sku", SKU, "--after-id", "1\n"
        )
        self.assert_param_error(code, out, err, "--after-id")
        self.assertIn("不能含空白", err)
        code, out, err = self.run_cli(
            "low-stock", "--threshold", "7\t"
        )
        self.assert_param_error(code, out, err, "--threshold")
        self.assertIn("不能含空白", err)

    def test_omitted_threshold_rejected(self):
        # low-stock 完全省略 --threshold：参数错误，stdout 为空。
        code, out, err = self.low_stock()
        self.assert_param_error(code, out, err, "--threshold")
        # --threshold 缺值同样拒绝。
        code, out, err = self.run_cli("low-stock", "--threshold")
        self.assert_param_error(code, out, err, "--threshold")

    def test_rejections_do_not_create_database_file(self):
        # 参数拒绝发生在打开数据库之前：文件尚不存在时两个入口都不得创建。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        invocations = (
            ("show", "--sku", SKU, "--after-id", LONG_NINES),
            ("show", "--sku", SKU, "--after-id", "1\n"),
            ("low-stock", "--threshold", OVER_MAX_TEXT),
            ("low-stock", "--threshold", ""),
        )
        for args in invocations:
            with self.subTest(args=args[0]):
                code, out, err = self.run_cli(*args, db_path=missing_db)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )

    def test_max_boundary_itself_accepted_at_both_entries(self):
        # 上限本身成功：show 返回空流水但商品信息完整；low-stock 命中商品。
        page = self.show(after_id=MAX_TEXT)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(page["movements"], [])
        code, out, err = self.low_stock(MAX_TEXT)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out)["products"],
            [{"sku": SKU, "name": NAME, "quantity": 7}],
        )


if __name__ == "__main__":
    unittest.main()
