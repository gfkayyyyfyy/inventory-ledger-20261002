"""单次数量与库存余额 64 位整数边界的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证：

- 单次出入库数量限定为 1 至 9223372036854775807，边界值合法，前导零不改变含义；
- 库存余额限定为 0 至 9223372036854775807：合法入库使余额恰好达到上限，
  再入库即使本次数量合法也按业务错误拒绝，退出码 2 且数据、流水原样不变；
- 超过单次上限的 --qty（含 9223372036854775808）入库/出库均退出码 2，
  stdout 为空，stderr 包含 --qty 与允许上限，无异常堆栈；
- 零、负数、小数、非数字仍按数量无效拒绝；数量非法且 SKU 不存在时
  数量错误优先（命令尚未访问台账）；
- show 的 quantity 与流水的 quantity、balance 在 JSON 原文与解析结果中
  都是精确整数（不出现小数、指数、字符串）；
- 重新打开同一台账，持久化结果与拒绝前一致。

从项目根目录执行：

    python -m unittest discover
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SKU = "DEMO-1"
NAME = "演示螺母"
MISSING_SKU = "NOT-EXIST"

MAX_QTY = 9223372036854775807
MAX_QTY_TEXT = "9223372036854775807"
OVER_MAX_QTY = MAX_QTY + 1
OVER_MAX_TEXT = "9223372036854775808"
NEAR_MAX = MAX_QTY - 1
NEAR_MAX_TEXT = "9223372036854775806"


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
        """运行应被参数/业务规则拒绝的命令，返回 (stderr, stdout)。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝，stderr: {err}")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), f"命令 {args} 被拒绝时 stderr 应说明原因")
        self.assertNotIn("Traceback", err, "业务拒绝不应输出异常堆栈")
        return err, out

    def add_demo_product(self):
        payload = self.run_ok("add", "--sku", SKU, "--name", NAME)
        self.assertEqual(payload, {"sku": SKU, "name": NAME, "quantity": 0})

    def show(self):
        code, out, err = self.run_cli("show", "--sku", SKU)
        self.assertEqual(code, 0, f"show 应成功，stderr: {err}")
        return out, json.loads(out)

    def assert_exact_json_int(self, text, key, value):
        """JSON 原文中 key 的值必须是精确十进制整数字面量。

        拒绝指数记法（9.22e18）、小数（.0）与字符串引号。
        """
        pattern = (
            r'"' + re.escape(key) + r'"\s*:\s*'
            + str(value)
            + r'(?=\s*[,\}])'
        )
        self.assertRegex(
            text, pattern, f"{key} 应为精确整数 {value} 的 JSON 字面量"
        )


class TestBalanceUpperBoundary(InventoryCLITestCase):
    """余额上界：入库 MAX-1 后再入库 1 恰好到顶，继续入库必须拒绝且不变。"""

    def test_receive_to_cap_then_overflow_rejected(self):
        self.add_demo_product()

        # 准备：入库 2^63-2。
        near = self.run_ok("receive", "--sku", SKU, "--qty", NEAR_MAX_TEXT)
        self.assertEqual(near, {"sku": SKU, "name": NAME, "quantity": NEAR_MAX})

        before_out, before = self.show()
        self.assertEqual(before["quantity"], NEAR_MAX)
        self.assertEqual(len(before["movements"]), 1)
        self.assertEqual(
            (
                before["movements"][0]["type"],
                before["movements"][0]["quantity"],
                before["movements"][0]["balance"],
            ),
            ("receive", NEAR_MAX, NEAR_MAX),
        )
        # 准备阶段的大整数在 JSON 原文中也必须是精确整数字面量。
        self.assert_exact_json_int(before_out, "quantity", NEAR_MAX)
        self.assert_exact_json_int(
            before_out, "balance", NEAR_MAX
        )

        # 本次数量合法（1），入库后余额恰好等于上限：成功。
        capped = self.run_ok("receive", "--sku", SKU, "--qty", "1")
        self.assertEqual(
            capped, {"sku": SKU, "name": NAME, "quantity": MAX_QTY}
        )

        capped_out, capped_page = self.show()
        self.assertEqual(capped_page["quantity"], MAX_QTY)
        # show 输出的大整数：解析为 int，且原文不退化。
        self.assertIsInstance(capped_page["quantity"], int)
        self.assert_exact_json_int(capped_out, "quantity", MAX_QTY)
        self.assertEqual(len(capped_page["movements"]), 2)
        last = capped_page["movements"][-1]
        self.assertEqual(
            (last["type"], last["quantity"], last["balance"]),
            ("receive", 1, MAX_QTY),
        )
        self.assert_exact_json_int(capped_out, "balance", MAX_QTY)
        for m in capped_page["movements"]:
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)
        # 上限整数绝不能以指数/小数/字符串形态出现在输出里。
        self.assertNotIn("9.2233", capped_out)
        self.assertNotIn(f'"{MAX_QTY_TEXT}"', capped_out)

        # 再入库 1：本次数量自身合法，但余额会超过上限 -> 退出码 2。
        err, out = self.run_rejected("receive", "--sku", SKU, "--qty", "1")
        # stderr 必须说明库存上限、当前余额和本次数量。
        self.assertIn(MAX_QTY_TEXT, err)
        self.assertIn(str(NEAR_MAX + 1), err)  # 当前余额 = 上限
        self.assertIn("1", err)  # 本次数量

        # 重新打开同一台账查询：完整结果与拒绝前一致，没有新增流水。
        after_out, after = self.show()
        self.assertEqual(after, capped_page)
        self.assertEqual(after_out, capped_out)

    def test_single_movement_added_on_successful_receive(self):
        """成功入库每次只新增一条流水；被拒绝时一条也不新增。"""
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", NEAR_MAX_TEXT)
        _, at_cap = self.show()
        self.run_ok("receive", "--sku", SKU, "--qty", "1")
        _, capped = self.show()
        self.assertEqual(len(capped["movements"]), len(at_cap["movements"]) + 1)

        # 多试几种会越界的本次数量，均不允许留下任何痕迹。
        for qty in ("1", "2", NEAR_MAX_TEXT, MAX_QTY_TEXT):
            with self.subTest(qty=qty):
                self.run_rejected("receive", "--sku", SKU, "--qty", qty)
                _, again = self.show()
                self.assertEqual(again, capped)


class TestSingleMoveQuantityBoundary(InventoryCLITestCase):
    """空余额台账：单次按上限入库成功、按上限出库成功归零；MAX+1 均拒绝。"""

    def test_receive_max_then_issue_max(self):
        self.add_demo_product()

        # 单次入库上限数量：成功，余额恰好为上限，JSON 为精确整数。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", MAX_QTY_TEXT
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": MAX_QTY}
        )
        self.assert_exact_json_int(out, "quantity", MAX_QTY)

        # 单次出库相同数量：成功归零。
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", MAX_QTY_TEXT
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 0}
        )

        # 重新打开同一台账：两条流水，数量与余额都是精确大整数。
        page_out, page = self.show()
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 0)
        self.assertEqual(
            [
                (m["type"], m["quantity"], m["balance"])
                for m in page["movements"]
            ],
            [("receive", MAX_QTY, MAX_QTY), ("issue", MAX_QTY, 0)],
        )
        self.assert_exact_json_int(page_out, "quantity", MAX_QTY)
        self.assert_exact_json_int(page_out, "balance", MAX_QTY)
        self.assertNotIn("9.2233", page_out)

    def test_over_max_qty_rejected_for_both_commands(self):
        self.add_demo_product()
        # 给一点余额，确认拒绝来自数量上界而非出库超量。
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        before_out, before = self.show()

        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                err, out = self.run_rejected(
                    cmd, "--sku", SKU, "--qty", OVER_MAX_TEXT
                )
                # stderr 必须包含 --qty 和允许的上限，且不是异常堆栈。
                self.assertIn("--qty", err)
                self.assertIn(MAX_QTY_TEXT, err)
                # 台账原样不变（名称、余额、流水均无变化）。
                after_out, after = self.show()
                self.assertEqual(after, before)
                self.assertEqual(after_out, before_out)

    def test_far_over_max_qty_rejected(self):
        self.add_demo_product()
        for qty in ("999999999999999999999999999999", "1" + "0" * 40):
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty=qty):
                    err, _ = self.run_rejected(
                        cmd, "--sku", SKU, "--qty", qty
                    )
                    self.assertIn("--qty", err)
                    self.assertIn(MAX_QTY_TEXT, err)
                _, page = self.show()
                self.assertEqual(page["quantity"], 0)
                self.assertEqual(page["movements"], [])


class TestInvalidQuantityStillRejected(InventoryCLITestCase):
    """零、负数、小数、非数字：退出码 2 且说明数量无效，数据不变。"""

    def test_invalid_quantities(self):
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        before_out, before = self.show()

        for bad in ("0", "-1", "-0", "1.5", "abc", "", " 1", "1 ", "0x1", "+1"):
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty=bad):
                    args = [cmd, "--sku", SKU, "--qty", bad]
                    code, out, err = self.run_cli(*args)
                    self.assertEqual(code, 2, f"{args} 应拒绝，err: {err}")
                    self.assertEqual(out, "")
                    self.assertIn("数量", err)
                    self.assertNotIn("Traceback", err)
                    after_out, after = self.show()
                    self.assertEqual(after, before)
                    self.assertEqual(after_out, before_out)

    def test_leading_zeros_preserve_value(self):
        self.add_demo_product()
        # 前导零不改变数量含义。
        payload = self.run_ok("receive", "--sku", SKU, "--qty", "00010")
        self.assertEqual(payload["quantity"], 10)
        payload = self.run_ok("issue", "--sku", SKU, "--qty", "0000000003")
        self.assertEqual(payload["quantity"], 7)
        # 前导零形式的上限同样合法且等于上限。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", "0" + MAX_QTY_TEXT
        )
        # 当前余额 7，加上上限必然越过库存上界 -> 业务错误而非参数错误，
        # 说明前导零上限被正确解析为上限值本身。
        self.assertEqual(code, 2)
        self.assertIn(MAX_QTY_TEXT, err)
        _, page = self.show()
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(len(page["movements"]), 2)

    def test_leading_zero_zero_is_invalid(self):
        self.add_demo_product()
        err, _ = self.run_rejected("receive", "--sku", SKU, "--qty", "000")
        self.assertIn("数量", err)
        # 零值不属于“超过单次上限”，错误中不应出现上限数值。
        self.assertNotIn(MAX_QTY_TEXT, err)


class TestQuantityErrorPrecedesMissingSku(InventoryCLITestCase):
    """数量非法且 SKU 不存在：先按数量错误拒绝，不访问台账、不建商品。"""

    def test_invalid_qty_with_missing_sku_rejected_first(self):
        # 台账文件尚未创建，非法数量也必须在打开数据库前被拒绝。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        proc = subprocess.run(
            [
                sys.executable, "-m", "inventory", "--db", missing_db,
                "receive", "--sku", MISSING_SKU, "--qty", OVER_MAX_TEXT,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--qty", proc.stderr)
        self.assertIn(MAX_QTY_TEXT, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        # 数量解析失败发生在建库之前，台账文件不应被创建。
        self.assertFalse(
            Path(missing_db).exists(), "数量非法时不应创建台账文件"
        )

    def test_zero_qty_with_missing_sku_rejected_first(self):
        self.add_demo_product()
        for bad in ("0", "-5", "1.0", "abc"):
            with self.subTest(qty=bad):
                code, out, err = self.run_cli(
                    "receive", "--sku", MISSING_SKU, "--qty", bad
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("数量", err)
                self.assertNotIn("商品不存在", err)

    def test_valid_qty_with_missing_sku_still_rejected(self):
        """数量合法但商品不存在：维持退出码 2 的既有语义。"""
        code, out, err = self.run_cli(
            "receive", "--sku", MISSING_SKU, "--qty", "1"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("商品不存在", err)
        code, out, err = self.run_cli(
            "issue", "--sku", MISSING_SKU, "--qty", OVER_MAX_TEXT
        )
        self.assertEqual(code, 2)
        self.assertIn("--qty", err)


class TestSmallQuantityBehaviorUnchanged(InventoryCLITestCase):
    """边界改造后，常规小数量的出入库与流水语义保持不变。"""

    def test_small_receive_and_issue(self):
        self.add_demo_product()
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "3")["quantity"], 7
        )
        # 出库超余额仍按既有业务错误拒绝。
        err, _ = self.run_rejected("issue", "--sku", SKU, "--qty", "8")
        self.assertIn("超过", err)
        _, page = self.show()
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )


if __name__ == "__main__":
    unittest.main()
