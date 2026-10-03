"""超长 --qty 数量文本（数千个前导零）的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证在 Python 3.11+ 默认的整数字符串转换
位数限制（4300 位）下，由 ASCII 数字组成的超长 --qty 文本仍按实际
数值判断，而不是仅因文本长度被拒绝：

- 5000 个 0 后接 10 / 3：入库、出库均退出码 0，返回数量 10 / 7 的
  商品 JSON；show 返回余额 7 与入库 10 后余额 10、出库 3 后余额 7
  两条流水，数量与余额在 JSON 原文与解析结果中都是精确整数；
- 5000 个 0：仍是零值，按数量无效拒绝，退出码 2，stdout 为空，
  stderr 说明原因且无异常堆栈；
- 5000 个 9、任意前导零后接 9223372036854775808：按超过单次上限
  拒绝，stderr 包含 --qty 与允许上限；
- 前导零后数值恰好等于上限仍是合法数量，随后按现有余额规则判断
  入库溢出 / 出库超量；
- 超长非法数量与不存在的 SKU 同时出现时仍先报数量错误，且不创建
  尚不存在的数据库文件；已有商品名称、余额与完整流水保持不变。

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
OVER_MAX_TEXT = "9223372036854775808"

# 远超 Python 3.11+ 默认 4300 位 int 转换上限的前导零个数。
ZERO_PAD = "0" * 5000


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


class TestLongLeadingZeroQtyAccepted(InventoryCLITestCase):
    """主场景：5000 个前导零后接合法数量，按实际数值正常入出库。"""

    def test_receive_and_issue_with_long_leading_zeros(self):
        self.add_demo_product()

        # 入库：5000 个 0 后接 10，退出码 0，返回数量为 10 的商品 JSON。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", ZERO_PAD + "10"
        )
        self.assertEqual(code, 0, err)
        received = json.loads(out)
        self.assertEqual(received, {"sku": SKU, "name": NAME, "quantity": 10})
        self.assertIsInstance(received["quantity"], int)
        self.assert_exact_json_int(out, "quantity", 10)

        # 出库：5000 个 0 后接 3，退出码 0，返回数量为 7 的商品 JSON。
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", ZERO_PAD + "3"
        )
        self.assertEqual(code, 0, err)
        issued = json.loads(out)
        self.assertEqual(issued, {"sku": SKU, "name": NAME, "quantity": 7})
        self.assertIsInstance(issued["quantity"], int)
        self.assert_exact_json_int(out, "quantity", 7)

        # show：余额 7，仅有入库 10 后余额 10、出库 3 后余额 7 两条流水，
        # 数量与余额都是精确整数，不能保留为长字符串。
        page_out, page = self.show()
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(len(page["movements"]), 2)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        for m in page["movements"]:
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)
        self.assert_exact_json_int(page_out, "quantity", 10)
        self.assert_exact_json_int(page_out, "balance", 10)
        self.assert_exact_json_int(page_out, "quantity", 7)
        self.assert_exact_json_int(page_out, "balance", 7)
        # 长参数文本不得以字符串形态残留在输出里。
        self.assertNotIn(ZERO_PAD, page_out)

    def test_long_leading_zeros_before_max_qty_is_valid(self):
        """前导零后数值恰好等于上限：合法数量，按现有余额规则处理。"""
        self.add_demo_product()

        # 空余额入库 5000 个 0 后接上限：成功，余额恰好为上限。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", ZERO_PAD + MAX_QTY_TEXT
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(
            payload, {"sku": SKU, "name": NAME, "quantity": MAX_QTY}
        )
        self.assert_exact_json_int(out, "quantity", MAX_QTY)

        # 再入库 1：本次数量合法，但余额会超过上限 -> 既有业务错误。
        err = self.run_rejected("receive", "--sku", SKU, "--qty", "1")
        self.assertIn(MAX_QTY_TEXT, err)

        # 出库 5000 个 0 后接上限：成功归零。
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", ZERO_PAD + MAX_QTY_TEXT
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 0}
        )

        # 流水完整且数量、余额均为精确大整数。
        page_out, page = self.show()
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", MAX_QTY, MAX_QTY), ("issue", MAX_QTY, 0)],
        )
        self.assert_exact_json_int(page_out, "quantity", MAX_QTY)
        self.assert_exact_json_int(page_out, "balance", MAX_QTY)
        self.assertNotIn("9.2233", page_out)

    def test_long_leading_zeros_before_over_max_rejected(self):
        """任意前导零后接 9223372036854775808：按超过单次上限拒绝。"""
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        before_out, before = self.show()

        for qty in (ZERO_PAD + OVER_MAX_TEXT, "0" + OVER_MAX_TEXT):
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty_len=len(qty)):
                    err = self.run_rejected(cmd, "--sku", SKU, "--qty", qty)
                    self.assertIn("--qty", err)
                    self.assertIn(MAX_QTY_TEXT, err)
                    # 台账原样不变（名称、余额、流水均无变化）。
                    after_out, after = self.show()
                    self.assertEqual(after, before)
                    self.assertEqual(after_out, before_out)


class TestLongQtyRejected(InventoryCLITestCase):
    """超长文本的零值与超上限：退出码 2，stdout 为空，无异常堆栈。"""

    def test_all_zeros_long_text_is_invalid(self):
        """5000 个 0 仍是零值，按数量无效拒绝。"""
        self.add_demo_product()
        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                err = self.run_rejected(cmd, "--sku", SKU, "--qty", ZERO_PAD)
                self.assertIn("数量", err)
                # 零值不属于“超过单次上限”，错误中不应出现上限数值。
                self.assertNotIn(MAX_QTY_TEXT, err)
        _, page = self.show()
        self.assertEqual(page["quantity"], 0)
        self.assertEqual(page["movements"], [])

    def test_all_nines_long_text_is_over_limit(self):
        """5000 个 9：按超过单次上限拒绝，错误包含 --qty 与允许上限。"""
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        before_out, before = self.show()

        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                err = self.run_rejected(cmd, "--sku", SKU, "--qty", "9" * 5000)
                self.assertIn("--qty", err)
                self.assertIn(MAX_QTY_TEXT, err)
                after_out, after = self.show()
                self.assertEqual(after, before)
                self.assertEqual(after_out, before_out)

    def test_long_invalid_qty_with_missing_sku_rejected_first(self):
        """超长非法数量且 SKU 不存在：先报数量错误，不创建数据库文件。"""
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in (ZERO_PAD, "9" * 5000, ZERO_PAD + OVER_MAX_TEXT):
            with self.subTest(qty_len=len(bad)):
                proc = subprocess.run(
                    [
                        sys.executable, "-m", "inventory", "--db", missing_db,
                        "receive", "--sku", MISSING_SKU, "--qty", bad,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn("Traceback", proc.stderr)
                self.assertNotIn("商品不存在", proc.stderr)
                # 数量解析失败发生在建库之前，台账文件不应被创建。
                self.assertFalse(
                    Path(missing_db).exists(), "数量非法时不应创建台账文件"
                )


if __name__ == "__main__":
    unittest.main()
