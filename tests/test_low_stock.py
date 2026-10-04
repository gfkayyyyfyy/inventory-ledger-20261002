"""low-stock 子命令的回归测试：按统一阈值查看低库存商品。

以 README 公开的 `python -m inventory --db ... low-stock` 命令入口为
验收对象，使用真实 SQLite 临时文件，验证：

- 必填的 --threshold 决定返回集合：当前数量小于或等于阈值的全部已登记
  商品（含没有流水、数量为零的商品）都返回，严格大于的不返回；
- 成功时退出码 0、标准错误为空，标准输出是只含 products 数组的单个
  JSON 对象；每项只含 sku、name、quantity，数量为精确整数，不带流水；
- 结果按 SKU 的 Unicode 字符顺序升序排列并区分大小写，与登记顺序无关；
- 阈值接受 0 至 9223372036854775807，数字文本沿用 --after-id 语义
  （Unicode 十进制数字、各文字混写、前导零不改变数值），零只匹配零库存；
- 完整值含空白、正负号、小数点或其他非数字，或参数省略、缺值、空字符串、
  数值越界时退出码 2、stdout 为空、stderr 含 --threshold 与原因、无堆栈，
  且在访问数据库前拒绝（不创建不存在的数据库文件、不改变已有数据）；
- 参数合法但数据库无法打开时退出码 1，stdout 为空；
- 查询为只读：不改变商品与流水，数据不变时重复结果一致；文件尚不存在
  且父目录存在时沿用建立空台账的约定，返回 {"products":[]}。

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

MAX_VALUE = 9223372036854775807
LEADING_ZEROS = "0" * 5000


class LowStockTestCase(unittest.TestCase):
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
        self.assertEqual(code, 0, f"命令 {args} 应成功，stderr: {err}")
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        return payload

    def add(self, sku, name):
        self.run_ok("add", "--sku", sku, "--name", name)

    def receive(self, sku, qty):
        self.assertEqual(self.run_ok("receive", "--sku", sku, "--qty", str(qty))["quantity"], qty)

    def issue(self, sku, qty):
        return self.run_ok("issue", "--sku", sku, "--qty", str(qty))["quantity"]

    def low_stock(self, threshold):
        return self.run_ok("low-stock", "--threshold", str(threshold))["products"]

    def seed_demo(self):
        """DEMO-1 螺母 0、DEMO-2 螺丝 5、DEMO-3 垫片 6。"""
        self.add("DEMO-1", "螺母")
        self.add("DEMO-2", "螺丝")
        self.add("DEMO-3", "垫片")
        self.receive("DEMO-2", 5)
        self.receive("DEMO-3", 6)

    def assert_threshold_error(self, code, out, err):
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--threshold", err)
        self.assertNotIn("Traceback", err, "参数拒绝不应输出异常堆栈")


class TestLowStockAcceptance(LowStockTestCase):
    """验收主场景：阈值 5 只返回前两项；出库后第三项以数量 5 返回。"""

    def test_threshold_five_returns_first_two_only(self):
        self.seed_demo()
        products = self.low_stock(5)
        self.assertEqual(
            products,
            [
                {"sku": "DEMO-1", "name": "螺母", "quantity": 0},
                {"sku": "DEMO-2", "name": "螺丝", "quantity": 5},
            ],
        )
        for item in products:
            self.assertEqual(set(item), {"sku", "name", "quantity"})
            self.assertIsInstance(item["quantity"], int)

    def test_issue_one_then_threshold_five_returns_all_three(self):
        self.seed_demo()
        self.assertEqual(self.issue("DEMO-3", 1), 5)
        products = self.low_stock(5)
        self.assertEqual(
            products,
            [
                {"sku": "DEMO-1", "name": "螺母", "quantity": 0},
                {"sku": "DEMO-2", "name": "螺丝", "quantity": 5},
                {"sku": "DEMO-3", "name": "垫片", "quantity": 5},
            ],
        )

    def test_zero_threshold_matches_only_zero_stock(self):
        self.seed_demo()
        self.assertEqual(
            self.low_stock(0),
            [{"sku": "DEMO-1", "name": "螺母", "quantity": 0}],
        )

    def test_products_without_movements_participate(self):
        # 只登记、从不出入库：数量为零，任何阈值都应返回。
        self.add("IDLE-1", "呆滞品")
        self.assertEqual(
            self.low_stock(0),
            [{"sku": "IDLE-1", "name": "呆滞品", "quantity": 0}],
        )

    def test_no_match_returns_empty_array(self):
        self.seed_demo()
        # 阈值 0：DEMO-1 数量为零会命中；先让它入库，再无零库存商品。
        self.receive("DEMO-1", 9)
        self.assertEqual(self.low_stock(0), [])

    def test_empty_ledger_returns_empty_array(self):
        # 空台账（表存在但无商品）。
        self.assertEqual(self.low_stock(5), [])

    def test_missing_file_with_existing_parent_creates_empty_ledger(self):
        missing = str(Path(self._tmp.name) / "fresh.db")
        code, out, err = self.run_cli(
            "low-stock", "--threshold", "5", db_path=missing
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"products": []})
        self.assertTrue(Path(missing).exists())

    def test_boundary_is_inclusive_and_upper_bound_accepted(self):
        self.seed_demo()
        # 阈值 4：数量 0 命中，5 与 6 不命中（边界为小于或等于）。
        self.assertEqual([p["sku"] for p in self.low_stock(4)], ["DEMO-1"])
        # 最大合法阈值：全部返回（数量不可能超过上界）。
        self.assertEqual(
            [p["sku"] for p in self.low_stock(MAX_VALUE)],
            ["DEMO-1", "DEMO-2", "DEMO-3"],
        )


class TestLowStockOrdering(LowStockTestCase):
    """按 SKU 的 Unicode 字符顺序升序、区分大小写，与登记顺序无关。"""

    def test_case_sensitive_unicode_order_independent_of_registration(self):
        # 故意按与码位顺序不同的次序登记。
        for sku, name in (
            ("b", "小写b"),
            ("A", "大写A"),
            ("中", "中文"),
            ("a", "小写a"),
            ("B", "大写B"),
            ("aa", "小写aa"),
        ):
            self.add(sku, name)
        skus = [p["sku"] for p in self.low_stock(0)]
        # 大写字母码位先于小写；同首字母短串在前；中文码位在 ASCII 之后。
        self.assertEqual(skus, ["A", "B", "a", "aa", "b", "中"])


class TestLowStockDigitText(LowStockTestCase):
    """阈值数字文本沿用 --after-id 语义。"""

    def test_unicode_digits_mixed_scripts_and_leading_zeros(self):
        self.seed_demo()
        expected = [p["sku"] for p in self.low_stock(5)]
        # 全角 ００５、全角 ５、阿拉伯印度 ٥ 与 ASCII 5 同值。
        for text in ("0005", "００５", "٥", "5", LEADING_ZEROS + "5"):
            with self.subTest(threshold=text[:6]):
                payload = self.run_ok("low-stock", "--threshold", text)
                self.assertEqual([p["sku"] for p in payload["products"]], expected)
        # 5000 个零与阈值 0 等价（只匹配零库存的 DEMO-1）。
        payload = self.run_ok("low-stock", "--threshold", LEADING_ZEROS)
        self.assertEqual(
            [p["sku"] for p in payload["products"]], ["DEMO-1"]
        )


class TestLowStockInvalidThreshold(LowStockTestCase):
    """各类非法阈值在访问数据库前以退出码 2 拒绝。"""

    def test_invalid_forms_rejected_before_database_access(self):
        self.seed_demo()
        bad_values = (
            "",                    # 空字符串
            "-1",                  # 负号
            "+5",                  # 正号
            "5.0",                 # 小数点
            " 5", "5 ", "5 5",     # 空白：开头/结尾/中间
            "5\n", "5\t", "5\r",   # 其他空白
            "abc", "0x5", "5a",    # 非数字
            "5_0",                 # 下划线
            str(MAX_VALUE + 1),    # 越界 1
            "9" * 30,              # 位数远超上界
        )
        for bad in bad_values:
            with self.subTest(threshold=bad):
                code, out, err = self.run_cli("low-stock", "--threshold", bad)
                self.assert_threshold_error(code, out, err)
                # 拒绝不改变数据：阈值 5 的结果仍是前两项。
                self.assertEqual(
                    [p["sku"] for p in self.low_stock(5)],
                    ["DEMO-1", "DEMO-2"],
                )

    def test_missing_and_omitted_threshold_rejected(self):
        # 缺值（--threshold 后无值）。
        code, out, err = self.run_cli("low-stock", "--threshold")
        self.assert_threshold_error(code, out, err)
        # 完全省略 --threshold。
        code, out, err = self.run_cli("low-stock")
        self.assert_threshold_error(code, out, err)

    def test_rejection_does_not_create_database_file(self):
        missing = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("", "-1", "5\n", "+5", "5.0", "abc", str(MAX_VALUE + 1)):
            with self.subTest(threshold=bad):
                code, out, err = self.run_cli(
                    "low-stock", "--threshold", bad, db_path=missing
                )
                self.assert_threshold_error(code, out, err)
                self.assertFalse(
                    Path(missing).exists(), "参数非法时不应创建台账文件"
                )


class TestLowStockReadonly(LowStockTestCase):
    """查询只读：商品、流水不变，重复结果一致。"""

    def test_repeated_queries_identical_and_data_unchanged(self):
        self.seed_demo()
        first, err = [], ""
        code, out1, err = self.run_cli("low-stock", "--threshold", "5")
        self.assertEqual(code, 0)
        code, out2, err = self.run_cli("low-stock", "--threshold", "5")
        self.assertEqual(code, 0)
        self.assertEqual(out1, out2)
        # show 仍能看到完整流水：low-stock 未改动任何数据。
        page = self.run_ok("show", "--sku", "DEMO-3")
        self.assertEqual(page["quantity"], 6)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 6, 6)],
        )
        # 阈值不保存：下一次换阈值 0 只返回零库存项。
        self.assertEqual(
            [p["sku"] for p in self.low_stock(0)], ["DEMO-1"]
        )


class TestLowStockDatabaseError(LowStockTestCase):
    """参数合法但数据库无法打开：退出码 1，stdout 为空。"""

    def test_nonexistent_parent_directory_is_runtime_error(self):
        bad_db = str(Path(self._tmp.name) / "no-dir" / "ledger.db")
        code, out, err = self.run_cli(
            "low-stock", "--threshold", "5", db_path=bad_db
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertTrue(err.strip())


if __name__ == "__main__":
    unittest.main()
