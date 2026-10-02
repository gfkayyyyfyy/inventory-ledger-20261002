"""单次数量上限与库存余额上限（64 位整数边界）的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，覆盖：

- 入库 2^63-2 后再入库 1，余额恰好达到 9223372036854775807；再入库被拒绝，
  重新打开同一台账查询，名称、余额、流水与拒绝前完全一致，不新增流水；
- 空余额台账单次入库上限数量成功、单次出库上限数量成功归零；
- 9223372036854775808（2^63）入库与出库均以退出码 2 拒绝；
- 零、负数、小数、非数字、前导零写法的拒绝/接受规则；
- 数量非法优先于商品不存在；
- 成功输出与流水的 quantity/balance 是精确 JSON 整数，
  在 SQLite 中存储类型也必须是 integer（而非退化为 real）。

从项目根目录执行：

    python -m unittest discover
"""

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SKU = "DEMO-1"
NAME = "演示螺母"
OTHER_SKU = "DEMO-EMPTY"

MAX_QTY = 9223372036854775807
OVER_MAX_QTY = 9223372036854775808
BELOW_MAX_QTY = 9223372036854775806


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
        return json.loads(out)

    def run_rejected(self, *args):
        """运行应被参数/业务规则拒绝的命令，断言退出码 2、stdout 为空、
        stderr 非空且不含异常堆栈，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝，stderr: {err}")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), f"命令 {args} 被拒绝时 stderr 应说明原因")
        self.assertNotIn("Traceback", err, "拒绝时不应输出异常堆栈")
        return err

    def add_demo_product(self, sku=SKU, name=NAME):
        payload = self.run_ok("add", "--sku", sku, "--name", name)
        self.assertEqual(payload, {"sku": sku, "name": name, "quantity": 0})

    def show(self, sku=SKU):
        return self.run_ok("show", "--sku", sku)

    def assert_exact_integer_json(self, raw_text, expected_values=()):
        """stdout 原文中的数量/余额必须写作精确十进制整数。

        不允许小数点（浮点）、科学计数法（近似值）或字符串包裹。
        expected_values 中的整数必须能在原文中以逐位数字形式找到。
        """
        for field in ("quantity", "balance"):
            # 右侧只能是整数字面量：不得是字符串、小数或科学计数法。
            self.assertNotRegex(
                raw_text,
                rf'"{field}"\s*:\s*"',
                f"{field} 不应是字符串",
            )
            self.assertNotRegex(
                raw_text,
                rf'"{field}"\s*:\s*-?\d+\.\d',
                f"{field} 不应是小数",
            )
            self.assertNotRegex(
                raw_text,
                rf'"{field}"\s*:\s*-?\d+(\.\d+)?[eE]',
                f"{field} 不应使用科学计数法",
            )
        for value in expected_values:
            self.assertIn(
                f": {value}",
                raw_text,
                f"输出中应逐位包含精确整数 {value}，实际输出: {raw_text}",
            )

    def raw_show(self, sku=SKU):
        """返回 show 命令的 (解析 JSON, stdout 原文)。"""
        code, out, err = self.run_cli("show", "--sku", sku)
        self.assertEqual(code, 0, f"show 应成功，stderr: {err}")
        return json.loads(out), out

    def assert_sqlite_integers(self, sku=SKU, expected_quantity=None):
        """直接打开数据库文件，断言余额与流水数量/余额的存储类型均为 integer。"""
        conn = sqlite3.connect(self.db_path)
        try:
            qrow = conn.execute(
                "SELECT quantity, typeof(quantity) FROM products WHERE sku = ?",
                (sku,),
            ).fetchone()
            self.assertIsNotNone(qrow)
            self.assertEqual(qrow[1], "integer", "余额存储类型必须是 integer")
            if expected_quantity is not None:
                self.assertEqual(qrow[0], expected_quantity)
            for q, t, b, tb in conn.execute(
                "SELECT quantity, typeof(quantity), balance, typeof(balance) "
                "FROM movements WHERE sku = ? ORDER BY id",
                (sku,),
            ):
                self.assertEqual(t, "integer", "流水数量存储类型必须是 integer")
                self.assertEqual(tb, "integer", "流水余额存储类型必须是 integer")
                self.assertIsInstance(q, int)
                self.assertIsInstance(b, int)
        finally:
            conn.close()


class TestStockBalanceCap(InventoryCLITestCase):
    """主场景：余额逼近并达到上限，超上限入库被拒绝且持久化结果不变。"""

    def test_reach_cap_then_over_cap_receive_rejected(self):
        self.add_demo_product()

        # 准备：入库 2^63-2。
        stocked = self.run_ok(
            "receive", "--sku", SKU, "--qty", str(BELOW_MAX_QTY)
        )
        self.assertEqual(stocked["quantity"], BELOW_MAX_QTY)

        # 再入库 1 成功，余额恰好达到上限（边界值合法）。
        at_cap = self.run_ok("receive", "--sku", SKU, "--qty", "1")
        self.assertEqual(
            at_cap,
            {"sku": SKU, "name": NAME, "quantity": MAX_QTY},
        )

        page, raw = self.raw_show()
        self.assertEqual(page["quantity"], MAX_QTY)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [
                ("receive", BELOW_MAX_QTY, BELOW_MAX_QTY),
                ("receive", 1, MAX_QTY),
            ],
        )
        # 每个数量与余额在 JSON 原文中都是逐位精确整数。
        self.assert_exact_integer_json(raw, (BELOW_MAX_QTY, MAX_QTY))
        for m in page["movements"]:
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)
        self.assert_sqlite_integers(expected_quantity=MAX_QTY)

        # 再入库 1：本次数量合法，但余额会超过上限，以退出码 2 拒绝。
        err = self.run_rejected("receive", "--sku", SKU, "--qty", "1")
        self.assertIn("库存上限", err)
        self.assertIn(str(MAX_QTY), err)
        self.assertIn("当前余额", err)
        self.assertIn(str(MAX_QTY), err)
        self.assertIn("本次数量", err)
        self.assertIn("1", err)

        # 拒绝后（子进程结束即相当于重新打开同一台账）完整查询结果不变，
        # 且只有此前两条流水，没有新增。
        after, raw_after = self.raw_show()
        self.assertEqual(after, page)
        self.assertEqual(raw_after, raw)
        self.assertEqual(len(after["movements"]), 2)
        self.assert_sqlite_integers(expected_quantity=MAX_QTY)

        # 商品名称也保持不变。
        self.assertEqual(after["name"], NAME)

    def test_valid_qty_but_balance_overflow_rejected_with_no_movement(self):
        # 余额为 5 时入库上限数量同样超过库存上限，即使数量自身合法也拒绝。
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "5")
        before = self.show()

        err = self.run_rejected(
            "receive", "--sku", SKU, "--qty", str(MAX_QTY)
        )
        self.assertIn("库存上限", err)
        self.assertIn(str(MAX_QTY), err)
        self.assertIn("当前余额", err)
        self.assertIn("5", err)
        self.assertIn("本次数量", err)

        # 名称、余额、流水（仅一条入库 5）全部保持原样。
        after = self.show()
        self.assertEqual(after, before)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in after["movements"]],
            [("receive", 5, 5)],
        )

        # 被拒后业务仍可继续：小额入库成功，只新增一条流水。
        more = self.run_ok("receive", "--sku", SKU, "--qty", "2")
        self.assertEqual(more["quantity"], 7)
        self.assertEqual(len(self.show()["movements"]), 2)


class TestSingleMoveQuantityLimits(InventoryCLITestCase):
    """空余额台账：单次入库/出库上限数量成功，超过上限拒绝。"""

    def test_receive_and_issue_at_max(self):
        self.add_demo_product(sku=OTHER_SKU, name=NAME)

        # 单次入库上限数量成功。
        received = self.run_ok(
            "receive", "--sku", OTHER_SKU, "--qty", str(MAX_QTY)
        )
        self.assertEqual(
            received,
            {"sku": OTHER_SKU, "name": NAME, "quantity": MAX_QTY},
        )
        page, raw = self.raw_show(OTHER_SKU)
        self.assertEqual(page["quantity"], MAX_QTY)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", MAX_QTY, MAX_QTY)],
        )
        self.assert_exact_integer_json(raw, (MAX_QTY,))
        self.assert_sqlite_integers(OTHER_SKU, expected_quantity=MAX_QTY)

        # 单次出库相同数量成功，余额归零，只新增一条出库流水。
        issued = self.run_ok(
            "issue", "--sku", OTHER_SKU, "--qty", str(MAX_QTY)
        )
        self.assertEqual(
            issued,
            {"sku": OTHER_SKU, "name": NAME, "quantity": 0},
        )
        drained, raw_drained = self.raw_show(OTHER_SKU)
        self.assertEqual(drained["quantity"], 0)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in drained["movements"]],
            [
                ("receive", MAX_QTY, MAX_QTY),
                ("issue", MAX_QTY, 0),
            ],
        )
        self.assert_exact_integer_json(raw_drained, (MAX_QTY,))
        self.assert_sqlite_integers(OTHER_SKU, expected_quantity=0)

    def test_over_max_qty_rejected_for_receive_and_issue(self):
        self.add_demo_product(sku=OTHER_SKU, name=NAME)
        self.run_ok("receive", "--sku", OTHER_SKU, "--qty", "10")
        before = self.show(OTHER_SKU)

        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                err = self.run_rejected(
                    cmd, "--sku", OTHER_SKU, "--qty", str(OVER_MAX_QTY)
                )
                # stderr 必须包含参数名 --qty 与允许的上限。
                self.assertIn("--qty", err)
                self.assertIn(str(MAX_QTY), err)
                # 数据不变、不新增流水。
                self.assertEqual(self.show(OTHER_SKU), before)

    def test_far_over_max_qty_rejected(self):
        self.add_demo_product(sku=OTHER_SKU, name=NAME)
        for bad in ("999999999999999999999999999999", "18446744073709551616"):
            with self.subTest(qty=bad):
                err = self.run_rejected(
                    "receive", "--sku", OTHER_SKU, "--qty", bad
                )
                self.assertIn("--qty", err)
                self.assertIn(str(MAX_QTY), err)
                self.assertEqual(
                    self.show(OTHER_SKU)["movements"], []
                )


class TestInvalidQuantityForms(InventoryCLITestCase):
    """零、负数、小数、非数字：receive 与 issue 都拒绝且数据不变。"""

    def test_invalid_forms_both_commands(self):
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        before = self.show()

        for bad in ("0", "-1", "-0", "1.5", "abc", "1e3", "0x1", "+1", " 1", ""):
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty=bad):
                    err = self.run_rejected(
                        cmd, "--sku", SKU, "--qty", bad
                    )
                    self.assertIn("--qty", err)
                    self.assertIn("数量", err)
                    self.assertEqual(self.show(), before)

    def test_leading_zeros_do_not_change_value(self):
        self.add_demo_product(sku=OTHER_SKU, name=NAME)

        # 前导零的 1 与普通 1 等价。
        payload = self.run_ok(
            "receive", "--sku", OTHER_SKU, "--qty", "0000000001"
        )
        self.assertEqual(payload["quantity"], 1)

        # 前导零写法的上限数量同样合法。
        self.run_ok("issue", "--sku", OTHER_SKU, "--qty", "1")
        payload = self.run_ok(
            "receive",
            "--sku",
            OTHER_SKU,
            "--qty",
            "000" + str(MAX_QTY),
        )
        self.assertEqual(payload["quantity"], MAX_QTY)

        # 前导零不改变数量含义：超过上限的写法仍拒绝。
        err = self.run_rejected(
            "receive",
            "--sku",
            OTHER_SKU,
            "--qty",
            "000" + str(OVER_MAX_QTY),
        )
        self.assertIn("--qty", err)
        self.assertIn(str(MAX_QTY), err)
        # 全零写法视为零，按无效数量拒绝。
        err = self.run_rejected("issue", "--sku", OTHER_SKU, "--qty", "0000")
        self.assertIn("--qty", err)
        self.assertIn("数量", err)

        # 拒绝未改变余额；此前三次成功操作的流水逐条保留，未因拒绝新增。
        page = self.show(OTHER_SKU)
        self.assertEqual(page["quantity"], MAX_QTY)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [
                ("receive", 1, 1),
                ("issue", 1, 0),
                ("receive", MAX_QTY, MAX_QTY),
            ],
        )


class TestQuantityErrorPrecedence(InventoryCLITestCase):
    """数量非法且 SKU 不存在时，先按数量错误拒绝。"""

    def test_bad_qty_reported_before_missing_product(self):
        # 不登记任何商品。
        for bad in ("0", "-5", "1.0", "abc", str(OVER_MAX_QTY)):
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty=bad):
                    err = self.run_rejected(
                        cmd, "--sku", "NOT-EXIST", "--qty", bad
                    )
                    self.assertIn("--qty", err)
                    self.assertNotIn("商品不存在", err)

        # 数量合法时才报告商品不存在。
        err = self.run_rejected("receive", "--sku", "NOT-EXIST", "--qty", "1")
        self.assertIn("商品不存在", err)
        err = self.run_rejected("issue", "--sku", "NOT-EXIST", "--qty", "1")
        self.assertIn("商品不存在", err)

    def test_balance_cap_check_requires_existing_product(self):
        # 商品不存在时，即使数量大到会“溢出”，也报商品不存在而非库存上限。
        err = self.run_rejected(
            "receive", "--sku", "NOT-EXIST", "--qty", str(MAX_QTY)
        )
        self.assertIn("商品不存在", err)
        self.assertNotIn("库存上限", err)


class TestSmallQuantitiesUnchanged(InventoryCLITestCase):
    """边界改动后，既有小数量正常操作语义保持不变。"""

    def test_small_receive_and_issue_still_works(self):
        self.add_demo_product()
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "3")["quantity"], 7
        )
        page = self.show()
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        # 出库超过余额仍按既有业务错误拒绝。
        err = self.run_rejected("issue", "--sku", SKU, "--qty", "8")
        self.assertIn("超过", err)
        self.assertEqual(self.show(), page)


if __name__ == "__main__":
    unittest.main()
