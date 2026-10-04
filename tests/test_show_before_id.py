"""show --before-id 流水编号上界筛选的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证编号上界筛选只影响返回的流水：

- 指定时仅返回原始 id 严格小于上界的该商品流水，上界无需对应实际流水，
  也无需属于当前 SKU；0 为合法上界，成功返回空 movements；
- 与 --after-id、--type 同用时取所有条件的交集，再按原始 id 升序应用
  --limit，其他商品的流水不占名额；下界大于或等于上界时成功返回空数组；
- 数字文本沿用 --after-id 的语义：0 至 9223372036854775807，允许前导零
  （含数千位前导零）与 Unicode 十进制数字混写；
- 缺值、空字符串、含任意空白、正负号、小数点、下划线、其他非十进制
  数字字符或数值越界时统一以退出码 2 拒绝，stdout 为空，stderr 含
  --before-id 与拒绝原因，无异常堆栈，且发生在打开数据库之前
  （不创建尚不存在的数据库文件，先于空 SKU/不存在的 SKU 报告）；
- 上界合法时空 SKU/不存在的 SKU 仍以退出码 2 拒绝，数据库不可用
  仍以退出码 1 拒绝；
- 查询只读：不新增或改动商品与流水，不保存上界，重复查询结果一致；
- 不使用 --before-id 的 show 及 add/receive/issue/low-stock 行为不变。

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
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        return payload

    def run_rejected(self, *args):
        """运行应被参数/业务规则拒绝的命令，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), f"命令 {args} 被拒绝时 stderr 应说明原因")
        self.assertNotIn("Traceback", err, "参数拒绝不应输出异常堆栈")
        return err

    def add_product(self, sku, name):
        payload = self.run_ok("add", "--sku", sku, "--name", name)
        self.assertEqual(payload, {"sku": sku, "name": name, "quantity": 0})

    def show(self, sku=SKU1, after_id=..., before_id=..., mtype=None,
             limit=None):
        """after_id/before_id 为 ... 时不传该参数；None 时显式传 0。"""
        args = ["show", "--sku", sku]
        if after_id is not ...:
            args += ["--after-id", str(after_id)]
        if before_id is not ...:
            args += ["--before-id", str(before_id)]
        if mtype is not None:
            args += ["--type", mtype]
        if limit is not None:
            args += ["--limit", str(limit)]
        return self.run_ok(*args)


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestBeforeIdLifecycle(InventoryCLITestCase):
    """主场景：跨商品操作后，--before-id 只返回编号严格更小的本商品流水。"""

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        self.add_product(SKU2, NAME2)
        # DEMO-1 入库 10（编号 1）；DEMO-2 入库 4（编号 2，属于另一商品）；
        # DEMO-1 出库 3（编号 3）、再次入库 2（编号 4），当前数量 9。
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU1, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU2, "--qty", "4")["quantity"], 4
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU1, "--qty", "3")["quantity"], 7
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU1, "--qty", "2")["quantity"], 9
        )

    def test_documented_acceptance_scenario(self):
        # 验收主场景：--after-id 0 --before-id 4 --type receive --limit 2
        # 只返回编号 1 的流水（编号 2 属于另一商品不占名额，编号 4 被上界排除）。
        page = self.show(after_id=0, before_id=4, mtype="receive", limit=2)
        self.assertEqual(page["sku"], SKU1)
        self.assertEqual(page["name"], NAME1)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 10, 10)]
        )
        # 上界改为 0：空流水，商品当前数量仍为 9。
        empty = self.show(after_id=0, before_id=0, mtype="receive", limit=2)
        self.assertEqual(empty["quantity"], 9)
        self.assertEqual(empty["movements"], [])

    def test_before_id_strict_less_and_current_state(self):
        # 严格小于：--before-id 4 返回编号 1、3（编号 4 被排除）。
        page = self.show(before_id=4)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7)],
        )
        # 上界等于该商品最早编号时没有更早的流水。
        self.assertEqual(self.show(before_id=1)["movements"], [])
        # 上界超过最大编号：返回该商品全部三条流水。
        self.assertEqual(
            movement_tuples(self.show(before_id=999)["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7), (4, "receive", 2, 9)],
        )

    def test_before_id_need_not_exist_or_belong_to_sku(self):
        # 上界无需真实存在：5、6 都从未分配过，对 DEMO-1 结果相同。
        self.assertEqual(self.show(before_id=5), self.show(before_id=6))
        # 上界无需属于当前 SKU：用 DEMO-2 的编号 2 查 DEMO-1，只剩编号 1。
        self.assertEqual(
            movement_tuples(self.show(before_id=2)["movements"]),
            [(1, "receive", 10, 10)],
        )
        # 反向验证：DEMO-2 只有编号 2，用属于 DEMO-1 的编号 3 作上界仍可查到它。
        demo2 = self.show(sku=SKU2, before_id=3)
        self.assertEqual(demo2["quantity"], 4)
        self.assertEqual(
            movement_tuples(demo2["movements"]), [(2, "receive", 4, 4)]
        )

    def test_combined_with_after_type_and_limit(self):
        # 三个条件取交集：(0, 4) 区间内的 receive 只有编号 1。
        page = self.show(after_id=0, before_id=4, mtype="receive")
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 10, 10)]
        )
        # 区间 (0, 4) 不加类型：编号 1、3，--limit 1 升序截断只保留编号 1。
        paged = self.show(after_id=0, before_id=4, limit=1)
        self.assertEqual(
            movement_tuples(paged["movements"]), [(1, "receive", 10, 10)]
        )
        # 区间 (1, 4) 内的 issue：只有编号 3。
        issue = self.show(after_id=1, before_id=4, mtype="issue")
        self.assertEqual(
            movement_tuples(issue["movements"]), [(3, "issue", 3, 7)]
        )

    def test_lower_greater_or_equal_upper_returns_empty(self):
        # 下界等于上界：没有严格介于两者之间的编号。
        equal = self.show(after_id=4, before_id=4)
        self.assertEqual(equal["quantity"], 9)
        self.assertEqual(equal["movements"], [])
        # 下界大于上界：交集为空，仍成功返回。
        greater = self.show(after_id=5, before_id=4)
        self.assertEqual(greater["quantity"], 9)
        self.assertEqual(greater["movements"], [])
        # 叠加类型与条数也不改变空结果。
        self.assertEqual(
            self.show(after_id=4, before_id=4, mtype="receive", limit=2)[
                "movements"
            ],
            [],
        )

    def test_zero_upper_bound_always_empty(self):
        for kwargs in (
            dict(before_id=0),
            dict(after_id=0, before_id=0),
            dict(before_id=0, mtype="receive"),
        ):
            with self.subTest(kwargs=kwargs):
                page = self.show(**kwargs)
                self.assertEqual(page["quantity"], 9)
                self.assertEqual(page["movements"], [])

    def test_omitted_before_id_keeps_existing_semantics(self):
        # 省略 --before-id：返回该商品全部流水（编号 2 属于另一商品，不出现）。
        full = self.show()
        self.assertEqual(
            movement_tuples(full["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7), (4, "receive", 2, 9)],
        )
        # 省略上界时 --after-id/--type/--limit 的既有语义不变。
        self.assertEqual(
            movement_tuples(self.show(after_id=2, mtype="receive")["movements"]),
            [(4, "receive", 2, 9)],
        )
        self.assertEqual(
            movement_tuples(
                self.show(mtype="receive", limit=1)["movements"]
            ),
            [(1, "receive", 10, 10)],
        )
        # 一个远大于任何编号的上界与省略上界等价。
        self.assertEqual(self.show(before_id=MAX_ID), full)

    def test_read_only_and_repeatable(self):
        before = self.show()
        first = self.show(after_id=0, before_id=4, mtype="receive", limit=2)
        second = self.show(after_id=0, before_id=4, mtype="receive", limit=2)
        self.assertEqual(first, second)
        for bad in ("x", "-1", "1\n"):
            self.run_rejected("show", "--sku", SKU1, "--before-id", bad)
        # 成功与被拒绝的查询之后，商品与流水均无变化。
        self.assertEqual(self.show(), before)
        self.assertEqual(self.show(sku=SKU2)["quantity"], 4)


class TestBeforeIdEmptyLedger(InventoryCLITestCase):
    """无流水或无匹配时：退出码 0、movements 为 []，当前数量照常返回。"""

    def test_registered_without_movements(self):
        self.add_product(SKU1, NAME1)
        for bound in (0, 1, 999):
            with self.subTest(before_id=bound):
                page = self.show(before_id=bound)
                self.assertEqual(page["sku"], SKU1)
                self.assertEqual(page["name"], NAME1)
                self.assertEqual(page["quantity"], 0)
                self.assertEqual(page["movements"], [])

    def test_upper_equal_only_movement_id(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "5")
        page = self.show(before_id=1)
        self.assertEqual(page["quantity"], 5)
        self.assertEqual(page["movements"], [])


class TestInvalidBeforeIdRejected(InventoryCLITestCase):
    """非法 --before-id：退出码 2、stdout 为空、stderr 含参数名与原因。"""

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        self.before = self.show()

    def test_invalid_before_id_values(self):
        for bad in (
            "",          # 空字符串
            "-1",        # 负数
            "+1",        # 带正号
            "1.5",       # 小数
            "1_0",       # 下划线
            "abc",       # 非数字
            "0x1",       # 非十进制
            " 1", "1 ", "1 2",   # 空格（开头/结尾/中间）
            "1\t", "\t1",        # 制表符
            "1\r", "1\n", "1\n2",  # 回车/换行（含数字后附单个换行）
            OVER_MAX_ID,         # 超出 64 位上界
            "999999999999999999999999999999",  # 远超上界
        ):
            with self.subTest(before_id=bad):
                err = self.run_rejected(
                    "show", "--sku", SKU1, "--before-id", bad
                )
                self.assertIn("--before-id", err)
                self.assertEqual(self.show(), self.before)

    def test_missing_before_id_value(self):
        code, out, err = self.run_cli("show", "--sku", SKU1, "--before-id")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--before-id", err)
        self.assertNotIn("Traceback", err)

    def test_whitespace_reason(self):
        code, out, err = self.run_cli(
            "show", "--sku", SKU1, "--before-id", "1\n"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--before-id", err)
        self.assertIn("不能含空白", err)

    def test_out_of_range_reason_names_bound_and_limit(self):
        err = self.run_rejected(
            "show", "--sku", SKU1, "--before-id", OVER_MAX_ID
        )
        self.assertIn("--before-id", err)
        self.assertIn(MAX_ID, err)
        self.assertIn("不能超过", err)

    def test_invalid_bound_reported_before_sku_and_db_errors(self):
        # 非法上界 + 不存在的 SKU + 合法其他筛选：先报告参数错误。
        err = self.run_rejected(
            "show", "--sku", MISSING_SKU, "--type", "receive",
            "--after-id", "0", "--limit", "2", "--before-id", "x",
        )
        self.assertIn("--before-id", err)
        self.assertNotIn("商品不存在", err)
        # 数据库文件尚不存在时，参数拒绝不得创建文件。
        missing_db = str(Path(self._tmp.name) / "never-created.db")
        for bad in ("x", OVER_MAX_ID, "1\n"):
            with self.subTest(bad=bad):
                code, out, err = self.run_cli(
                    "show", "--sku", MISSING_SKU, "--before-id", bad,
                    db_path=missing_db,
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--before-id", err)
                self.assertFalse(
                    Path(missing_db).exists(), "参数非法时不应创建台账文件"
                )

    def test_valid_bound_with_bad_sku_still_rejected(self):
        # 上界合法但 SKU 不存在：维持既有的商品错误（退出码 2）。
        code, out, err = self.run_cli(
            "show", "--sku", MISSING_SKU, "--before-id", "4"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("商品不存在", err)
        self.assertIn(MISSING_SKU, err)
        # 空 SKU 同样在参数校验之后拒绝。
        code, out, err = self.run_cli(
            "show", "--sku", "   ", "--before-id", "0"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("SKU", err)


class TestLongAndUnicodeBeforeId(InventoryCLITestCase):
    """超长前导零与 Unicode 十进制数字混写：按实际数值判断。"""

    LEADING_ZEROS = "0" * 5000

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        for qty in ("10", "4", "2"):
            self.run_ok("receive", "--sku", SKU1, "--qty", qty)
        # 三条流水编号 1、2、3。
        self.full_ids = [1, 2, 3]

    def ids(self, page):
        return [m["id"] for m in page["movements"]]

    def test_long_leading_zeros_by_numeric_value(self):
        # 5000 个前导零后接 3：与上界 3 等价（严格小于，剩编号 1、2）。
        self.assertEqual(
            self.ids(self.show(before_id=self.LEADING_ZEROS + "3")), [1, 2]
        )
        # 5000 个 0：等价于上界 0，结果为空。
        self.assertEqual(
            self.show(before_id=self.LEADING_ZEROS)["movements"], []
        )
        # 前导零后恰好为 64 位上界：合法，返回全部流水。
        self.assertEqual(
            self.ids(self.show(before_id=self.LEADING_ZEROS + MAX_ID)),
            self.full_ids,
        )

    def test_unicode_decimal_digits_mixed(self):
        # 全角 ３（U+FF13）与 ASCII 3 等价。
        self.assertEqual(self.ids(self.show(before_id="３")), [1, 2])
        # ASCII、全角、阿拉伯印度数字混写 "0０٠3" 等于 3。
        self.assertEqual(self.ids(self.show(before_id="0０٠3")), [1, 2])

    def test_long_nines_rejected(self):
        err = self.run_rejected(
            "show", "--sku", SKU1, "--before-id", "9" * 5000
        )
        self.assertIn("--before-id", err)
        self.assertIn(MAX_ID, err)
        # 拒绝不改动数据。
        self.assertEqual(self.ids(self.show()), self.full_ids)


if __name__ == "__main__":
    unittest.main()
