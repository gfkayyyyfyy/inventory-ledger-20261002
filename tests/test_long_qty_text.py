"""超长数量文本（数千个前导零）的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证：

- 由 ASCII 数字组成的 --qty 文本按实际数值判断，不因文本长度被拒绝：
  5000 个前导零后接 10 / 3 与普通写法完全等价，退出码 0，
  返回的商品 JSON 中数量是精确整数（不是长字符串）；
- 5000 个 0 仍是零值，按数量无效拒绝（退出码 2、stdout 为空、
  stderr 说明原因、无异常堆栈）；
- 5000 个 9、任意数量前导零后接 9223372036854775808 按超过单次上限
  拒绝，stderr 包含 --qty 与允许上限；
- 前导零后数值恰好等于上限仍为合法数量，随后按既有余额规则判断
  入库溢出 / 出库超量；
- 非法长文本数量与不存在的 SKU 同时出现时先报告数量错误，
  且不创建尚不存在的数据库文件；
- 短参数既有数字形式（普通整数、少量前导零）行为不变。

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


class TestLongLeadingZeroQuantity(InventoryCLITestCase):
    """主场景：5000 个前导零后接合法数量，按实际数值正常入出库。"""

    def test_long_leading_zeros_receive_and_issue(self):
        self.add_demo_product()

        # 5000 个 0 后接 10：与 --qty 10 完全等价。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", LEADING_ZEROS + "10"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 10}
        )
        # 数量是精确整数 10，不是 5000 余位数字组成的长文本。
        self.assert_exact_json_int(out, "quantity", 10)
        self.assertNotIn(LEADING_ZEROS, out)

        # 5000 个 0 后接 3：与 --qty 3 完全等价。
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", LEADING_ZEROS + "3"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 7}
        )
        self.assert_exact_json_int(out, "quantity", 7)

        # show：余额 7，仅有入库 10 后余额 10、出库 3 后余额 7 两条流水，
        # 数量与余额均为精确整数。
        page_out, page = self.show()
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        for m in page["movements"]:
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)
        self.assertNotIn(LEADING_ZEROS, page_out)

    def test_long_leading_zeros_before_max_qty_is_valid(self):
        """前导零后数值恰好等于上限：合法数量，按既有余额规则处理。"""
        self.add_demo_product()

        # 空余额入库上限：成功，余额恰好到达上限。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", LEADING_ZEROS + MAX_QTY_TEXT
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": MAX_QTY}
        )
        self.assert_exact_json_int(out, "quantity", MAX_QTY)

        # 余额已为上限，再按上限入库：数量本身合法，按入库溢出业务规则拒绝。
        err, _ = self.run_rejected(
            "receive", "--sku", SKU, "--qty", LEADING_ZEROS + MAX_QTY_TEXT
        )
        self.assertIn(MAX_QTY_TEXT, err)

        # 按上限出库：成功归零，证明长文本被精确解析为上限值本身。
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", LEADING_ZEROS + MAX_QTY_TEXT
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 0}
        )

        # 流水只有入库上限、出库上限两条，数量均为精确整数。
        _, page = self.show()
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", MAX_QTY, MAX_QTY), ("issue", MAX_QTY, 0)],
        )


class TestLongQuantityRejection(InventoryCLITestCase):
    """超长文本的拒绝路径：零值按数量无效、超上限按 --qty 上限拒绝。"""

    def test_all_zeros_is_invalid_quantity(self):
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        _, before = self.show()

        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                err, out = self.run_rejected(cmd, "--sku", SKU, "--qty", LEADING_ZEROS)
                # 5000 个 0 仍是零值：按数量无效拒绝，而非超过上限。
                self.assertIn("数量", err)
                self.assertNotIn(MAX_QTY_TEXT, err)
                _, after = self.show()
                self.assertEqual(after, before)

    def test_all_nines_exceeds_max(self):
        self.add_demo_product()
        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                err, out = self.run_rejected(
                    cmd, "--sku", SKU, "--qty", "9" * 5000
                )
                self.assertIn("--qty", err)
                self.assertIn(MAX_QTY_TEXT, err)
        _, page = self.show()
        self.assertEqual(page["quantity"], 0)
        self.assertEqual(page["movements"], [])

    def test_leading_zeros_before_over_max_exceeds_max(self):
        self.add_demo_product()
        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                err, out = self.run_rejected(
                    cmd, "--sku", SKU, "--qty", LEADING_ZEROS + OVER_MAX_TEXT
                )
                self.assertIn("--qty", err)
                self.assertIn(MAX_QTY_TEXT, err)
        _, page = self.show()
        self.assertEqual(page["quantity"], 0)
        self.assertEqual(page["movements"], [])

    def test_invalid_long_qty_with_missing_sku_rejected_first(self):
        """数量非法且 SKU 不存在：先报数量错误，不创建数据库文件。"""
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in (LEADING_ZEROS, "9" * 5000, LEADING_ZEROS + OVER_MAX_TEXT):
            with self.subTest(qty=bad[:8] + "..."):
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
                self.assertNotIn("商品不存在", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(
                    Path(missing_db).exists(), "数量非法时不应创建台账文件"
                )

    def test_valid_long_qty_with_missing_sku_reports_missing_product(self):
        """长文本数量合法但商品不存在：维持商品不存在的既有语义。"""
        code, out, err = self.run_cli(
            "receive", "--sku", MISSING_SKU, "--qty", LEADING_ZEROS + "5"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("商品不存在", err)


class TestShortQuantityFormsUnchanged(InventoryCLITestCase):
    """短参数既有数字形式保持兼容：普通整数与少量前导零行为不变。"""

    def test_short_forms_still_accepted(self):
        self.add_demo_product()
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "0005")["quantity"], 15
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "8")["quantity"], 7
        )
        _, page = self.show()
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 10, 10), ("receive", 5, 15), ("issue", 8, 7)],
        )

    def test_short_invalid_forms_still_rejected(self):
        self.add_demo_product()
        for bad in ("0", "-1", "1.5", "abc", "", "+1"):
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty=bad):
                    err, out = self.run_rejected(cmd, "--sku", SKU, "--qty", bad)
                    self.assertIn("数量", err)


if __name__ == "__main__":
    unittest.main()
