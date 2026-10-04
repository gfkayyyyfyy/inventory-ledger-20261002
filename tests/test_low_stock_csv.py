"""low-stock --format csv 的回归测试。

以 README 公开的 `python -m inventory --db ... low-stock` 命令入口为
验收对象，使用真实 SQLite 临时文件，验证：

- 省略 --format 或显式 --format json 时输出与原来完全一致的单个 JSON
  对象（字段、排序、阈值判断、退出码、stderr 均不变）；
- --format csv 时商品集合与同阈值 JSON 完全相同，只改变输出格式：
  标准输出为无 BOM 的 UTF-8、记录以 LF 结束、首行固定 sku,name,quantity，
  每个商品一条逻辑记录，按既有区分大小写的 SKU 字符顺序排列；
- SKU 与名称保留原始文本：含逗号、双引号、回车或换行的字段用双引号包裹，
  内部双引号写成两个双引号，字段内部回车与换行原样保留；数量写为精确
  ASCII 十进制整数；无匹配商品时只输出表头；
- --format 缺值、空字符串、其他取值、大小写变体或带两端空白均以退出码 2
  拒绝，stdout 为空，stderr 含 --format 与原因且无异常堆栈；校验发生在
  访问数据库前（不创建数据库文件、不改变已有数据）；
- 格式与阈值合法但数据库无法打开时退出码 1、stdout 为空；文件尚不存在
  且父目录存在时仍建立空台账并只输出表头；
- CSV 导出只读：不改变商品、库存或流水，不保存格式与阈值。

注意：含回车的用例以字节方式捕获子进程输出，避免 text=True 的统一
换行转换把字段内部的 \\r 改写。

从项目根目录执行：

    python -m unittest discover
"""

import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MAX_VALUE = 9223372036854775807

HEADER = "sku,name,quantity\n"


class LowStockCsvTestCase(unittest.TestCase):
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

    def run_cli_bytes(self, *args, db_path=None):
        """以字节方式捕获，保留字段内部的回车/换行，不做统一换行转换。"""
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

    def run_json_ok(self, *args):
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"命令 {args} 应成功，stderr: {err}")
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        return payload

    def add(self, sku, name):
        self.run_json_ok("add", "--sku", sku, "--name", name)

    def receive(self, sku, qty):
        payload = self.run_json_ok("receive", "--sku", sku, "--qty", str(qty))
        self.assertEqual(payload["quantity"], qty)

    def seed_demo(self):
        """DEMO-1 螺母 0、DEMO-2 螺丝 5、DEMO-3 垫片 6。"""
        self.add("DEMO-1", "螺母")
        self.add("DEMO-2", "螺丝")
        self.add("DEMO-3", "垫片")
        self.receive("DEMO-2", 5)
        self.receive("DEMO-3", 6)

    def csv_text(self, threshold, threshold_text=None):
        """以 --format csv 查询并返回解码后的文本，另返回原始字节。"""
        text_arg = str(threshold) if threshold_text is None else threshold_text
        code, raw, err = self.run_cli_bytes(
            "low-stock", "--threshold", text_arg, "--format", "csv"
        )
        self.assertEqual(code, 0, f"CSV 查询应成功，stderr: {err!r}")
        self.assertEqual(err, b"")
        # 严格按 UTF-8 解码（无 BOM）：非 UTF-8 字节或 BOM 都会在此暴露。
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "CSV 不应带 UTF-8 BOM")
        return raw.decode("utf-8"), raw

    @staticmethod
    def parse_csv(text):
        # newline="" 让 csv 模块自行处理字段内部的回车与换行，不做翻译。
        return list(csv.reader(io.StringIO(text, newline="")))


class TestLowStockCsvAcceptance(LowStockCsvTestCase):
    """验收主场景：表头 + 螺母 0、螺丝 5；JSON 输出保持原样。"""

    def test_csv_threshold_five_matches_spec_example(self):
        self.seed_demo()
        text, raw = self.csv_text(5)
        # 与需求给出的示例逐字节一致：LF 结束、无 BOM、数量为 ASCII 整数。
        self.assertEqual(
            text,
            "sku,name,quantity\n"
            "DEMO-1,螺母,0\n"
            "DEMO-2,螺丝,5\n",
        )
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        self.assertNotIn(b"\xef\xbb\xbf", raw)

    def test_default_and_json_format_keep_original_json_unchanged(self):
        self.seed_demo()
        expected = {"products": [
            {"sku": "DEMO-1", "name": "螺母", "quantity": 0},
            {"sku": "DEMO-2", "name": "螺丝", "quantity": 5},
        ]}
        code_default, out_default, err_default = self.run_cli(
            "low-stock", "--threshold", "5"
        )
        code_json, out_json, err_json = self.run_cli(
            "low-stock", "--threshold", "5", "--format", "json"
        )
        self.assertEqual((code_default, err_default), (0, ""))
        self.assertEqual((code_json, err_json), (0, ""))
        # 省略与显式 json 的文本一致，且仍是原有单个 JSON 对象。
        self.assertEqual(out_default, out_json)
        self.assertEqual(json.loads(out_default), expected)

    def test_csv_set_identical_to_json_at_every_threshold(self):
        self.seed_demo()
        for value in (0, 4, 5, 6, MAX_VALUE):
            with self.subTest(threshold=value):
                products = self.run_json_ok(
                    "low-stock", "--threshold", str(value)
                )["products"]
                text, _ = self.csv_text(value)
                rows = self.parse_csv(text)
                self.assertEqual(rows[0], ["sku", "name", "quantity"])
                self.assertEqual(
                    rows[1:],
                    [[p["sku"], p["name"], str(p["quantity"])] for p in products],
                )
                # 数量往返为精确整数。
                self.assertEqual(
                    [int(r[2]) for r in rows[1:]],
                    [p["quantity"] for p in products],
                )

    def test_csv_keeps_unicode_digit_threshold_semantics(self):
        # 阈值数字文本沿用既有语义：全角 ５ 与 ASCII 5 命中同一集合。
        self.seed_demo()
        text, _ = self.csv_text(5, threshold_text="５")
        self.assertEqual(
            [r[0] for r in self.parse_csv(text)[1:]], ["DEMO-1", "DEMO-2"]
        )


class TestLowStockCsvLayout(LowStockCsvTestCase):
    """表头、空结果、排序、数量写法。"""

    def test_no_matches_outputs_header_only(self):
        self.seed_demo()
        self.receive("DEMO-1", 9)
        text, raw = self.csv_text(0)
        self.assertEqual(text, HEADER)
        self.assertEqual(raw, b"sku,name,quantity\n")

    def test_empty_ledger_outputs_header_only(self):
        text, raw = self.csv_text(5)
        self.assertEqual(text, HEADER)
        self.assertEqual(raw, b"sku,name,quantity\n")

    def test_missing_file_with_existing_parent_outputs_header_and_creates_ledger(self):
        fresh = str(Path(self._tmp.name) / "fresh.db")
        code, raw, err = self.run_cli_bytes(
            "low-stock", "--threshold", "5", "--format", "csv", db_path=fresh
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, b"")
        self.assertEqual(raw, b"sku,name,quantity\n")
        self.assertTrue(Path(fresh).exists())

    def test_rows_ordered_by_case_sensitive_sku_codepoints(self):
        for sku, name in (
            ("b", "小写b"),
            ("A", "大写A"),
            ("中", "中文"),
            ("a", "小写a"),
            ("B", "大写B"),
            ("aa", "小写aa"),
        ):
            self.add(sku, name)
        text, _ = self.csv_text(0)
        skus = [r[0] for r in self.parse_csv(text)[1:]]
        self.assertEqual(skus, ["A", "B", "a", "aa", "b", "中"])

    def test_max_quantity_written_as_plain_ascii_decimal_integer(self):
        self.add("BIG", "大数")
        self.receive("BIG", MAX_VALUE)
        text, raw = self.csv_text(MAX_VALUE)
        rows = self.parse_csv(text)
        self.assertEqual(rows[-1], ["BIG", "大数", str(MAX_VALUE)])
        # 数量字段是纯 ASCII 十进制文本：无分组符、无指数、无小数点。
        qty_field = raw.splitlines()[-1].rsplit(b",", 1)[1]
        self.assertEqual(qty_field, b"9223372036854775807")
        self.assertTrue(all(c in b"0123456789" for c in qty_field))


class TestLowStockCsvQuoting(LowStockCsvTestCase):
    """逗号、双引号、回车、换行的加引号与转义规则。"""

    def test_special_characters_are_quoted_and_round_trip(self):
        seeded = [
            ("S,1", "螺母,A", 0),        # 逗号
            ('S"2', '他说"你好"', 0),    # 双引号
            ("S\r3", "行1\r行2", 0),     # 回车
            ("S\n4", "换\n行", 0),       # 换行
        ]
        for sku, name, qty in seeded:
            self.add(sku, name)
        # 让双引号商品持有最大数量，验证大整数仍参与同一 CSV 集合。
        self.receive('S"2', MAX_VALUE)

        text, raw = self.csv_text(MAX_VALUE)

        # 记录结束符一律 LF：不出现 CRLF（种子数据本身也没有相邻的 \r\n）。
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))

        # 针对四种字符的逐字节加引号规则。
        self.assertIn(b'"S,1","\xe8\x9e\xba\xe6\xaf\x8d,A",0\n', raw)
        self.assertIn(
            ('"S""2","他说""你好""",%d\n' % MAX_VALUE).encode("utf-8"), raw
        )
        self.assertIn(b'"S\r3","\xe8\xa1\x8c1\r\xe8\xa1\x8c2",0\n', raw)
        self.assertIn(b'"S\n4","\xe6\x8d\xa2\n\xe8\xa1\x8c",0\n', raw)

        # 用 csv 读回：字段内部 CR/LF 保持原样，双引号还原为单个，
        # 记录集合（按二进制 SKU 排序）与写入完全一致。
        rows = self.parse_csv(text)
        self.assertEqual(rows[0], ["sku", "name", "quantity"])
        ordered = sorted(seeded, key=lambda p: p[0].encode("utf-8"))
        self.assertEqual(
            rows[1:],
            [[sku, name, str(qty if sku != 'S"2' else MAX_VALUE)]
             for sku, name, qty in ordered],
        )

    def test_record_with_embedded_newline_is_one_logical_record(self):
        # 首行表头之后，含换行的商品仍是一条逻辑记录：csv 解析共两行记录，
        # 物理上多出的换行只存在于引号字段内部。
        self.add("ONLY", "第一行\n第二行")
        text, raw = self.csv_text(0)
        rows = self.parse_csv(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], ["ONLY", "第一行\n第二行", "0"])
        # 只有含换行的名称需要引号；SKU 无特殊字符故不加引号。
        self.assertEqual(raw, "sku,name,quantity\n".encode("utf-8")
                         + 'ONLY,"第一行\n第二行",0\n'.encode("utf-8"))


class TestLowStockInvalidFormat(LowStockCsvTestCase):
    """非法 --format 在访问数据库前以退出码 2 拒绝。"""

    def assert_format_error(self, code, out_text, err_text):
        self.assertEqual(code, 2)
        self.assertEqual(out_text, "")
        self.assertIn("--format", err_text)
        self.assertNotIn("Traceback", err_text, "参数拒绝不应输出异常堆栈")

    def test_invalid_forms_rejected_before_database_access(self):
        self.seed_demo()
        bad_values = (
            "",                    # 空字符串
            "CSV", "Json", "JSON",  # 大小写变体
            " csv", "csv ", " csv ",  # 两端空白
            "\tcsv", "csv\t", "csv\n", "csv\r",
            "yaml", "text", "csv5", "json-csv",  # 其他取值
        )
        missing = str(Path(self._tmp.name) / "never-created.db")
        for bad in bad_values:
            with self.subTest(format=bad.encode("unicode_escape").decode()):
                code, out, err = self.run_cli(
                    "low-stock", "--threshold", "5", "--format", bad,
                    db_path=missing,
                )
                self.assert_format_error(code, out, err)
                self.assertFalse(
                    Path(missing).exists(), "格式非法时不应创建台账文件"
                )
                # 拒绝不改变已有数据：阈值 5 的 JSON 结果仍是前两项。
                self.assertEqual(
                    [p["sku"] for p in self.run_json_ok(
                        "low-stock", "--threshold", "5"
                    )["products"]],
                    ["DEMO-1", "DEMO-2"],
                )

    def test_missing_format_value_rejected(self):
        code, out, err = self.run_cli(
            "low-stock", "--threshold", "5", "--format"
        )
        self.assert_format_error(code, out, err)

    def test_invalid_format_with_invalid_threshold_still_argument_error(self):
        # 两个参数都非法时仍是退出码 2（参数错误），不可能落到数据库错误。
        missing = str(Path(self._tmp.name) / "nope.db")
        code, out, err = self.run_cli(
            "low-stock", "--threshold", "not-a-number", "--format", "yaml",
            db_path=missing,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(Path(missing).exists())

    def test_valid_format_does_not_relax_threshold_validation(self):
        # 阈值非法时即便格式合法，仍按 --threshold 参数错误拒绝且不建库。
        missing = str(Path(self._tmp.name) / "nope2.db")
        code, out, err = self.run_cli(
            "low-stock", "--threshold", " 5", "--format", "csv",
            db_path=missing,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--threshold", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(Path(missing).exists())


class TestLowStockCsvDatabaseError(LowStockCsvTestCase):
    """格式与阈值合法但数据库无法打开：退出码 1、stdout 为空。"""

    def test_nonexistent_parent_directory_is_runtime_error(self):
        bad_db = str(Path(self._tmp.name) / "no-dir" / "ledger.db")
        for fmt in ("csv", "json"):
            with self.subTest(format=fmt):
                code, raw, err = self.run_cli_bytes(
                    "low-stock", "--threshold", "5", "--format", fmt,
                    db_path=bad_db,
                )
                self.assertEqual(code, 1)
                self.assertEqual(raw, b"")
                self.assertNotIn(b"Traceback", err)
                self.assertTrue(err.strip())


class TestLowStockCsvReadonly(LowStockCsvTestCase):
    """CSV 导出只读：商品、库存、流水不变，格式与阈值不保存。"""

    def test_export_changes_nothing_and_is_repeatable(self):
        self.seed_demo()
        _, raw1 = self.csv_text(5)
        _, raw2 = self.csv_text(5)
        self.assertEqual(raw1, raw2)

        # show 仍能看到完整流水与当前数量。
        page = self.run_json_ok("show", "--sku", "DEMO-3")
        self.assertEqual(page["quantity"], 6)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in page["movements"]],
            [("receive", 6, 6)],
        )
        # 格式不保存：不带 --format 的同阈值查询仍是 JSON 对象。
        code, out, err = self.run_cli("low-stock", "--threshold", "5")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["products"][0]["sku"], "DEMO-1")
        # 阈值不保存：换阈值 0 只返回零库存项。
        self.assertEqual(
            [p["sku"] for p in self.run_json_ok(
                "low-stock", "--threshold", "0"
            )["products"]],
            ["DEMO-1"],
        )


if __name__ == "__main__":
    unittest.main()
