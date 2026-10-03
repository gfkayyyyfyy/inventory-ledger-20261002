"""Unicode 零字符数量的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证：

- --qty 的纯十进制数字文本只要实际数值为零就统一按参数错误拒绝：
  "0"、"000"、全角零“０”（U+FF10）、阿拉伯印度零“٠”（U+0660）、
  其他文字的十进制零（如 NKo 零“߀”，U+07C0）以及这些零字符的混写
  （如 "0０٠"、5000 个 ASCII 零、5000 个全角零），receive 与 issue
  两个命令都得到退出码 2、stdout 为空、stderr 含 --qty 并说明数量必须
  大于零、不出现异常堆栈；
- 零数量拒绝发生在打开数据库与检查商品之前：与未登记 SKU、去空白后为空
  的 SKU 同时出现时仍先报告数量错误，尚不存在的数据库文件不被创建；
- 拒绝后台账原样不变：商品名称、库存余额以及每条流水的编号、类型、数量、
  余额保持原样，不新增流水；
- 既有合法非 ASCII 正整数（全角“２”、阿拉伯印度“٣”）仍可出入库，
  返回数量与流水数量为精确整数，前导零（含非 ASCII 零）不改变数值。

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

# 各种文字的十进制零字符。
ASCII_ZERO = "0"
FULLWIDTH_ZERO = "０"      # 全角零 U+FF10
ARABIC_INDIC_ZERO = "٠"   # 阿拉伯印度零 U+0660
NKO_ZERO = "߀"             # NKo 零 U+07C0

ZERO_TEXTS = [
    "0",
    "000",
    FULLWIDTH_ZERO,
    ARABIC_INDIC_ZERO,
    NKO_ZERO,
    "0" + FULLWIDTH_ZERO + ARABIC_INDIC_ZERO,
    FULLWIDTH_ZERO + NKO_ZERO + ASCII_ZERO,
    ASCII_ZERO * 5000,          # 长零文本：5000 个 ASCII 零
    FULLWIDTH_ZERO * 5000,      # 长零文本：5000 个全角零
    ASCII_ZERO * 1000 + FULLWIDTH_ZERO * 1000 + ARABIC_INDIC_ZERO * 1000,
]


class InventoryCLITestCase(unittest.TestCase):
    """每个用例使用独立的临时数据库，互不依赖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")

    def run_cli(self, *args, db_path=...):
        """运行一条 CLI 命令，返回 (退出码, stdout 文本, stderr 文本)。"""
        if db_path is ...:
            db_path = self.db_path
        proc = subprocess.run(
            [sys.executable, "-m", "inventory", "--db", db_path, *args],
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

    def assert_qty_error(self, code, out, err, args):
        """零数量必须以参数错误（退出码 2）拒绝：stdout 空、stderr 含 --qty
        与“大于零”，且不出现异常堆栈。"""
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝，stderr: {err}")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertIn("--qty", err, f"命令 {args} 的 stderr 应含 --qty: {err}")
        self.assertIn("数量", err)
        self.assertIn("大于零", err, f"命令 {args} 的 stderr 应说明数量必须大于零: {err}")
        self.assertNotIn("Traceback", err, "不应出现异常堆栈")
        # 零值不属于“超过单次上限”，错误中不应出现上限数值。
        self.assertNotIn("9223372036854775807", err)

    def add_demo_product(self):
        payload = self.run_ok("add", "--sku", SKU, "--name", NAME)
        self.assertEqual(payload, {"sku": SKU, "name": NAME, "quantity": 0})

    def show_raw(self):
        code, out, err = self.run_cli("show", "--sku", SKU)
        self.assertEqual(code, 0, f"show 应成功，stderr: {err}")
        return out, json.loads(out)

    def seed_balance_seven(self):
        """登记 DEMO-1 并入库 10、出库 3，得到余额 7 与两条原始流水。"""
        self.add_demo_product()
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "3")["quantity"], 7
        )
        return self.show_raw()


class TestZeroQuantityRejected(InventoryCLITestCase):
    """零字符文本：receive/issue 都按参数错误拒绝。"""

    def test_zero_texts_rejected_for_both_commands(self):
        self.seed_balance_seven()

        for qty in ZERO_TEXTS:
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty=qty[:12]):
                    code, out, err = self.run_cli(cmd, "--sku", SKU, "--qty", qty)
                    self.assert_qty_error(code, out, err, (cmd, qty[:12]))

    def test_acceptance_fullwidth_zero_receive(self):
        """验收：入库 10、出库 3 后 receive --qty ０ 被拒绝，台账不变。"""
        before_out, before = self.seed_balance_seven()
        code, out, err = self.run_cli(
            "receive", "--sku", SKU, "--qty", FULLWIDTH_ZERO
        )
        self.assert_qty_error(code, out, err, ("receive", FULLWIDTH_ZERO))
        after_out, after = self.show_raw()
        self.assertEqual(after, before)
        self.assertEqual(after_out, before_out)

    def test_acceptance_arabic_indic_zero_issue(self):
        """验收：issue --qty ٠ 被拒绝，台账仍为余额 7 与原有两条流水。"""
        before_out, before = self.seed_balance_seven()
        code, out, err = self.run_cli(
            "issue", "--sku", SKU, "--qty", ARABIC_INDIC_ZERO
        )
        self.assert_qty_error(code, out, err, ("issue", ARABIC_INDIC_ZERO))
        after_out, after = self.show_raw()
        self.assertEqual(after, before)
        self.assertEqual(after_out, before_out)
        self.assertEqual(after["quantity"], 7)
        self.assertEqual(
            [
                (m["id"], m["type"], m["quantity"], m["balance"])
                for m in after["movements"]
            ],
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )


class TestZeroRejectionLeavesDataUntouched(InventoryCLITestCase):
    """拒绝零数量不新增流水：商品、余额与每条流水原样保留。"""

    def test_data_unchanged_after_mixed_zero_rejections(self):
        before_out, before = self.seed_balance_seven()
        self.assertEqual(before["name"], NAME)
        self.assertEqual(before["quantity"], 7)

        for qty in ZERO_TEXTS:
            for cmd in ("receive", "issue"):
                with self.subTest(cmd=cmd, qty=qty[:12]):
                    code, out, err = self.run_cli(cmd, "--sku", SKU, "--qty", qty)
                    self.assert_qty_error(code, out, err, (cmd, qty[:12]))
                    after_out, after = self.show_raw()
                    # 商品名称、余额不变。
                    self.assertEqual(after["name"], NAME)
                    self.assertEqual(after["quantity"], 7)
                    # 流水编号、类型、数量、余额全部原样，且没有新增流水。
                    self.assertEqual(
                        [
                            (m["id"], m["type"], m["quantity"], m["balance"])
                            for m in after["movements"]
                        ],
                        [(1, "receive", 10, 10), (2, "issue", 3, 7)],
                    )
                    self.assertEqual(after_out, before_out)


class TestZeroErrorPrecedesSkuAndDatabase(InventoryCLITestCase):
    """零数量错误优先：先于商品检查，且不打开/创建数据库文件。"""

    def test_zero_qty_with_missing_sku_rejected_first(self):
        # 台账文件尚不存在：零数量必须在打开数据库前被拒绝，文件不被创建。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for qty in ("0", FULLWIDTH_ZERO, ARABIC_INDIC_ZERO, "0" * 5000):
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
                    Path(missing_db).exists(), "零数量被拒绝时不应创建台账文件"
                )

    def test_zero_qty_with_blank_sku_rejected_first(self):
        # 即使 SKU 去空白后为空，仍先报告数量错误（参数解析先于业务校验）。
        for cmd in ("receive", "issue"):
            with self.subTest(cmd=cmd):
                code, out, err = self.run_cli(
                    cmd, "--sku", "   ", "--qty", ARABIC_INDIC_ZERO
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--qty", err)
                self.assertIn("大于零", err)
                self.assertNotIn("SKU", err)
                self.assertNotIn("Traceback", err)


class TestValidNonAsciiQuantities(InventoryCLITestCase):
    """合法非 ASCII 正整数继续按既有十进制数值处理，结果为精确整数。"""

    def test_fullwidth_and_arabic_indic_digits_valid(self):
        self.add_demo_product()

        # 全角“２”入库：数量精确为 2。
        received = self.run_ok("receive", "--sku", SKU, "--qty", "２")
        self.assertEqual(received, {"sku": SKU, "name": NAME, "quantity": 2})

        # 阿拉伯印度“٣”出库：数量精确为 3，余额 2 - 3 不足 -> 先补足余额。
        # 改用：先入库 ASCII 5（余额 7），再以阿拉伯印度 ٣ 出库，余额 4。
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "5")["quantity"], 7
        )
        issued = self.run_ok("issue", "--sku", SKU, "--qty", "٣")
        self.assertEqual(issued, {"sku": SKU, "name": NAME, "quantity": 4})

        _, page = self.show_raw()
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 2, 2), ("receive", 5, 7), ("issue", 3, 4)],
        )
        for m in page["movements"]:
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)

    def test_non_ascii_leading_zeros_preserve_value(self):
        """非 ASCII 零作前导零不改变数值：０２ 与 2 等价，٠٠٠٣ 与 3 等价。"""
        self.add_demo_product()
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "０２")["quantity"], 2
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", ARABIC_INDIC_ZERO + "٠٠٣")[
                "quantity"
            ],
            5
        )
        page = self.run_ok("show", "--sku", SKU)
        self.assertEqual(page["quantity"], 5)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 2, 2), ("receive", 3, 5)],
        )

    def test_mixed_script_digits_valid(self):
        """不同文字的十进制数字混写按同一数值处理：2０٣ 等于 203。"""
        self.add_demo_product()
        payload = self.run_ok("receive", "--sku", SKU, "--qty", "2" + FULLWIDTH_ZERO + "٣")
        self.assertEqual(payload["quantity"], 203)
        _, page = self.show_raw()
        self.assertEqual(page["movements"][0]["quantity"], 203)
        self.assertIsInstance(page["movements"][0]["quantity"], int)

    def test_long_ascii_zeros_before_ten_still_valid(self):
        """既有行为保留：5000 个 ASCII 零后接 10 与普通 10 等价。"""
        self.add_demo_product()
        payload = self.run_ok(
            "receive", "--sku", SKU, "--qty", ASCII_ZERO * 5000 + "10"
        )
        self.assertEqual(payload["quantity"], 10)
        _, page = self.show_raw()
        self.assertEqual(page["movements"][0]["quantity"], 10)


if __name__ == "__main__":
    unittest.main()
