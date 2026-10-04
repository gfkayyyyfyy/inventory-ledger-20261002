"""show --before-id 流水编号上界筛选的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证编号上界筛选只影响返回的流水：

- 仅返回该 SKU 中原始 id 严格小于上界的流水，上界无需真实存在，
  也无需属于当前 SKU；传入 0 时成功返回空 movements；
- 与 --after-id、--type 同用时取所有条件的交集，再按原始 id 升序
  应用 --limit，其他商品的流水不占名额；下界大于或等于上界时成功
  返回空数组；
- 数字文本沿用 --after-id 的语义（0 至 9223372036854775807 的十进制
  非负整数，允许前导零与 Unicode 十进制数字混写，数千位前导零仍按
  实际数值判断）；缺值、空字符串、任意空白、正负号、小数点、下划线、
  其他非十进制数字字符或越界时以退出码 2 拒绝，stdout 为空，stderr
  包含 --before-id 与拒绝原因，不出现异常堆栈，且校验发生在访问
  数据库之前（不创建尚不存在的台账文件，先于空/不存在的 SKU 报告）；
- 查询为只读：不改变商品当前数量，不新增、删除或改写流水，不保存上界。

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

SKU1 = "DEMO-1"
NAME1 = "演示螺母"
SKU2 = "DEMO-2"
NAME2 = "演示螺栓"
MISSING_SKU = "NOT-EXIST"

MAX_ID = "9223372036854775807"
OVER_MAX_ID = "9223372036854775808"

# 远超 Python 3.11+ 默认整数字符串转换位数上限（4300）的前导零个数。
LEADING_ZEROS = "0" * 5000
LONG_THREE = LEADING_ZEROS + "3"


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
        return err

    def add_product(self, sku, name):
        payload = self.run_ok("add", "--sku", sku, "--name", name)
        self.assertEqual(payload, {"sku": sku, "name": name, "quantity": 0})

    def show(self, sku, before_id=..., after_id=..., mtype=None, limit=...):
        """缺省（...）表示不传对应参数；None 不使用。"""
        args = ["show", "--sku", sku]
        if before_id is not ...:
            args += ["--before-id", str(before_id)]
        if after_id is not ...:
            args += ["--after-id", str(after_id)]
        if mtype is not None:
            args += ["--type", mtype]
        if limit is not ...:
            args += ["--limit", str(limit)]
        return self.run_ok(*args)


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestBeforeIdDemoScenario(InventoryCLITestCase):
    """任务给定的演示台账：DEMO-1 流水编号 1、3、4，编号 2 属于另一商品。"""

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        self.add_product(SKU2, NAME2)
        # DEMO-1 入库 10（编号 1）；DEMO-2 入库 5（编号 2，属于另一商品）；
        # DEMO-1 出库 3（编号 3）、入库 2（编号 4）。
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU1, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU2, "--qty", "5")["quantity"], 5
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU1, "--qty", "3")["quantity"], 7
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU1, "--qty", "2")["quantity"], 9
        )

    def test_full_movements_have_gap_owned_by_other_sku(self):
        # 前置事实：DEMO-1 只有编号 1、3、4，编号 2 属于 DEMO-2。
        page = self.show(SKU1)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7), (4, "receive", 2, 9)],
        )
        other = self.show(SKU2)
        self.assertEqual(
            movement_tuples(other["movements"]), [(2, "receive", 5, 5)]
        )

    def test_acceptance_intersection_with_type_and_limit(self):
        # --after-id 0 --before-id 4 --type receive --limit 2：
        # 交集内只有编号 1（receive 10，余额 10）；编号 4 被严格上界排除，
        # 编号 3 类型不符，编号 2 属于另一商品不参与。
        page = self.show(
            SKU1, before_id=4, after_id=0, mtype="receive", limit=2
        )
        self.assertEqual(page["sku"], SKU1)
        self.assertEqual(page["name"], NAME1)
        self.assertEqual(page["quantity"], 9)  # 当前数量仍是当前状态
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 10, 10)]
        )

    def test_before_zero_returns_empty_but_current_quantity_intact(self):
        # 上界 0：没有 id < 0 的流水，成功返回空数组，当前数量仍为 9。
        page = self.show(SKU1, before_id=0)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(page["movements"], [])
        # 叠加其他合法条件后仍为空。
        self.assertEqual(
            self.show(SKU1, before_id=0, after_id=0, mtype="receive")["movements"],
            [],
        )

    def test_before_id_strict_less_than(self):
        # 严格小于：--before-id 4 返回编号 1、3，编号 4 自身被排除。
        page = self.show(SKU1, before_id=4)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7)],
        )
        # 上界取 3：只剩编号 1；编号 3 自身同样被排除。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id=3)["movements"]], [1]
        )
        # 上界超过现有最大编号：等价于不带上界的全部流水。
        self.assertEqual(
            movement_tuples(self.show(SKU1, before_id=999)["movements"]),
            movement_tuples(self.show(SKU1)["movements"]),
        )
        # 上界无需真实存在：编号 5 从未分配，仍返回 1、3、4。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id=5)["movements"]],
            [1, 3, 4],
        )
        # 64 位有符号整数上界合法，且大于全部现有编号。
        edge = self.show(SKU1, before_id=MAX_ID)
        self.assertEqual(edge["quantity"], 9)
        self.assertEqual(
            [m["id"] for m in edge["movements"]], [1, 3, 4]
        )

    def test_upper_bound_need_not_belong_to_sku(self):
        # 上界无需属于当前 SKU：用 DEMO-2 的编号 2 查 DEMO-1，只得编号 1。
        page = self.show(SKU1, before_id=2)
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 10, 10)]
        )
        # 反向：用属于 DEMO-1 的编号 3 作 DEMO-2 的上界，编号 2 仍返回。
        other = self.show(SKU2, before_id=3)
        self.assertEqual(other["quantity"], 5)
        self.assertEqual(
            movement_tuples(other["movements"]), [(2, "receive", 5, 5)]
        )

    def test_intersection_with_after_id_and_type(self):
        # 区间 (0, 4)：编号 1、3。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id=4, after_id=0)["movements"]],
            [1, 3],
        )
        # 开区间端点严格：(1, 4) 只剩编号 3。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id=4, after_id=1)["movements"]],
            [3],
        )
        # 与类型取交集。
        self.assertEqual(
            movement_tuples(
                self.show(SKU1, before_id=4, after_id=0, mtype="issue")["movements"]
            ),
            [(3, "issue", 3, 7)],
        )

    def test_lower_bound_ge_upper_bound_returns_empty(self):
        # 下界等于上界：开区间为空。
        for same in (1, 3, 4, 999):
            with self.subTest(bound=same):
                page = self.show(SKU1, before_id=same, after_id=same)
                self.assertEqual(page["quantity"], 9)
                self.assertEqual(page["movements"], [])
        # 下界大于上界：同样成功返回空数组（不报参数错误）。
        page = self.show(SKU1, before_id=2, after_id=3)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(page["movements"], [])

    def test_limit_counts_only_filtered_rows_of_same_sku(self):
        # 区间 (0, 5) 内 DEMO-1 有 1、3、4 三条；--limit 2 只取前两条，
        # 编号 2（DEMO-2）不占名额。
        page = self.show(SKU1, before_id=5, after_id=0, limit=2)
        self.assertEqual(
            [m["id"] for m in page["movements"]], [1, 3]
        )
        # limit 为 1 时只剩编号 1。
        self.assertEqual(
            [m["id"] for m in self.show(
                SKU1, before_id=5, after_id=0, limit=1
            )["movements"]],
            [1],
        )

    def test_repeated_queries_consistent_and_read_only(self):
        before = self.show(SKU1)
        first = self.show(SKU1, before_id=4, after_id=0, mtype="receive", limit=2)
        second = self.show(SKU1, before_id=4, after_id=0, mtype="receive", limit=2)
        self.assertEqual(first, second)
        # 一系列筛选查询后台账内容不变（两个商品都是）。
        self.assertEqual(self.show(SKU1), before)
        self.assertEqual(
            movement_tuples(self.show(SKU2)["movements"]),
            [(2, "receive", 5, 5)],
        )


class TestBeforeIdTextForms(InventoryCLITestCase):
    """数字文本语义与 --after-id 一致：前导零、Unicode 数字、长前导零。"""

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU1, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU1, "--qty", "3")["quantity"], 7
        )
        # 两条流水编号 1、2。

    def test_leading_zeros_and_unicode_digits(self):
        # 前导零不改变数值：0002 与 2 等价（严格小于，只剩编号 1）。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id="0002")["movements"]],
            [1],
        )
        # 0001 与 1 等价：没有 id < 1 的流水，空数组。
        self.assertEqual(self.show(SKU1, before_id="0001")["movements"], [])
        # 0003 与 3 等价：返回编号 1、2。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id="0003")["movements"]],
            [1, 2],
        )
        # 全角数字 ３（U+FF13）与 ASCII 3 等价。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id="３")["movements"]],
            [1, 2],
        )
        # 各文字数字混写："0０٠3"（ASCII 0、全角 ０、阿拉伯印度 ٠、3）等于 3。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, before_id="0０٠3")["movements"]],
            [1, 2],
        )
        # 数千位前导零仍按实际数值判断：5000 个 0 后接 3 与 3 等价。
        self.assertEqual(
            self.show(SKU1, before_id=LONG_THREE),
            self.show(SKU1, before_id="3"),
        )
        # 5000 个 0 与上界 0 等价：空数组，当前数量照常返回。
        page = self.show(SKU1, before_id=LEADING_ZEROS)
        self.assertEqual(page["quantity"], 7)
        self.assertEqual(page["movements"], [])


class TestInvalidBeforeIdRejected(InventoryCLITestCase):
    """非法 --before-id：退出码 2、stdout 为空、stderr 含参数名与原因、数据不变。"""

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        self.before = self.show(SKU1)

    def test_invalid_before_id_values(self):
        for bad in (
            "",          # 空字符串
            "-1",        # 负数
            "+1",        # 带正号
            "1.5",       # 小数
            "1.0",       # 小数点
            "1_000",     # 下划线
            "abc",       # 非数字
            "0x1",       # 非十进制
            " 3",        # 含空白（开头）
            "3 ",        # 含空白（结尾）
            "1\n",       # 数字后附换行
            "\n1",       # 开头换行
            "1 2",       # 中间空格
            "3\t",       # 制表符
            "３\n",      # Unicode 数字旁附换行
            "²",         # 上标 2 不是十进制数字字符
            OVER_MAX_ID,  # 超出 64 位上界
            "999999999999999999999999999999",  # 远超上界
        ):
            with self.subTest(before_id=bad):
                err = self.run_rejected(
                    "show", "--sku", SKU1, "--before-id", bad
                )
                self.assertIn("--before-id", err)
                self.assertNotIn("Traceback", err, "参数拒绝不应输出异常堆栈")
                # 每次失败后商品信息与流水都与之前一致。
                self.assertEqual(self.show(SKU1), self.before)

    def test_whitespace_reason_explicit(self):
        code, out, err = self.run_cli(
            "show", "--sku", SKU1, "--before-id", "3\n"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--before-id", err)
        self.assertIn("不能含空白", err)

    def test_missing_before_id_value(self):
        # --before-id 缺值：argparse 以退出码 2 拒绝，stdout 为空。
        code, out, err = self.run_cli("show", "--sku", SKU1, "--before-id")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--before-id", err)
        self.assertNotIn("Traceback", err)

    def test_invalid_with_other_valid_options_still_rejected(self):
        # 非法上界与合法的 --after-id、--type、--limit 同用：参数错误优先。
        code, out, err = self.run_cli(
            "show", "--sku", SKU1,
            "--after-id", "0", "--type", "receive", "--limit", "2",
            "--before-id", "x",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--before-id", err)

    def test_invalid_upper_reported_before_sku_errors(self):
        # 上界错误先于不存在的 SKU 报告。
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--before-id", "1\n"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--before-id", err)
        self.assertNotIn("商品不存在", err)
        # 也先于空 SKU 报告。
        code, out, err = self.run_cli(
            "show", "--sku", "   ", "--before-id", "0\n"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--before-id", err)
        self.assertNotIn("不能为空", err)

    def test_rejection_does_not_create_database_file(self):
        # 参数校验发生在打开数据库之前：文件尚不存在时不得创建。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("1\n", LEADING_ZEROS + "1\n", "000\n", "\t3", "1 2", OVER_MAX_ID):
            with self.subTest(before_id=bad[:8]):
                code, out, err = self.run_cli(
                    "show", "--sku", SKU1, "--before-id", bad, db_path=missing_db
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--before-id", err)
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )

    def test_valid_upper_with_missing_sku_still_exit_2(self):
        # 上界合法时，不存在的 SKU 仍以退出码 2 拒绝。
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--before-id", "3"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("商品不存在", err)

    def test_valid_upper_with_blank_sku_still_exit_2(self):
        code, out, err = self.run_cli(
            "show", "--sku", "   ", "--before-id", "3"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("SKU", err)


class TestBeforeIdWithoutMovements(InventoryCLITestCase):
    """已登记但无流水的商品：任何合法上界都成功返回空数组。"""

    def test_registered_without_movements(self):
        self.add_product(SKU1, NAME1)
        for bound in (0, 1, 999, MAX_ID):
            with self.subTest(before_id=bound):
                page = self.show(SKU1, before_id=bound)
                self.assertEqual(page["sku"], SKU1)
                self.assertEqual(page["name"], NAME1)
                self.assertEqual(page["quantity"], 0)
                self.assertEqual(page["movements"], [])


if __name__ == "__main__":
    unittest.main()
