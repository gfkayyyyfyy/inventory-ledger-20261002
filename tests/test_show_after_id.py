"""show --after-id 流水编号筛选的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证编号下界筛选只影响返回的流水：
不改变商品当前数量，不新增、删除或改写已保存的流水，
不重新编号或重算余额；可与 --type 同时使用（两个条件取交集）。

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

MAX_ID = "9223372036854775807"
OVER_MAX_ID = "9223372036854775808"


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
        """运行应被参数/业务规则拒绝的命令，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), f"命令 {args} 被拒绝时 stderr 应说明原因")
        return err

    def add_product(self, sku, name):
        payload = self.run_ok("add", "--sku", sku, "--name", name)
        self.assertEqual(payload, {"sku": sku, "name": name, "quantity": 0})

    def show(self, sku, after_id=..., mtype=None):
        """after_id 为 None 时显式传 --after-id 0；省略该参数（...）时不传。"""
        args = ["show", "--sku", sku]
        if after_id is not ...:
            args += ["--after-id", str(after_id)]
        if mtype is not None:
            args += ["--type", mtype]
        return self.run_ok(*args)


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestAfterIdLifecycle(InventoryCLITestCase):
    """主场景：跨商品操作后，--after-id 只返回编号严格更大的流水。"""

    def setUp(self):
        super().setUp()
        # 先登记两个演示商品。
        self.add_product(SKU1, NAME1)
        self.add_product(SKU2, NAME2)
        # DEMO-1 入库 10；DEMO-2 入库 4；DEMO-1 出库 3、再次入库 2。
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

    def test_after_id_main_scenario(self):
        # --after-id 2：当前数量 9，仅编号 3、4 两条流水，
        # 分别为出库 3 后余额 7、入库 2 后余额 9。
        page = self.show(SKU1, after_id=2)
        self.assertEqual(page["sku"], SKU1)
        self.assertEqual(page["name"], NAME1)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(3, "issue", 3, 7), (4, "receive", 2, 9)],
        )

    def test_after_id_combined_with_type(self):
        # --after-id 2 --type receive：两个条件取交集，只剩编号 4。
        page = self.show(SKU1, after_id=2, mtype="receive")
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]), [(4, "receive", 2, 9)]
        )
        # 与 issue 取交集：只剩编号 3。
        page_issue = self.show(SKU1, after_id=2, mtype="issue")
        self.assertEqual(page_issue["quantity"], 9)
        self.assertEqual(
            movement_tuples(page_issue["movements"]), [(3, "issue", 3, 7)]
        )
        # 下界与类型组合后无匹配：退出码 0、空数组，当前数量照常返回。
        no_match = self.show(SKU1, after_id=4, mtype="receive")
        self.assertEqual(no_match["quantity"], 9)
        self.assertEqual(no_match["movements"], [])

    def test_after_id_bounds(self):
        # 0 表示从最早流水开始，等价于不带参数的全部流水。
        from_zero = self.show(SKU1, after_id=0)
        full = self.show(SKU1)
        self.assertEqual(from_zero, full)
        self.assertEqual(
            movement_tuples(from_zero["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7), (4, "receive", 2, 9)],
        )
        # 前导零与对应数值等价。
        self.assertEqual(self.show(SKU1, after_id="0002"), self.show(SKU1, 2))
        # 严格大于：--after-id 1 排除编号 1，保留 3、4（编号 2 属于 DEMO-2）。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, after_id=1)["movements"]], [3, 4]
        )
        # 下界无需属于当前 SKU：用 DEMO-2 的编号 2 查 DEMO-1 同样得到 3、4。
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, after_id=2)["movements"]], [3, 4]
        )
        # 下界超过现有最大编号：空数组，当前数量照常返回。
        over = self.show(SKU1, after_id=999)
        self.assertEqual(over["quantity"], 9)
        self.assertEqual(over["movements"], [])
        # 下界无需真实存在：编号 999 从未分配过也合法。
        self.assertEqual(self.show(SKU1, after_id=998), over)
        # 64 位有符号整数上界合法，且一定没有后续流水。
        edge = self.show(SKU1, after_id=MAX_ID)
        self.assertEqual(edge["quantity"], 9)
        self.assertEqual(edge["movements"], [])

    def test_boundary_does_not_belong_to_sku(self):
        # DEMO-2 只有编号 2 一条流水；用编号 1（属于 DEMO-1）作下界仍能查到它。
        demo2 = self.show(SKU2, after_id=1)
        self.assertEqual(demo2["quantity"], 4)
        self.assertEqual(
            movement_tuples(demo2["movements"]), [(2, "receive", 4, 4)]
        )
        # 下界等于该 SKU 自身流水编号时，严格大于使其被排除。
        self.assertEqual(self.show(SKU2, after_id=2)["movements"], [])

    def test_repeated_and_full_query_unchanged(self):
        # 完整查询作为基准。
        before = self.show(SKU1)
        # 重复同一筛选查询结果完全相同。
        first = self.show(SKU1, after_id=2)
        second = self.show(SKU1, after_id=2)
        self.assertEqual(first, second)
        self.assertEqual(
            self.show(SKU1, after_id=2, mtype="receive"),
            self.show(SKU1, after_id=2, mtype="receive"),
        )
        # 一系列筛选查询之后：商品信息与全部流水没有新增、删除或改写。
        self.assertEqual(self.show(SKU1), before)
        self.assertEqual(self.show(SKU2), self.show(SKU2, after_id=0))


class TestAfterIdEmptyCases(InventoryCLITestCase):
    """无匹配场景：退出码 0、movements 为 []，当前数量照常返回。"""

    def test_registered_without_movements(self):
        self.add_product(SKU1, NAME1)
        for bound in (0, 1, 999):
            with self.subTest(after_id=bound):
                page = self.show(SKU1, after_id=bound)
                self.assertEqual(page["sku"], SKU1)
                self.assertEqual(page["name"], NAME1)
                self.assertEqual(page["quantity"], 0)
                self.assertEqual(page["movements"], [])

    def test_no_movement_after_bound(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "5")
        before = self.show(SKU1)
        # 唯一流水编号为 1，下界取 1（严格大于）后没有匹配。
        page = self.show(SKU1, after_id=1)
        self.assertEqual(page["quantity"], 5)
        self.assertEqual(page["movements"], [])
        # 数据未被查询改变。
        self.assertEqual(self.show(SKU1), before)


class TestInvalidAfterIdRejected(InventoryCLITestCase):
    """非法 --after-id：退出码 2、stdout 为空、stderr 含参数名与原因、数据不变。"""

    def test_invalid_after_id_values(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        before = self.show(SKU1)

        for bad in (
            "",          # 空字符串
            "-1",        # 负数
            "1.5",       # 小数
            "abc",       # 非数字
            "0x1",       # 非十进制
            "+1",        # 带正号
            " 1",        # 含空白
            OVER_MAX_ID,  # 超出 64 位上界
            "999999999999999999999999999999",  # 远超上界
        ):
            with self.subTest(after_id=bad):
                err = self.run_rejected(
                    "show", "--sku", SKU1, "--after-id", bad
                )
                self.assertIn("--after-id", err)
                # 每次失败后商品信息与流水都与之前一致。
                self.assertEqual(self.show(SKU1), before)

    def test_missing_after_id_value(self):
        self.add_product(SKU1, NAME1)
        # --after-id 缺值：argparse 以退出码 2 拒绝，stdout 为空。
        code, out, err = self.run_cli("show", "--sku", SKU1, "--after-id")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--after-id", err)

    def test_invalid_after_id_with_type_still_rejected(self):
        self.add_product(SKU1, NAME1)
        # 非法编号与 --type 同用时，参数错误优先，不进行查询。
        code, out, err = self.run_cli(
            "show", "--sku", SKU1, "--type", "receive", "--after-id", "x"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--after-id", err)


if __name__ == "__main__":
    unittest.main()
