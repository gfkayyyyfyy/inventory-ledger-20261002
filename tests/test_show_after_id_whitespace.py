"""show --after-id 含空白文本的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证 --after-id 校验完整参数值、不先去空白：

- 数字后附单个实际换行（U+000A）的参数值按参数错误拒绝，不再被当作
  对应下界接受；含空格、制表符、回车、换行的值，无论空白位于开头、
  中间还是结尾，都以退出码 2 拒绝，stdout 为空，stderr 含 --after-id
  与不能含空白的原因，不输出异常堆栈；
- 全零文本后附换行、数千个前导零后附换行，与短文本后附换行同样拒绝；
- 拒绝时已有商品名称、库存数量和流水保持不变，再次完整查询结果一致；
- 指定数据库文件尚不存在时，参数拒绝不创建该文件；
- 即使 SKU 为空或不存在，也先报告下界参数错误；与合法的 --type、
  --limit 同用时，空白下界仍被拒绝；
- 合法下界 0、0001、9223372036854775807、5000 个前导零后接 1 的
  既有语义保持不变。

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

# 远超 Python 3.11+ 默认整数字符串转换位数上限（4300）的前导零个数。
LEADING_ZEROS = "0" * 5000
LONG_ONE = LEADING_ZEROS + "1"

# 含空白的非法下界：空白分别位于结尾、开头与中间，覆盖空格、制表符、
# 回车与换行；以及全零文本、数千个前导零文本后附单个实际换行。
WHITESPACE_AFTER_IDS = (
    "1\n",                 # 数字后附单个实际换行（U+000A）
    "1\r",                 # 数字后附回车
    "1 ",                  # 数字后附空格
    "1\t",                 # 数字后附制表符
    " 1",                  # 开头空格
    "\t1",                 # 开头制表符
    "\n1",                 # 开头换行
    "1 2",                 # 中间空格
    "1\t2",                # 中间制表符
    "1\n2",                # 中间换行
    "0\n",                 # 全零文本后附换行
    "0000\n",              # 多个零后附换行
    LONG_ONE + "\n",       # 5000 个前导零后接 1 再附换行
    LEADING_ZEROS + "\n",  # 5000 个零后附换行
)


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

    def assert_whitespace_rejected(self, *args, db_path=None):
        """空白下界应被拒绝：退出码 2、stdout 为空、stderr 含参数名与原因。"""
        code, out, err = self.run_cli(*args, db_path=db_path)
        self.assertEqual(code, 2, f"命令 {args[:4]}... 应以退出码 2 拒绝")
        self.assertEqual(out, "", "被拒绝时 stdout 应为空")
        self.assertIn("--after-id", err)
        self.assertIn("空白", err)
        self.assertNotIn("Traceback", err)
        return err


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestAfterIdTrailingNewlineRejected(InventoryCLITestCase):
    """主场景：数字 1 后附实际换行不再被当作下界 1 接受。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()
        # 基准：两条原始流水，编号 1、2，余额 10、7，当前数量 7。
        self.full = self.show()
        self.assertEqual(self.full["quantity"], 7)
        self.assertEqual(
            movement_tuples(self.full["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

    def test_legal_bound_one_returns_only_second_movement(self):
        # 合法下界 1：只返回编号 2、类型 issue、数量 3、余额 7 的流水。
        page = self.show(after_id=1)
        self.assertEqual(page["sku"], SKU)
        self.assertEqual(page["name"], NAME)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(
            movement_tuples(page["movements"]), [(2, "issue", 3, 7)]
        )

    def test_trailing_newline_rejected_and_data_unchanged(self):
        # 下界改为数字 1 后附实际换行：参数错误，不再查询成功。
        self.assert_whitespace_rejected(
            "show", "--sku", SKU, "--after-id", "1\n"
        )
        # 拒绝后商品数量与流水保持不变，再次完整查询仍返回原来的两条流水。
        again = self.show()
        self.assertEqual(again, self.full)
        self.assertEqual(
            movement_tuples(again["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )
        # 合法下界查询不受此前的拒绝影响。
        self.assertEqual(
            movement_tuples(self.show(after_id=1)["movements"]),
            [(2, "issue", 3, 7)],
        )

    def test_all_whitespace_forms_rejected(self):
        for bad in WHITESPACE_AFTER_IDS:
            with self.subTest(after_id=repr(bad[:16])):
                self.assert_whitespace_rejected(
                    "show", "--sku", SKU, "--after-id", bad
                )
                # 每次拒绝后数据保持不变。
                self.assertEqual(self.show(), self.full)

    def test_whitespace_bound_with_type_and_limit_still_rejected(self):
        # 与合法的 --type、--limit 同用时，空白下界仍按同一约定拒绝。
        self.assert_whitespace_rejected(
            "show", "--sku", SKU, "--type", "issue", "--limit", "1",
            "--after-id", "1\n",
        )
        self.assertEqual(self.show(), self.full)

    def test_whitespace_bound_reported_before_sku_errors(self):
        # SKU 不存在：仍先报告下界参数错误，而不是商品错误。
        err = self.assert_whitespace_rejected(
            "show", "--sku", MISSING_SKU, "--after-id", "1\n"
        )
        self.assertNotIn("商品不存在", err)
        # SKU 去掉两端空白后为空：同样先报告下界参数错误。
        err = self.assert_whitespace_rejected(
            "show", "--sku", "   ", "--after-id", "1\n"
        )
        self.assertNotIn("SKU 去掉两端空白后不能为空", err)

    def test_whitespace_bound_does_not_create_database_file(self):
        # 数据库文件尚不存在时，参数拒绝发生在打开数据库之前，不得创建文件。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("1\n", "0\n", LONG_ONE + "\n"):
            with self.subTest(after_id=repr(bad[:16])):
                self.assert_whitespace_rejected(
                    "show", "--sku", SKU, "--after-id", bad, db_path=missing_db
                )
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )


class TestLegalBoundsUnchanged(InventoryCLITestCase):
    """合法下界的既有语义在修复后保持不变。"""

    def setUp(self):
        super().setUp()
        self.seed_demo()
        self.full = self.show()

    def test_zero_and_leading_zero_bounds(self):
        # 0 与 0001 等既有合法写法含义不变。
        self.assertEqual(self.show(after_id=0), self.full)
        self.assertEqual(
            movement_tuples(self.show(after_id="0001")["movements"]),
            [(2, "issue", 3, 7)],
        )

    def test_max_and_long_leading_zeros_bounds(self):
        # 64 位上界合法，上界之后没有流水。
        edge = self.show(after_id=MAX_ID_TEXT)
        self.assertEqual(edge["quantity"], 7)
        self.assertEqual(edge["movements"], [])
        # 5000 个前导零后接 1 与普通写法 1 完全等价。
        self.assertEqual(self.show(after_id=LONG_ONE), self.show(after_id=1))
        self.assertEqual(
            movement_tuples(self.show(after_id=LONG_ONE)["movements"]),
            [(2, "issue", 3, 7)],
        )
        # 5000 个 0 本身等价于 0。
        self.assertEqual(self.show(after_id=LEADING_ZEROS), self.full)


if __name__ == "__main__":
    unittest.main()
