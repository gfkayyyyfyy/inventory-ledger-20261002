"""Unicode 十进制数字零值（全角 ０、阿拉伯印度 ٠ 等）的回归测试。

背景：--qty 的整数正则用 \\d 匹配，会放行全角数字（U+FF10 起）与
阿拉伯印度数字（U+0660 起）等 Unicode 十进制数字；修复前零值判定只
剥离 ASCII 零，"０"、"٠"、"0０٠" 等文本经 int() 得到 0 后绕过参数校验，
最终在存储层抛出未捕获的 ValueError，出现异常堆栈而非参数错误。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证：

- receive / issue 两个命令对纯十进制数字文本的零值统一拒绝：
  "0"、"000"、"０"、"٠"、"0０٠" 及长零文本（数千个 Unicode 零字符、
  不同体系零字符混写）都返回退出码 2，stdout 为空，stderr 包含 --qty
  并说明数量必须大于零，不出现异常堆栈；
- 零数量错误先于访问数据库与商品检查：与未登记 SKU、去空白后为空的
  SKU 同时出现时仍先报告数量错误，尚不存在的数据库文件不会被创建；
- 拒绝后台账原样不变：商品名称、库存余额与每条流水的编号、类型、
  数量、余额保持原样，不新增流水；
- 合法的非 ASCII 正整数数量继续按既有十进制数值精确处理：
  全角 "２"、阿拉伯印度 "٣" 可正常出入库，返回数量与流水数量为精确整数，
  前导零（含非 ASCII 零字符）不改变数值，单次数量上限继续适用。

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

MAX_QTY = 9223372036854775807
MAX_QTY_TEXT = "9223372036854775807"

# 各种体系的零字符：ASCII 0、全角 ０（U+FF10）、阿拉伯印度 ٠（U+0660）。
ASCII_ZERO = "0"
FULLWIDTH_ZERO = "０"
ARABIC_INDIC_ZERO = "٠"

# 远超 Python 3.11+ 默认整数字符串转换位数上限（4300）的文本长度。
LONG = 5000


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

    def assert_zero_qty_rejected(self, *args):
        """零数量必须按参数错误拒绝：退出码 2、stdout 空、stderr 含 --qty。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝，stderr: {err}")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertIn("--qty", err, f"错误信息应包含 --qty，stderr: {err}")
        self.assertIn("大于零", err, f"错误信息应说明数量必须大于零，stderr: {err}")
        self.assertNotIn(MAX_QTY_TEXT, err, "零值不是超过上限，不应出现上限数值")
        self.assertNotIn("Traceback", err, "参数拒绝不应输出异常堆栈")
        return err

    def add_demo_product(self):
        payload = self.run_ok("add", "--sku", SKU, "--name", NAME)
        self.assertEqual(payload, {"sku": SKU, "name": NAME, "quantity": 0})

    def show(self):
        code, out, err = self.run_cli("show", "--sku", SKU)
        self.assertEqual(code, 0, f"show 应成功，stderr: {err}")
        return out, json.loads(out)

    def seed_demo_ledger(self):
        """登记 DEMO-1 并入库 10、出库 3，返回 show 的 (原文, 解析结果)。"""
        self.add_demo_product()
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "3")["quantity"], 7
        )
        return self.show()


class TestUnicodeZeroRejectedForBothCommands(InventoryCLITestCase):
    """两个命令对各种零字符文本统一按零值参数错误拒绝。"""

    ZERO_TEXTS = [
        "0",
        "000",
        FULLWIDTH_ZERO,
        FULLWIDTH_ZERO * 3,
        ARABIC_INDIC_ZERO,
        ARABIC_INDIC_ZERO * 3,
        # 不同体系零字符混写。
        "0" + FULLWIDTH_ZERO + ARABIC_INDIC_ZERO,
        "０٠0",
        # 长零文本：数千个同一体系的零字符，以及混写。
        ASCII_ZERO * LONG,
        FULLWIDTH_ZERO * LONG,
        ARABIC_INDIC_ZERO * LONG,
        (ASCII_ZERO * (LONG // 2))
        + (FULLWIDTH_ZERO * (LONG // 4))
        + (ARABIC_INDIC_ZERO * (LONG // 4)),
        # 非 ASCII 零字符作为前导零，整体数值仍为零。
        FULLWIDTH_ZERO + ARABIC_INDIC_ZERO + ASCII_ZERO,
    ]

    def test_zero_texts_rejected_for_both_commands(self):
        before_out, before = self.seed_demo_ledger()

        for cmd in ("receive", "issue"):
            for qty in self.ZERO_TEXTS:
                with self.subTest(cmd=cmd, qty=qty[:12]):
                    self.assert_zero_qty_rejected(
                        cmd, "--sku", SKU, "--qty", qty
                    )
                    # 每次拒绝后台账都原样不变（余额与两条流水）。
                    after_out, after = self.show()
                    self.assertEqual(after, before)
                    self.assertEqual(after_out, before_out)

    def test_acceptance_demo_fullwidth_and_arabic_indic_zero(self):
        """验收主场景：入库 10、出库 3 后，receive ０ 与 issue ٠ 均被拒绝。"""
        before_out, before = self.seed_demo_ledger()

        self.assert_zero_qty_rejected(
            "receive", "--sku", SKU, "--qty", FULLWIDTH_ZERO
        )
        self.assert_zero_qty_rejected(
            "issue", "--sku", SKU, "--qty", ARABIC_INDIC_ZERO
        )

        # show 仍返回数量 7 和原有两条流水，编号/类型/数量/余额全部原样。
        after_out, after = self.show()
        self.assertEqual(after_out, before_out)
        self.assertEqual(after["name"], NAME)
        self.assertEqual(after["quantity"], 7)
        self.assertEqual(
            [(m["id"], m["type"], m["quantity"], m["balance"])
             for m in after["movements"]],
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        self.assertEqual(len(after["movements"]), 2)


class TestZeroErrorPrecedesSkuAndDatabase(InventoryCLITestCase):
    """零数量错误先于 SKU 检查与数据库访问：不报错商品，也不建库文件。"""

    def test_zero_qty_with_missing_sku_rejected_first(self):
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for qty in ("0", "000", FULLWIDTH_ZERO, ARABIC_INDIC_ZERO,
                    "0" + FULLWIDTH_ZERO + ARABIC_INDIC_ZERO,
                    FULLWIDTH_ZERO * LONG):
            with self.subTest(qty=qty[:12]):
                proc = subprocess.run(
                    [
                        sys.executable, "-m", "inventory", "--db", missing_db,
                        "receive", "--sku", MISSING_SKU, "--qty", qty,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertIn("--qty", proc.stderr)
                self.assertIn("大于零", proc.stderr)
                self.assertNotIn("商品不存在", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(
                    Path(missing_db).exists(), "数量非法时不应创建台账文件"
                )

    def test_zero_qty_with_blank_sku_rejected_first(self):
        """零数量与去空白后为空的 SKU 同时出现：仍先报告数量错误。"""
        self.seed_demo_ledger()
        for sku in ("", "   ", "\t\n"):
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, sku=repr(sku)):
                    code, out, err = self.run_cli(
                        cmd, "--sku", sku, "--qty", FULLWIDTH_ZERO
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertIn("--qty", err)
                    self.assertIn("大于零", err)
                    self.assertNotIn("SKU", err.replace("--qty", ""))
                    self.assertNotIn("Traceback", err)


class TestValidUnicodeQuantitiesPreserved(InventoryCLITestCase):
    """合法非 ASCII 正整数继续按数值处理，结果为精确整数。"""

    def test_fullwidth_and_arabic_indic_positive_digits(self):
        self.add_demo_product()

        # 全角 ２：入库 2 件。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", "２"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 2}
        )

        # 阿拉伯印度 ٣：再入库 3 件，余额 5。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", "٣"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 5}
        )

        # 全角数字组成的 10（两位）：入库后余额 15。
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", "１０"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["quantity"], 15)

        # 非 ASCII 零字符作前导零不改变数值："０٠２" 与 2 等价，出库后余额 13。
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", FULLWIDTH_ZERO + ARABIC_INDIC_ZERO + "２"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["quantity"], 13)

        # 流水数量与余额均为精确整数，JSON 原文中不出现小数、指数或字符串。
        page_out, page = self.show()
        self.assertEqual(page["quantity"], 13)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [
                ("receive", 2, 2),
                ("receive", 3, 5),
                ("receive", 10, 15),
                ("issue", 2, 13),
            ],
        )
        for m in page["movements"]:
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)
        self.assertNotIn("2.0", page_out)
        self.assertNotIn('"2"', page_out)

    def test_long_leading_unicode_zeros_preserve_value(self):
        """数千个 Unicode 零字符后接 10 / 3：与普通 10 / 3 完全等价。"""
        self.add_demo_product()

        code, out, err = self.run_cli(
            "receive", "--sku", SKU,
            "--qty", (FULLWIDTH_ZERO * (LONG // 2))
            + (ARABIC_INDIC_ZERO * (LONG // 2)) + "10",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 10}
        )

        code, out, err = self.run_cli(
            "issue", "--sku", SKU,
            "--qty", ARABIC_INDIC_ZERO * LONG + "٣",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(out), {"sku": SKU, "name": NAME, "quantity": 7}
        )

        _, page = self.show()
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )

    def test_unicode_digits_over_max_still_rejected(self):
        """非 ASCII 数字同样受单次数量上限约束：超界按 --qty 上限拒绝。"""
        self.add_demo_product()
        # 用全角数字写出 9223372036854775808（上限 + 1）。
        over_max_fullwidth = "".join(
            chr(ord("０") + int(d)) for d in str(MAX_QTY + 1)
        )
        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                code, out, err = self.run_cli(
                    cmd, "--sku", SKU, "--qty", over_max_fullwidth
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--qty", err)
                self.assertIn(MAX_QTY_TEXT, err)
                self.assertNotIn("Traceback", err)
        _, page = self.show()
        self.assertEqual(page["quantity"], 0)
        self.assertEqual(page["movements"], [])

    def test_ascii_quantities_and_issue_over_balance_unchanged(self):
        """普通 ASCII 数量与出库超余额的既有业务规则保持不变。"""
        self.seed_demo_ledger()
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", "8"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("超过", err)
        _, page = self.show()
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )


if __name__ == "__main__":
    unittest.main()
