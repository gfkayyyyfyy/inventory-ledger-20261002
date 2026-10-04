"""low-stock --format csv 的回归测试：同一低库存清单的 CSV 输出选项。

以 README 公开的 `python -m inventory --db ... low-stock` 命令入口为
验收对象，使用真实 SQLite 临时文件，验证：

- 省略 --format 或指定 json 时，输出与既有行为完全一致的单个 JSON 对象；
- 指定 csv 时导出与同一阈值下 JSON 完全相同的商品集合：首行固定为
  sku,name,quantity，随后每个商品一条记录，按区分大小写的 SKU 字符
  顺序排列；输出为无 BOM 的 UTF-8，记录以 LF 结束（不出现 CR）；
- SKU 与名称保留原始文本；字段含逗号、双引号、回车或换行时用双引号
  包裹并把内部双引号写成两个双引号，字段内部的回车与换行保持原样；
- 数量写为不带分组符或指数的 ASCII 十进制整数；没有匹配商品时只输出
  表头；成功时退出码 0、标准错误为空；
- --format 缺值、空字符串、其他取值、大小写变体或带两端空白的取值
  均以退出码 2 拒绝，stdout 为空、stderr 含 --format 与原因、无堆栈，
  且校验发生在访问数据库之前（不创建不存在的数据库文件、不改变已有
  数据）；
- 格式与阈值合法但数据库无法打开时退出码 1，stdout 为空；
- 数据库文件尚不存在但父目录存在时仍建立空台账并只输出表头；
- 导出不改变商品、库存或流水，也不保存格式与阈值；add/receive/issue/
  show 与既有数据库数据保持兼容。

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

# UTF-8 字节序标记；CSV 输出不允许以它开头。
UTF8_BOM = b"\xef\xbb\xbf"


class LowStockCsvTestCase(unittest.TestCase):
    """每个用例使用独立的临时数据库，互不依赖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")

    def run_cli(self, *args, db_path=None):
        # 以字节捕获输出，才能精确核验无 BOM、LF 行尾与 UTF-8 编码。
        proc = subprocess.run(
            [
                sys.executable, "-m", "inventory",
                "--db", self.db_path if db_path is None else db_path,
                *args,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run_ok_json(self, *args):
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"命令 {args} 应成功，stderr: {err!r}")
        self.assertEqual(err, b"")
        return json.loads(out.decode("utf-8"))

    def run_ok_csv(self, *args, db_path=None):
        code, out, err = self.run_cli(*args, db_path=db_path)
        self.assertEqual(code, 0, f"命令 {args} 应成功，stderr: {err!r}")
        self.assertEqual(err, b"")
        self.assertFalse(out.startswith(UTF8_BOM), "CSV 输出不应带 BOM")
        # 行尾是否为 LF 由各用例的精确文本断言核验（字段内部允许出现
        # 原始的回车与换行，不能在此对字节内容做一刀切检查）。
        return out.decode("utf-8")

    def add(self, sku, name):
        self.run_ok_json("add", "--sku", sku, "--name", name)

    def receive(self, sku, qty):
        payload = self.run_ok_json("receive", "--sku", sku, "--qty", str(qty))
        self.assertEqual(payload["quantity"], qty)

    def seed_demo(self):
        """DEMO-1 螺母 0、DEMO-2 螺丝 5、DEMO-3 垫片 6。"""
        self.add("DEMO-1", "螺母")
        self.add("DEMO-2", "螺丝")
        self.add("DEMO-3", "垫片")
        self.receive("DEMO-2", 5)
        self.receive("DEMO-3", 6)

    def assert_format_error(self, code, out, err):
        self.assertEqual(code, 2)
        self.assertEqual(out, b"")
        text = err.decode("utf-8")
        self.assertIn("--format", text)
        self.assertNotIn("Traceback", text, "参数拒绝不应输出异常堆栈")


class TestCsvAcceptance(LowStockCsvTestCase):
    """验收主场景：阈值 5 的 CSV 与 JSON 是同一商品集合的两种格式。"""

    def test_threshold_five_csv_matches_json_set(self):
        self.seed_demo()
        csv_text = self.run_ok_csv("low-stock", "--threshold", "5", "--format", "csv")
        self.assertEqual(
            csv_text,
            "sku,name,quantity\nDEMO-1,螺母,0\nDEMO-2,螺丝,5\n",
        )
        # 同阈值的默认查询仍返回对应的 JSON 清单。
        payload = self.run_ok_json("low-stock", "--threshold", "5")
        self.assertEqual(
            payload,
            {
                "products": [
                    {"sku": "DEMO-1", "name": "螺母", "quantity": 0},
                    {"sku": "DEMO-2", "name": "螺丝", "quantity": 5},
                ]
            },
        )

    def test_explicit_json_identical_to_omitted(self):
        self.seed_demo()
        code1, out1, _ = self.run_cli("low-stock", "--threshold", "5")
        code2, out2, _ = self.run_cli(
            "low-stock", "--threshold", "5", "--format", "json"
        )
        self.assertEqual((code1, out1), (code2, out2))

    def test_no_match_outputs_header_only(self):
        self.seed_demo()
        self.receive("DEMO-1", 9)  # 不再有零库存商品
        self.assertEqual(
            self.run_ok_csv("low-stock", "--threshold", "0", "--format", "csv"),
            "sku,name,quantity\n",
        )

    def test_empty_ledger_outputs_header_only(self):
        self.assertEqual(
            self.run_ok_csv("low-stock", "--threshold", "5", "--format", "csv"),
            "sku,name,quantity\n",
        )

    def test_missing_file_with_existing_parent_creates_empty_ledger(self):
        missing = str(Path(self._tmp.name) / "fresh.db")
        text = self.run_ok_csv(
            "low-stock", "--threshold", "5", "--format", "csv", db_path=missing
        )
        self.assertEqual(text, "sku,name,quantity\n")
        self.assertTrue(Path(missing).exists())

    def test_unicode_threshold_text_with_csv(self):
        self.seed_demo()
        # 全角 ５ 与 ASCII 5 同值，CSV 集合不变。
        self.assertEqual(
            self.run_ok_csv("low-stock", "--threshold", "５", "--format", "csv"),
            "sku,name,quantity\nDEMO-1,螺母,0\nDEMO-2,螺丝,5\n",
        )


class TestCsvQuotingAndOrdering(LowStockCsvTestCase):
    """字段引用规则、原始文本保留与区分大小写的 SKU 排序。"""

    def test_fields_with_comma_quote_cr_lf_are_quoted(self):
        self.add("Q,1", '含,逗号')
        self.add('Q"2', '含"双引号')
        self.add("Q3", "含\r回车")
        self.add("Q4", "含\n换行")
        self.add("PLAIN", "普通")
        text = self.run_ok_csv("low-stock", "--threshold", "0", "--format", "csv")
        # SKU 按字符顺序排列：'P' < 'Q'，且 '"' 先于 ','，故 Q"2 在 Q,1 之前；
        # 字段内部的回车与换行保持原样，记录仍以 LF 结束。
        self.assertEqual(
            text,
            "sku,name,quantity\n"
            "PLAIN,普通,0\n"
            '"Q""2","含""双引号",0\n'
            '"Q,1","含,逗号",0\n'
            'Q3,"含\r回车",0\n'
            'Q4,"含\n换行",0\n',
        )

    def test_case_sensitive_sku_order_matches_json(self):
        for sku, name in (
            ("b", "小写b"),
            ("A", "大写A"),
            ("中", "中文"),
            ("a", "小写a"),
            ("B", "大写B"),
            ("aa", "小写aa"),
        ):
            self.add(sku, name)
        text = self.run_ok_csv("low-stock", "--threshold", "0", "--format", "csv")
        lines = text.split("\n")
        self.assertEqual(lines[0], "sku,name,quantity")
        self.assertEqual(
            [line.split(",", 1)[0] for line in lines[1:-1]],
            ["A", "B", "a", "aa", "b", "中"],
        )

    def test_quantity_is_exact_ascii_decimal(self):
        self.add("BIG", "大额")
        self.receive("BIG", MAX_VALUE)
        text = self.run_ok_csv(
            "low-stock", "--threshold", str(MAX_VALUE), "--format", "csv"
        )
        self.assertEqual(text, f"sku,name,quantity\nBIG,大额,{MAX_VALUE}\n")


class TestInvalidFormat(LowStockCsvTestCase):
    """各类非法 --format 取值在访问数据库前以退出码 2 拒绝。"""

    BAD_VALUES = (
        "",                # 空字符串
        "JSON", "Json",    # 大小写变体
        "CSV", "Csv",
        " json", "json ",  # 带两端空白
        "csv ", " csv",
        "json\n", "csv\t", # 其他空白
        "xml", "text", "js", "jsonl",  # 其他取值
    )

    def test_invalid_values_rejected_before_database_access(self):
        self.seed_demo()
        for bad in self.BAD_VALUES:
            with self.subTest(format=bad):
                code, out, err = self.run_cli(
                    "low-stock", "--threshold", "5", "--format", bad
                )
                self.assert_format_error(code, out, err)
                # 拒绝不改变数据：阈值 5 的 JSON 结果仍是前两项。
                payload = self.run_ok_json("low-stock", "--threshold", "5")
                self.assertEqual(
                    [p["sku"] for p in payload["products"]],
                    ["DEMO-1", "DEMO-2"],
                )

    def test_missing_value_rejected(self):
        code, out, err = self.run_cli("low-stock", "--threshold", "5", "--format")
        self.assert_format_error(code, out, err)

    def test_rejection_does_not_create_database_file(self):
        missing = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("", "JSON", " csv", "xml"):
            with self.subTest(format=bad):
                code, out, err = self.run_cli(
                    "low-stock", "--threshold", "5", "--format", bad,
                    db_path=missing,
                )
                self.assert_format_error(code, out, err)
                self.assertFalse(
                    Path(missing).exists(), "格式非法时不应创建台账文件"
                )

    def test_format_error_reported_before_threshold_error(self):
        # 格式与阈值同时非法时，先按 --format 的参数错误拒绝。
        code, out, err = self.run_cli(
            "low-stock", "--format", "JSON", "--threshold", "abc"
        )
        self.assert_format_error(code, out, err)


class TestCsvDatabaseErrorAndReadonly(LowStockCsvTestCase):
    """数据库错误路径与只读语义。"""

    def test_nonexistent_parent_directory_is_runtime_error(self):
        bad_db = str(Path(self._tmp.name) / "no-dir" / "ledger.db")
        code, out, err = self.run_cli(
            "low-stock", "--threshold", "5", "--format", "csv", db_path=bad_db
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, b"")
        self.assertNotIn(b"Traceback", err)
        self.assertTrue(err.strip())

    def test_export_does_not_change_data_or_persist_format(self):
        self.seed_demo()
        self.run_ok_csv("low-stock", "--threshold", "5", "--format", "csv")
        self.run_ok_csv("low-stock", "--threshold", "5", "--format", "csv")
        # show 仍能看到完整流水：导出未改动任何数据。
        page = self.run_ok_json("show", "--sku", "DEMO-3")
        self.assertEqual(page["quantity"], 6)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 6, 6)],
        )
        # 格式与阈值不保存：默认仍是 JSON，换阈值 0 只返回零库存项。
        payload = self.run_ok_json("low-stock", "--threshold", "0")
        self.assertEqual(
            payload["products"],
            [{"sku": "DEMO-1", "name": "螺母", "quantity": 0}],
        )


if __name__ == "__main__":
    unittest.main()
