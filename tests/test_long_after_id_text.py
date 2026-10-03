"""超长 --after-id 文本（数千个前导零）的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证：

- 由 ASCII 数字组成的 --after-id 文本按实际数值判断，不因文本长度被拒绝：
  5000 个前导零后接 1 与普通写法 1 完全等价，5000 个 0 本身等价于 0，
  前导零后恰好为 64 位上界 9223372036854775807 时查询成功；
- 5000 个 9、任意数量前导零后接 9223372036854775808 按超出上界拒绝
  （退出码 2、stdout 为空、stderr 含 --after-id、允许上限与越界原因、
  无异常堆栈）；
- 缺值、空字符串、负数、小数、正号、含字母的输入继续以退出码 2 拒绝；
- 参数非法时即使 SKU 不存在也先报告参数错误，且不创建尚不存在的数据库；
- 长文本下界合法但 SKU 为空或不存在时，仍以退出码 2 报告商品错误；
- 查询与参数拒绝均不改动商品或流水，重复查询结果一致；
- 短参数与组合筛选的既有行为保持不变。

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

LONG_ONE = LEADING_ZEROS + "1"
LONG_ZERO = LEADING_ZEROS
LONG_MAX = LEADING_ZEROS + MAX_ID_TEXT
LONG_OVER_MAX = LEADING_ZEROS + OVER_MAX_ID_TEXT
LONG_NINES = "9" * 5000


class InventoryCLITestCase(unittest.TestCase):
    """每个用例使用独立的临时数据库，互不依赖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")

    def run_cli(self, *args, db_path=None):
        """运行一条 CLI 命令，返回 (退出码, stdout 文本, stderr 文本)。"""
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
        """运行应成功的命令，返回解析后的 JSON 对象。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"命令 {args[:4]}... 应成功，stderr: {err}")
        payload = json.loads(out)  # stdout 必须是单个可解析的 JSON 对象
        self.assertIsInstance(payload, dict)
        return payload

    def run_rejected(self, *args):
        """运行应被参数/业务规则拒绝的命令，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args[:4]}... 应以退出码 2 拒绝")
        self.assertEqual(out, "", "被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), "被拒绝时 stderr 应说明原因")
        self.assertNotIn("Traceback", err, "业务拒绝不应输出异常堆栈")
        return err

    def seed_demo(self):
        """登记 DEMO-1（演示螺母），入库 10、出库 3，流水编号为 1、2。"""
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

    def show(self, after_id=..., mtype=None, limit=None, sku=SKU):
        args = ["show", "--sku", sku]
        if after_id is not ...:
            args += ["--after-id", str(after_id)]
        if mtype is not None:
            args += ["--type", mtype]
        if limit is not None:
            args += ["--limit", str(limit)]
        return self.run_ok(*args)


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestLongAfterIdAccepted(InventoryCLITestCase):
    """合法超长文本：按实际数值参与筛选，与短写法等价。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()
        # 基准：两条原始流水，编号 1、2，余额 10、7。
        self.full = self.show()
        self.assertEqual(
            movement_tuples(self.full["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_long_leading_zeros_then_one_with_type_and_limit(self):
        # 主场景：5000 个 0 后接 1，与 --after-id 1 等价；
        # 再叠加 --type issue 与 --limit 1，只剩编号 2 的出库流水。
        page = self.show(after_id=LONG_ONE, mtype="issue", limit=1)
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
        )
        # 与短写法 1 的查询结果逐字段一致。
        self.assertEqual(
            page, self.show(after_id="1", mtype="issue", limit=1)
        )

    def test_long_leading_zeros_then_one_excludes_first_movement(self):
        # 仅用长下界：严格大于 1，排除编号 1，保留编号 2。
        page = self.show(after_id=LONG_ONE)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
        )
        self.assertEqual(page, self.show(after_id=1))

    def test_long_all_zeros_equals_zero(self):
        # 5000 个 0 与下界 0 等价：从最早流水开始，返回全部流水。
        page = self.show(after_id=LONG_ZERO)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(page, self.show(after_id=0))
        self.assertEqual(page, self.full)

    def test_long_leading_zeros_then_max_succeeds(self):
        # 前导零后恰好等于 64 位上界：合法，上界之后没有流水，
        # 商品信息照常返回，movements 为空数组。
        page = self.show(after_id=LONG_MAX)
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(page["movements"], [])
        self.assertEqual(page, self.show(after_id=MAX_ID_TEXT))

    def test_queries_do_not_mutate_and_repeatable(self):
        # 成功查询不改动商品或流水，重复查询结果一致。
        first = self.show(after_id=LONG_ONE, mtype="issue", limit=1)
        second = self.show(after_id=LONG_ONE, mtype="issue", limit=1)
        self.assertEqual(first, second)
        self.assertEqual(self.show(after_id=LONG_ZERO), self.full)
        self.assertEqual(self.show(), self.full)


class TestLongAfterIdRejected(InventoryCLITestCase):
    """越界超长文本：确定的参数错误，退出码 2，stderr 含参数名、上限与原因。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def assert_range_error(self, err):
        """越界拒绝的 stderr 必须包含参数名、允许上限与越界原因。"""
        self.assertIn("--after-id", err)
        self.assertIn(MAX_ID_TEXT, err)
        self.assertIn("不能超过", err)

    def test_five_thousand_nines_exceeds_max(self):
        err = self.run_rejected("show", "--sku", SKU, "--after-id", LONG_NINES)
        self.assert_range_error(err)
        # 拒绝不改动任何数据。
        self.assertEqual(self.show()["quantity"], 7)
        self.assertEqual(
            movement_tuples(self.show()["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_leading_zeros_then_over_max_exceeds_max(self):
        err = self.run_rejected(
            "show", "--sku", SKU, "--after-id", LONG_OVER_MAX
        )
        self.assert_range_error(err)
        self.assertEqual(self.show()["quantity"], 7)

    def test_out_of_range_with_type_and_limit_still_rejected(self):
        # 与其他筛选同用时参数错误优先，不进行查询。
        err = self.run_rejected(
            "show", "--sku", SKU, "--type", "issue", "--limit", "1",
            "--after-id", LONG_NINES,
        )
        self.assert_range_error(err)

    def test_out_of_range_with_missing_sku_reported_first(self):
        # 即使 SKU 不存在，仍先报告 --after-id 参数错误。
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--after-id", LONG_NINES
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assert_range_error(err)
        self.assertNotIn("商品不存在", err)
        self.assertNotIn("Traceback", err)

    def test_rejection_does_not_create_database_file(self):
        # 数据库文件尚不存在时，参数拒绝发生在打开数据库之前，不得创建文件。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in (LONG_NINES, LONG_OVER_MAX, LONG_ZERO[:-1] + "x", "abc"):
            with self.subTest(after_id=bad[:8] + "..."):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU, "--after-id", bad, db_path=missing_db
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--after-id", err)
                self.assertNotIn("Traceback", err)
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )

    def test_short_invalid_forms_still_rejected(self):
        # 缺值以外的非法短文本：stderr 含参数名与原因。
        for bad in (
            "",        # 空字符串
            "-1",      # 负数
            "1.5",     # 小数
            "+1",      # 带正号
            "abc",     # 含字母
            "0x1",     # 非十进制
            " 1",      # 含空白
            OVER_MAX_ID_TEXT,  # 超出 64 位上界
        ):
            with self.subTest(after_id=bad):
                err = self.run_rejected(
                    "show", "--sku", SKU, "--after-id", bad
                )
                self.assertIn("--after-id", err)
        # 拒绝后数据不变。
        self.assertEqual(self.show()["quantity"], 7)

    def test_missing_after_id_value(self):
        # --after-id 缺值：argparse 以退出码 2 拒绝，stdout 为空。
        code, out, err = self.run_cli("show", "--sku", SKU, "--after-id")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--after-id", err)
        self.assertNotIn("Traceback", err)

    def test_missing_value_does_not_create_database_file(self):
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        code, out, err = self.run_cli(
            "show", "--sku", SKU, "--after-id", db_path=missing_db
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(Path(missing_db).exists())


class TestValidLongAfterIdProductErrors(InventoryCLITestCase):
    """长下界合法但商品有问题：维持既有的商品错误语义（退出码 2）。"""

    def test_valid_long_bound_with_missing_sku_reports_missing_product(self):
        # 数据库存在但 SKU 不存在：先通过参数校验，再报商品不存在。
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--after-id", LONG_ONE
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("商品不存在", err)
        self.assertIn(MISSING_SKU, err)
        self.assertNotIn("Traceback", err)

    def test_valid_long_bound_with_blank_sku_reports_sku_error(self):
        # SKU 去掉两端空白后为空：报 SKU 错误，stdout 为空。
        code, out, err = self.run_cli(
            "show", "--sku", "   ", "--after-id", LONG_MAX
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("SKU", err)
        self.assertNotIn("Traceback", err)

    def test_valid_long_bound_with_missing_sku_does_not_create_file(self):
        # 商品错误发生在数据库初始化之后：文件允许已存在，
        # 但其中不应有任何商品数据。
        missing_db = str(Path(self._tmp.name) / "other.db")
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--after-id", LONG_ONE,
            db_path=missing_db,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("商品不存在", err)
        # 文件即便已由初始化创建，也不含该商品。
        code2, out2, err2 = self.run_cli(
            "show", "--sku", MISSING_SKU, db_path=missing_db
        )
        self.assertEqual(code2, 2)
        self.assertEqual(out2, "")
        self.assertIn("商品不存在", err2)


class TestShortFormsAndCombinationsUnchanged(InventoryCLITestCase):
    """短参数与组合筛选的既有行为在修复后保持不变。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()

    def test_short_after_id_forms(self):
        # 普通整数、少量前导零、全零短文本的含义不变。
        self.assertEqual(
            movement_tuples(self.show(after_id=0)["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        self.assertEqual(
            movement_tuples(self.show(after_id="000")["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        self.assertEqual(
            movement_tuples(self.show(after_id="0001")["movements"]),
            [(2, "issue", 3, 7)],
        )
        self.assertEqual(self.show(after_id=MAX_ID_TEXT)["movements"], [])

    def test_combined_type_and_limit_with_short_bound(self):
        # 短下界与 --type/--limit 组合：交集后截断，结果不变。
        page = self.show(after_id=1, mtype="issue", limit=1)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
        )
        # 仅类型筛选：两条中只剩 issue。
        self.assertEqual(
            movement_tuples(self.show(mtype="receive")["movements"]),
            [(1, "receive", 10, 10)],
        )
        # 仅条数限制：按 id 升序保留第一条。
        self.assertEqual(
            movement_tuples(self.show(limit=1)["movements"]),
            [(1, "receive", 10, 10)],
        )


if __name__ == "__main__":
    unittest.main()
