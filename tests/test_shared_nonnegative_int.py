"""--after-id 与 --threshold 共用非负整数校验规则的回归测试。

两个入口在 inventory/cli.py 中曾各自重复实现同一条非负整数校验规则，
重构后共同规则只维护一处（_nonnegative_int），after_id 与 threshold
仅以参数名区分拒绝原因。本测试围绕这两个入口锁定兼容性：

- 直接调用校验入口：合法文本返回 int，非法文本抛 argparse.ArgumentTypeError；
  两个入口对同值文本给出同一数值、对同类非法文本给出仅参数名不同的原因；
- 命令行行为：退出码 2、stdout 为空、stderr 含本参数名与原有拒绝原因、
  不出现堆栈，且错误信息不串用另一个参数名；
- 数字文本：ASCII、全角、阿拉伯印度数字及其混写按同一数值处理，
  前导零不改变结果；5000 个零等价于 0，5000 个零后接 1 等价于 1；
  上限 9223372036854775807 成功，上限加一与 5000 个九拒绝；
- 空字符串、正负号、小数、下划线、非十进制数字字符、任意位置的空白
  （含数字后仅一个换行）一律拒绝；
- 非法值在访问数据库前拒绝：不创建尚不存在的数据库文件，
  不改变已有商品与流水；
- show 省略 --after-id 仍返回全部流水；low-stock 省略或缺值仍按
  参数错误拒绝；筛选与 --type、--limit 的组合语义不变；
- --qty 与 --limit 保留各自原有规则，未被共用规则影响。

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

from inventory.cli import after_id, threshold

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SKU = "DEMO-1"
NAME = "演示螺母"
MISSING_SKU = "NOT-EXIST"

MAX_TEXT = "9223372036854775807"
OVER_MAX_TEXT = "9223372036854775808"
MAX_VALUE = 9223372036854775807

# 远超 Python 3.11+ 默认整数字符串转换位数上限（4300）的前导零个数。
LEADING_ZEROS = "0" * 5000
LONG_ZERO = LEADING_ZEROS
LONG_ONE = LEADING_ZEROS + "1"
LONG_MAX = LEADING_ZEROS + MAX_TEXT
LONG_OVER_MAX = LEADING_ZEROS + OVER_MAX_TEXT
LONG_NINES = "9" * 5000

# 全角 U+FF10..FF19 与阿拉伯印度 U+0660..U+0669 的数字文本。
FW_ZERO, FW_ONE, FW_SEVEN = "０", "１", "７"
AR_ZERO, AR_ONE, AR_SEVEN = "٠", "١", "٧"

# 每个条目都是数值 1 的不同写法（ASCII、全角、阿拉伯印度、混写、前导零）。
ONE_FORMS = ("1", "0001", FW_ONE, AR_ONE, f"0{FW_ZERO}{AR_ZERO}{FW_ONE}", LONG_ONE)
SEVEN_FORMS = ("7", "0007", FW_SEVEN, AR_SEVEN, f"0{FW_ZERO}{AR_ZERO}{FW_SEVEN}")
ZERO_FORMS = ("0", "0000", FW_ZERO, AR_ZERO, f"0{FW_ZERO}{AR_ZERO}", LONG_ZERO)

INVALID_FORMS = (
    "",        # 空字符串
    "-1",      # 负号
    "+1",      # 正号
    "1.5",     # 小数
    "1_0",     # 下划线
    "abc",     # 非数字
    "0x1",     # 非十进制
    " 1", "1 ", "1 2",     # 空白：开头/结尾/中间
    "1\n", "\n1", "1\t", "1\r",  # 换行/制表/回车
    OVER_MAX_TEXT,          # 上限加一
    "9" * 30,               # 位数远超上限
    LONG_NINES,             # 5000 个九
    LONG_OVER_MAX,          # 前导零后接上限加一
)


class DirectValidatorTest(unittest.TestCase):
    """直接调用校验入口：返回 int，错误为 argparse.ArgumentTypeError。"""

    def test_valid_forms_return_python_int(self):
        for fn, forms, expected in (
            (after_id, ONE_FORMS, 1),
            (after_id, ZERO_FORMS, 0),
            (after_id, (MAX_TEXT, LONG_MAX), MAX_VALUE),
            (threshold, SEVEN_FORMS, 7),
            (threshold, ZERO_FORMS, 0),
            (threshold, (MAX_TEXT, LONG_MAX), MAX_VALUE),
        ):
            for text in forms:
                with self.subTest(fn=fn.__name__, text=text[:8]):
                    result = fn(text)
                    self.assertIs(type(result), int)
                    self.assertEqual(result, expected)

    def test_five_thousand_zeros_and_then_one(self):
        # 长文本边界单独锁定：五千个零等价于零，其后接一等价于一。
        self.assertEqual(after_id(LONG_ZERO), 0)
        self.assertEqual(after_id(LONG_ONE), 1)
        self.assertEqual(threshold(LONG_ZERO), 0)
        self.assertEqual(threshold(LONG_ONE), 1)

    def test_invalid_forms_raise_argument_type_error(self):
        for fn in (after_id, threshold):
            for text in INVALID_FORMS:
                with self.subTest(fn=fn.__name__, text=text[:8]):
                    with self.assertRaises(argparse.ArgumentTypeError):
                        fn(text)

    def test_mixed_scripts_share_value(self):
        # 三种文字混写按同一数值处理。
        mixed_one = f"{AR_ZERO}{FW_ZERO}0{AR_ONE}"
        self.assertEqual(after_id(mixed_one), after_id("1"))
        mixed_seven = f"{FW_ZERO}0{AR_ZERO}{AR_SEVEN}"
        self.assertEqual(threshold(mixed_seven), threshold("7"))

    def test_two_entry_points_share_identical_rule(self):
        # 同一规则只维护一处：两个入口对同值文本同结果、同类错误同原因，
        # 差异只能出现在参数名上。
        for text in ONE_FORMS + ZERO_FORMS + (MAX_TEXT, LONG_MAX):
            with self.subTest(text=text[:8]):
                self.assertEqual(after_id(text), threshold(text))
        for text in ("", "1\n", "x", OVER_MAX_TEXT, LONG_NINES):
            with self.subTest(text=text[:8]):
                with self.assertRaises(argparse.ArgumentTypeError) as cm_a:
                    after_id(text)
                with self.assertRaises(argparse.ArgumentTypeError) as cm_t:
                    threshold(text)
                msg_a = str(cm_a.exception).replace("--after-id", "<P>")
                msg_t = str(cm_t.exception).replace("--threshold", "<P>")
                self.assertEqual(msg_a, msg_t)


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
        self.assertEqual(err, "")
        payload = json.loads(out)  # stdout 必须是单个可解析 JSON 对象
        self.assertIsInstance(payload, dict)
        return payload

    def run_rejected(self, *args):
        """运行应以退出码 2 拒绝的命令，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args[:4]}... 应以退出码 2 拒绝")
        self.assertEqual(out, "", "被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), "被拒绝时 stderr 应说明原因")
        self.assertNotIn("Traceback", err, "参数拒绝不应输出异常堆栈")
        return err

    def seed_demo(self):
        """登记 DEMO-1，入库 10、出库 3；流水编号 1（收，余额 10）、2（发，余额 7）。"""
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

    def show(self, after_id=..., mtype=..., limit=...):
        args = ["show", "--sku", SKU]
        if after_id is not ...:
            args += ["--after-id", after_id]
        if mtype is not ...:
            args += ["--type", mtype]
        if limit is not ...:
            args += ["--limit", limit]
        return self.run_ok(*args)

    def low_stock(self, threshold_text):
        return self.run_ok("low-stock", "--threshold", threshold_text)["products"]


def movement_tuples(movements):
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestAcceptanceScenario(InventoryCLITestCase):
    """用户兼容验收：新台账 + 入十出三后，两种数字写法查询结果一致。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_after_id_equivalent_texts_return_only_issue_movement(self):
        expected = {"id": 2, "type": "issue", "quantity": 3, "balance": 7}
        pages = []
        for text in ("0001", FW_ONE, "1", AR_ONE):
            with self.subTest(after_id=text):
                page = self.show(after_id=text)
                self.assertEqual(page["sku"], SKU)
                self.assertEqual(page["name"], NAME)
                # 商品当前数量与流水余额均为七。
                self.assertEqual(page["quantity"], 7)
                self.assertEqual(len(page["movements"]), 1)
                self.assertEqual(page["movements"][0], expected)
                pages.append(page)
        self.assertTrue(all(p == pages[0] for p in pages))

    def test_threshold_equivalent_texts_include_product_at_seven(self):
        results = []
        for text in ("0007", FW_SEVEN, "7", AR_SEVEN):
            with self.subTest(threshold=text):
                products = self.low_stock(text)
                self.assertEqual(
                    products, [{"sku": SKU, "name": NAME, "quantity": 7}]
                )
                results.append(products)
        self.assertTrue(all(r == results[0] for r in results))
        # 阈值 6 不含该商品，确认边界语义未变。
        self.assertEqual(self.low_stock("6"), [])

    def test_repeated_queries_keep_result_and_database_content(self):
        # 重复查询结果一致，且数据库内容不变。
        for _ in range(3):
            self.assertEqual(
                movement_tuples(self.show(after_id="0001")["movements"]),
                [(2, "issue", 3, 7)],
            )
            self.assertEqual(
                self.low_stock("0007"),
                [{"sku": SKU, "name": NAME, "quantity": 7}],
            )
        full = self.show()
        self.assertEqual(full["quantity"], 7)
        self.assertEqual(
            movement_tuples(full["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )


class TestEquivalentDigitTexts(InventoryCLITestCase):
    """同值文本（含长文本）在两个入口上完全等价。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_after_id_all_one_forms_equivalent(self):
        baseline = movement_tuples(self.show(after_id="1")["movements"])
        self.assertEqual(baseline, [(2, "issue", 3, 7)])
        for text in ONE_FORMS:
            with self.subTest(after_id=text[:8]):
                page = self.show(after_id=text)
                self.assertEqual(movement_tuples(page["movements"]), baseline)

    def test_after_id_zero_forms_return_all_movements(self):
        baseline = movement_tuples(self.show()["movements"])
        for text in ZERO_FORMS:
            with self.subTest(after_id=text[:8]):
                page = self.show(after_id=text)
                self.assertEqual(movement_tuples(page["movements"]), baseline)
        # 省略 --after-id 同样返回全部匹配流水。
        self.assertEqual(movement_tuples(self.show()["movements"]), baseline)

    def test_threshold_all_seven_forms_equivalent(self):
        baseline = self.low_stock("7")
        for text in SEVEN_FORMS:
            with self.subTest(threshold=text[:8]):
                self.assertEqual(self.low_stock(text), baseline)

    def test_long_zero_forms(self):
        # 五千个零：after-id 等价 0（全部流水），threshold 等价 0（无零库存商品）。
        self.assertEqual(
            movement_tuples(self.show(after_id=LONG_ZERO)["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        self.assertEqual(self.low_stock(LONG_ZERO), [])
        # 五千个零后接一：after-id 等价 1，threshold 等价 1（不含数量 7 的商品）。
        self.assertEqual(
            movement_tuples(self.show(after_id=LONG_ONE)["movements"]),
            [(2, "issue", 3, 7)],
        )
        self.assertEqual(self.low_stock(LONG_ONE), [])

    def test_upper_bound_accepted_and_over_rejected(self):
        # 上限本身在两个入口都成功。
        page = self.show(after_id=MAX_TEXT)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(page["movements"], [])
        self.assertEqual(
            self.show(after_id=LONG_MAX)["movements"],
            self.show(after_id=MAX_TEXT)["movements"],
        )
        self.assertEqual(
            [p["sku"] for p in self.low_stock(MAX_TEXT)], [SKU]
        )
        # 上限加一与五千个九在两个入口都拒绝。
        for bad in (OVER_MAX_TEXT, LONG_OVER_MAX, LONG_NINES):
            with self.subTest(bad=bad[:8]):
                err_a = self.run_rejected("show", "--sku", SKU, "--after-id", bad)
                self.assertIn(MAX_TEXT, err_a)
                self.assertIn("不能超过", err_a)
                err_t = self.run_rejected("low-stock", "--threshold", bad)
                self.assertIn(MAX_TEXT, err_t)
                self.assertIn("不能超过", err_t)
        # 拒绝后数据不变。
        self.assertEqual(self.show()["quantity"], 7)
        self.assertEqual([p["sku"] for p in self.low_stock("7")], [SKU])


class TestErrorMessagesDoNotCrossParameterNames(InventoryCLITestCase):
    """每个入口的错误只出现自己的参数名与原有拒绝原因。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_after_id_errors_keep_after_id_name_and_reasons(self):
        cases = {
            "": "不能为空",
            "1\n": "不能含空白",
            "x": "必须是十进制非负整数",
            OVER_MAX_TEXT: "不能超过",
        }
        for bad, reason in cases.items():
            with self.subTest(after_id=bad[:8] or "<empty>"):
                err = self.run_rejected("show", "--sku", SKU, "--after-id", bad)
                self.assertIn("--after-id", err)
                self.assertIn(reason, err)
                # 不串用另一个参数名。
                self.assertNotIn("--threshold", err)

    def test_threshold_errors_keep_threshold_name_and_reasons(self):
        cases = {
            "": "不能为空",
            "7 ": "不能含空白",
            "x": "必须是十进制非负整数",
            OVER_MAX_TEXT: "不能超过",
        }
        for bad, reason in cases.items():
            with self.subTest(threshold=bad[:8] or "<empty>"):
                err = self.run_rejected("low-stock", "--threshold", bad)
                self.assertIn("--threshold", err)
                self.assertIn(reason, err)
                # 不串用另一个参数名。
                self.assertNotIn("--after-id", err)

    def test_trailing_newline_rejected_for_both(self):
        # 数字后仅附一个换行也不能接受，且原因是“不能含空白”。
        err_a = self.run_rejected("show", "--sku", SKU, "--after-id", "1\n")
        self.assertIn("不能含空白", err_a)
        self.assertNotIn("--threshold", err_a)
        err_t = self.run_rejected("low-stock", "--threshold", "7\n")
        self.assertIn("不能含空白", err_t)
        self.assertNotIn("--after-id", err_t)

    def test_invalid_forms_matrix(self):
        for bad in INVALID_FORMS:
            with self.subTest(text=bad[:8] or "<empty>"):
                err_a = self.run_rejected(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assertIn("--after-id", err_a)
                self.assertNotIn("--threshold", err_a)
                err_t = self.run_rejected("low-stock", "--threshold", bad)
                self.assertIn("--threshold", err_t)
                self.assertNotIn("--after-id", err_t)


class TestRejectionBeforeDatabaseAccess(InventoryCLITestCase):
    """非法值在访问数据库前拒绝：不创建文件，不改变已有数据。"""

    def test_invalid_values_do_not_create_database_file(self):
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("", "-1", "1\n", "+1", "1.5", "abc", "1_0", OVER_MAX_TEXT,
                    LONG_NINES):
            with self.subTest(text=bad[:8] or "<empty>"):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad, db_path=missing_db
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )
                code, out, err = self.run_cli(
                    "low-stock", "--threshold", bad, db_path=missing_db
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )

    def test_invalid_values_leave_data_unchanged(self):
        self.seed_demo()
        before_show = self.show()
        before_low = self.low_stock("7")
        for bad in ("x", "1\n", OVER_MAX_TEXT, LONG_NINES, ""):
            with self.subTest(text=bad[:8] or "<empty>"):
                self.run_cli("show", "--sku", SKU, "--after-id", bad)
                self.run_cli("low-stock", "--threshold", bad)
        self.assertEqual(self.show(), before_show)
        self.assertEqual(self.low_stock("7"), before_low)

    def test_bad_after_id_reported_before_missing_sku(self):
        # 即使 SKU 不存在，仍先报告 --after-id 参数错误，不访问数据库。
        missing_db = str(Path(self._tmp.name) / "pre-arg-check.db")
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--after-id", LONG_NINES,
            db_path=missing_db,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--after-id", err)
        self.assertNotIn("商品不存在", err)
        self.assertFalse(Path(missing_db).exists())


class TestOmittedAndCombinedSemantics(InventoryCLITestCase):
    """省略参数与组合筛选的既有语义保持不变。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_show_without_after_id_returns_all_movements(self):
        page = self.show()
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_after_id_combined_with_type_and_limit(self):
        # after-id 与 --type、--limit 组合：交集后按 id 升序截断。
        page = self.show(after_id="0001", mtype="issue", limit="1")
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
        )
        # 全角数字下界与 --type 组合：同值同义。
        page_fw = self.show(after_id=FW_ONE, mtype="issue")
        self.assertEqual(
            movement_tuples(page_fw["movements"]), [(2, "issue", 3, 7)]
        )
        # 交集为空：退出码 0、空数组。
        self.assertEqual(
            self.show(after_id="0001", mtype="receive")["movements"], []
        )

    def test_low_stock_threshold_required(self):
        # 完全省略 --threshold：退出码 2、stdout 为空、stderr 含参数名。
        code, out, err = self.run_cli("low-stock")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--threshold", err)
        self.assertNotIn("Traceback", err)
        # --threshold 缺值同样拒绝。
        code, out, err = self.run_cli("low-stock", "--threshold")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--threshold", err)


class TestOtherValidatorsKeepOwnRules(InventoryCLITestCase):
    """--qty 与 --limit 未被共用规则影响，保留各自原有规则。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_qty_zero_still_rejected(self):
        # --qty 仍要求正整数：零（含全角/长零文本）按数量无效拒绝，
        # 且错误信息属于 --qty，不借用共用入口的参数名。
        for bad in ("0", FW_ZERO, LONG_ZERO):
            with self.subTest(qty=bad[:8]):
                err = self.run_rejected("receive", "--sku", SKU, "--qty", bad)
                self.assertIn("--qty", err)
                self.assertNotIn("--after-id", err)
                self.assertNotIn("--threshold", err)
        self.assertEqual(self.show()["quantity"], 7)

    def test_limit_rejects_unicode_digits(self):
        # --limit 仍只接受 ASCII 数字：全角写法按 --limit 规则拒绝。
        err = self.run_rejected(
            "show", "--sku", SKU, "--limit", "１"
        )
        self.assertIn("--limit", err)
        self.assertNotIn("--after-id", err)
        self.assertNotIn("--threshold", err)
        # 合法 ASCII 前导零写法照常工作。
        page = self.show(limit="0001")
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 10, 10)]
        )


if __name__ == "__main__":
    unittest.main()
