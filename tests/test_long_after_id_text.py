"""show --after-id 超长数字文本（数千个前导零）的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证：

- 由 ASCII 数字组成的 --after-id 文本按实际数值判断，不因文本长度被拒绝：
  5000 个前导零后接 1 与普通写法 1 完全等价，5000 个 0 本身与 0 等价，
  5000 个 0 后接 9223372036854775807（编号上界）也是合法下界；
- 5000 个 9、任意数量前导零后接 9223372036854775808 按超过编号上界
  以参数错误拒绝（退出码 2、stdout 为空、stderr 含 --after-id、
  允许的上限与越界原因、无异常堆栈）；
- 参数错误先于商品校验：即使 SKU 不存在也先报告 --after-id 错误，
  且数据库文件尚不存在时不得因该拒绝创建文件；
- 缺值、空字符串、负数、小数、正号、含字母的输入继续以退出码 2 拒绝；
- 长参数数值合法但 SKU 为空或不存在时，仍报告对应商品错误；
- 查询不改动商品与流水，重复查询结果一致；短参数既有形式行为不变。

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
OVER_MAX_ID_TEXT = "9223372036854775808"

# 远超 Python 3.11+ 默认整数字符串转换位数上限（4300）的前导零个数。
LEADING_ZEROS = "0" * 5000


class InventoryCLITestCase(unittest.TestCase):
    """每个用例使用独立的临时数据库，互不依赖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")

    def run_cli(self, *args):
        """运行一条 CLI 命令，返回 (退出码, stdout 文本, stderr 文本)。"""
        proc = subprocess.run(
            [sys.executable, "-m", "inventory", "--db", self.db_path, *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run_ok(self, *args):
        """运行应成功的命令，返回解析后的 JSON 对象。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"命令 {args} 应成功，stderr: {err}")
        payload = json.loads(out)  # stdout 必须是单个可解析的 JSON 对象
        self.assertIsInstance(payload, dict)
        return payload

    def run_rejected(self, *args):
        """运行应被参数/业务规则拒绝的命令，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝，stderr: {err}")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), f"命令 {args} 被拒绝时 stderr 应说明原因")
        self.assertNotIn("Traceback", err, "业务拒绝不应输出异常堆栈")
        return err

    def seed_demo_with_two_movements(self):
        """登记 DEMO-1，入库 10、出库 3：流水 1(receive,10,10)、2(issue,3,7)。"""
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

    def show(self, *extra):
        return self.run_ok("show", "--sku", SKU, *extra)


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestLongAfterIdAccepted(InventoryCLITestCase):
    """长 ASCII 数字文本按实际数值接受，与短写法等价。"""

    def test_long_zeros_then_one_with_type_and_limit(self):
        """主场景：5000 个 0 后接 1，配合 --type issue --limit 1 只留编号 2。"""
        self.seed_demo_with_two_movements()

        page = self.show(
            "--type", "issue",
            "--limit", "1",
            "--after-id", LEADING_ZEROS + "1",
        )
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        # 当前数量不受筛选影响：出库 3 后余额为 7。
        self.assertEqual(page["quantity"], 7)
        # id 严格大于 1 且类型为 issue，limit 1：仅剩编号 2 的原始流水。
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(2, "issue", 3, 7)],
        )

    def test_all_zeros_equivalent_to_zero(self):
        """5000 个 0 与 0 等价：从最早流水开始，返回全部两条原始流水。"""
        self.seed_demo_with_two_movements()

        via_long = self.show("--after-id", LEADING_ZEROS)
        via_zero = self.show("--after-id", "0")
        via_short = self.show("--after-id", "0000")
        self.assertEqual(via_long, via_zero)
        self.assertEqual(via_long, via_short)
        self.assertEqual(via_long["quantity"], 7)
        self.assertEqual(
            movement_tuples(via_long["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_long_zeros_then_max_id_returns_empty_movements(self):
        """前导零后恰为编号上界：合法下界，没有后续流水，商品信息照常返回。"""
        self.seed_demo_with_two_movements()

        page = self.show("--after-id", LEADING_ZEROS + MAX_ID_TEXT)
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(page["movements"], [])
        # 与直接使用上界的短写法结果一致。
        self.assertEqual(
            page, self.show("--after-id", MAX_ID_TEXT)
        )

    def test_long_zeros_then_one_equivalent_to_short_one(self):
        """长前导零写法与普通写法 1 在任何组合下结果完全相同。"""
        self.seed_demo_with_two_movements()

        for extra in (
            (),
            ("--type", "receive"),
            ("--type", "issue"),
            ("--limit", "1"),
            ("--type", "issue", "--limit", "1"),
        ):
            with self.subTest(extra=extra):
                long_page = self.show(*extra, "--after-id", LEADING_ZEROS + "1")
                short_page = self.show(*extra, "--after-id", "1")
                tiny_page = self.show(*extra, "--after-id", "0001")
                self.assertEqual(long_page, short_page)
                self.assertEqual(long_page, tiny_page)


class TestLongAfterIdRejected(InventoryCLITestCase):
    """越界长文本：确定的参数错误，退出码 2、stdout 空、无堆栈。"""

    def assert_after_id_param_error(self, err):
        """stderr 必须包含参数名、允许上限与越界原因。"""
        self.assertIn("--after-id", err)
        self.assertIn(MAX_ID_TEXT, err)
        self.assertIn("不能超过", err)

    def test_all_nines_exceeds_max(self):
        self.seed_demo_with_two_movements()
        before = self.show()

        err = self.run_rejected(
            "show", "--sku", SKU, "--after-id", "9" * 5000
        )
        self.assert_after_id_param_error(err)
        # 拒绝不改动商品或流水。
        self.assertEqual(self.show(), before)

    def test_long_zeros_then_over_max_exceeds_max(self):
        self.seed_demo_with_two_movements()
        before = self.show()

        err = self.run_rejected(
            "show", "--sku", SKU,
            "--after-id", LEADING_ZEROS + OVER_MAX_ID_TEXT,
        )
        self.assert_after_id_param_error(err)
        self.assertEqual(self.show(), before)

    def test_out_of_range_reported_before_missing_sku(self):
        """越界 --after-id 与不存在的 SKU 同时出现：先报告参数错误。"""
        self.seed_demo_with_two_movements()
        for bad in ("9" * 5000, LEADING_ZEROS + OVER_MAX_ID_TEXT):
            with self.subTest(value=bad[:8] + "..."):
                code, out, err = self.run_cli(
                    "show", "--sku", MISSING_SKU, "--after-id", bad
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assert_after_id_param_error(err)
                self.assertNotIn("商品不存在", err)
                self.assertNotIn("Traceback", err)

    def test_out_of_range_does_not_create_missing_database(self):
        """数据库文件尚不存在时，参数拒绝不得创建该文件。"""
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("9" * 5000, LEADING_ZEROS + OVER_MAX_ID_TEXT):
            with self.subTest(value=bad[:8] + "..."):
                proc = subprocess.run(
                    [
                        sys.executable, "-m", "inventory", "--db", missing_db,
                        "show", "--sku", MISSING_SKU, "--after-id", bad,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )


class TestInvalidLongAfterIdForms(InventoryCLITestCase):
    """缺值与非数字文本继续以退出码 2 拒绝，stderr 说明参数名与原因。"""

    def test_missing_value(self):
        self.seed_demo_with_two_movements()
        code, out, err = self.run_cli("show", "--sku", SKU, "--after-id")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--after-id", err)
        self.assertNotIn("Traceback", err)

    def test_invalid_values(self):
        self.seed_demo_with_two_movements()
        before = self.show()
        for bad in (
            "",            # 空字符串
            "-1",          # 负数
            "1.5",         # 小数
            "+1",          # 正号
            "abc",         # 含字母
            "0x1",         # 非十进制
            " 1",          # 含空白
            "1 ",          # 尾部空白
            LEADING_ZEROS + "x",   # 长前导零后接字母
            LEADING_ZEROS + "1.0",  # 长前导零后接小数
        ):
            with self.subTest(after_id=bad[:10]):
                err = self.run_rejected(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assertIn("--after-id", err)
                # 每次拒绝后数据不变。
                self.assertEqual(self.show(), before)


class TestValidLongAfterIdWithBadSku(InventoryCLITestCase):
    """长下界合法时，SKU 错误仍按既有商品错误拒绝（退出码 2、stdout 空）。"""

    def test_valid_long_bound_with_nonexistent_sku(self):
        self.seed_demo_with_two_movements()
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU,
            "--after-id", LEADING_ZEROS + "1",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("商品不存在", err)
        self.assertNotIn("Traceback", err)
        # 已有商品与流水不受影响。
        page = self.show()
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_valid_long_bound_with_empty_sku(self):
        self.seed_demo_with_two_movements()
        code, out, err = self.run_cli(
            "show", "--sku", "   ",
            "--after-id", LEADING_ZEROS + "1",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("SKU", err)
        self.assertNotIn("Traceback", err)


class TestLongAfterIdQueriesDoNotMutate(InventoryCLITestCase):
    """成功查询与参数拒绝均不改动数据，重复查询结果一致。"""

    def test_repeated_queries_identical_and_data_unchanged(self):
        self.seed_demo_with_two_movements()
        baseline = self.show()

        accepted = [
            ("--after-id", LEADING_ZEROS + "1"),
            ("--after-id", LEADING_ZEROS),
            ("--after-id", LEADING_ZEROS + MAX_ID_TEXT),
            ("--type", "issue", "--limit", "1",
             "--after-id", LEADING_ZEROS + "1"),
        ]
        for args in accepted:
            first = self.show(*args)
            self.assertEqual(first, self.show(*args))

        for bad in ("9" * 5000, LEADING_ZEROS + OVER_MAX_ID_TEXT):
            self.run_rejected("show", "--sku", SKU, "--after-id", bad)

        self.assertEqual(self.show(), baseline)
        self.assertEqual(
            movement_tuples(baseline["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        self.assertEqual(baseline["quantity"], 7)


class TestShortAfterIdFormsUnchanged(InventoryCLITestCase):
    """短参数既有数字形式保持兼容：普通整数、少量前导零、既有拒绝均不变。"""

    def test_short_forms_still_accepted(self):
        self.seed_demo_with_two_movements()
        self.assertEqual(
            movement_tuples(self.show("--after-id", "1")["movements"]),
            [(2, "issue", 3, 7)],
        )
        self.assertEqual(
            movement_tuples(self.show("--after-id", "0001")["movements"]),
            [(2, "issue", 3, 7)],
        )
        self.assertEqual(
            self.show("--after-id", "1", "--type", "issue", "--limit", "1"),
            self.show("--after-id", "0001", "--type", "issue", "--limit", "01"),
        )
        self.assertEqual(
            self.show("--after-id", MAX_ID_TEXT)["movements"], []
        )

    def test_short_invalid_forms_still_rejected(self):
        self.seed_demo_with_two_movements()
        for bad in ("", "-1", "1.5", "abc", "+1", OVER_MAX_ID_TEXT):
            with self.subTest(after_id=bad):
                err = self.run_rejected(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assertIn("--after-id", err)


if __name__ == "__main__":
    unittest.main()
