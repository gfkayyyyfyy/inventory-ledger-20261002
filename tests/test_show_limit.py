"""show --limit 流水条数限定的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证 --limit 只影响返回的流水条数：
在 SKU、--type、--after-id 筛选之后按原始 id 升序取前 N 条，
其他商品的流水不占名额；不改变商品当前数量，不重新编号或重算余额，
不新增、删除或改写已保存的流水。

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

    def show(self, sku, limit=..., after_id=..., mtype=None):
        """省略 limit/after_id 时不传对应参数；显式传 None 时也不传。"""
        args = ["show", "--sku", sku]
        if mtype is not None:
            args += ["--type", mtype]
        if after_id is not ...:
            args += ["--after-id", str(after_id)]
        if limit is not ...:
            args += ["--limit", str(limit)]
        return self.run_ok(*args)


def movement_tuples(movements):
    """提取 (id, type, quantity, balance)，便于只比对业务内容。"""
    return [(m["id"], m["type"], m["quantity"], m["balance"]) for m in movements]


class TestLimitAcceptanceScenario(InventoryCLITestCase):
    """README 验收主场景：新建台账，limit 配合 type/after-id 翻页。"""

    def setUp(self):
        super().setUp()
        # 只登记 DEMO-1：入库 10、出库 3、入库 2，当前数量 9，编号 1、2、3。
        self.add_product(SKU1, NAME1)
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU1, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU1, "--qty", "3")["quantity"], 7
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU1, "--qty", "2")["quantity"], 9
        )

    def test_limit_first_receive(self):
        page = self.show(SKU1, limit=1, mtype="receive")
        # 只返回编号 1：流水数量与余额均为 10，商品当前数量仍是 9。
        self.assertEqual(page["sku"], SKU1)
        self.assertEqual(page["name"], NAME1)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 10, 10)]
        )

    def test_limit_with_after_id_returns_next_receive(self):
        # 保留原参数并加入 --after-id 1：跳过编号 1，下一条入库是编号 3，
        # 出库流水编号 2 不占 receive 的名额。
        page = self.show(SKU1, limit=1, after_id=1, mtype="receive")
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]), [(3, "receive", 2, 9)]
        )

    def test_repeated_and_full_query_unchanged(self):
        first = self.show(SKU1, limit=1, mtype="receive")
        second = self.show(SKU1, limit=1, mtype="receive")
        self.assertEqual(first, second)  # 重复查询结果一致
        paged = self.show(SKU1, limit=1, after_id=1, mtype="receive")
        self.assertEqual(
            self.show(SKU1, limit=1, after_id=1, mtype="receive"), paged
        )
        # 取消 --limit 后返回两个入库流水（编号 1、3，保留原始编号与余额）。
        full_receives = self.show(SKU1, mtype="receive")
        self.assertEqual(
            movement_tuples(full_receives["movements"]),
            [(1, "receive", 10, 10), (3, "receive", 2, 9)],
        )
        # 不带任何筛选时三条流水原样返回，当前数量为 9。
        full = self.show(SKU1)
        self.assertEqual(full["quantity"], 9)
        self.assertEqual(
            movement_tuples(full["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7), (3, "receive", 2, 9)],
        )


class TestLimitFiltering(InventoryCLITestCase):
    """跨商品、边界与组合场景。"""

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        self.add_product(SKU2, NAME2)
        # DEMO-1 入库 10（编号 1）；DEMO-2 入库 4（编号 2）；
        # DEMO-1 出库 3（编号 3）、再次入库 2（编号 4）。
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        self.run_ok("receive", "--sku", SKU2, "--qty", "4")
        self.run_ok("issue", "--sku", SKU1, "--qty", "3")
        self.run_ok("receive", "--sku", SKU1, "--qty", "2")

    def test_other_products_do_not_consume_slots(self):
        # 不限类型取前 2 条：只有 DEMO-1 自己的编号 1、3 占用名额，
        # DEMO-2 的编号 2 不属于该 SKU，不出现在结果里。
        page = self.show(SKU1, limit=2)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7)],
        )

    def test_limit_applied_after_type_and_after_id(self):
        # 先按 receive + after_id 1 筛选得到编号 4，再取前 1 条。
        page = self.show(SKU1, limit=1, after_id=1, mtype="receive")
        self.assertEqual(
            movement_tuples(page["movements"]), [(4, "receive", 2, 9)]
        )
        # after_id 3 + limit 1 作用于全部类型：只剩编号 4。
        page_all = self.show(SKU1, limit=1, after_id=3)
        self.assertEqual(
            movement_tuples(page_all["movements"]), [(4, "receive", 2, 9)]
        )

    def test_limit_larger_than_matches_returns_all(self):
        # 匹配条数少于 N 时全部返回；1000 是允许的上限。
        page = self.show(SKU1, limit=1000)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7), (4, "receive", 2, 9)],
        )
        self.assertEqual(self.show(SKU1, limit=999), page)

    def test_leading_zeros_equivalent(self):
        # 允许前导零：0002 与 2 等价，0001 与 1 等价。
        self.assertEqual(
            self.show(SKU1, limit="0002"), self.show(SKU1, limit=2)
        )
        self.assertEqual(
            [m["id"] for m in self.show(SKU1, limit="0001")["movements"]], [1]
        )

    def test_limit_pagination_covers_all_in_order(self):
        # 用 limit=1 逐页翻完 DEMO-1 的全部流水：编号 1、3、4，顺序不乱。
        seen = []
        after = 0
        for _ in range(3):
            page = self.show(SKU1, limit=1, after_id=after)
            self.assertEqual(len(page["movements"]), 1)
            seen.append(page["movements"][0]["id"])
            after = page["movements"][0]["id"]
        self.assertEqual(seen, [1, 3, 4])
        # 再翻一页为空。
        self.assertEqual(self.show(SKU1, limit=1, after_id=after)["movements"], [])

    def test_limit_does_not_modify_data(self):
        before = self.show(SKU1)
        self.show(SKU1, limit=1)
        self.show(SKU1, limit=2, after_id=1, mtype="receive")
        self.show(SKU1, limit=1000)
        # 一系列限定查询之后：商品信息与全部流水没有新增、删除或改写。
        self.assertEqual(self.show(SKU1), before)
        demo2 = self.show(SKU2)
        self.assertEqual(demo2["quantity"], 4)
        self.assertEqual(
            movement_tuples(demo2["movements"]), [(2, "receive", 4, 4)]
        )


class TestLimitEmptyCases(InventoryCLITestCase):
    """无流水或无匹配：退出码 0、movements 为 []，当前数量照常返回。"""

    def test_registered_without_movements(self):
        self.add_product(SKU1, NAME1)
        for n in (1, 1000):
            with self.subTest(limit=n):
                page = self.show(SKU1, limit=n)
                self.assertEqual(page["sku"], SKU1)
                self.assertEqual(page["name"], NAME1)
                self.assertEqual(page["quantity"], 0)
                self.assertEqual(page["movements"], [])

    def test_no_match_after_filter(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "5")
        # 唯一入库流水编号 1，after-id 1 之后无匹配：名额再大也是空数组。
        page = self.show(SKU1, limit=1, after_id=1)
        self.assertEqual(page["quantity"], 5)
        self.assertEqual(page["movements"], [])
        page_issue = self.show(SKU1, limit=3, mtype="issue")
        self.assertEqual(page_issue["quantity"], 5)
        self.assertEqual(page_issue["movements"], [])


class TestInvalidLimitRejected(InventoryCLITestCase):
    """非法 --limit：退出码 2、stdout 为空、stderr 含 --limit 与原因、数据不变。"""

    def test_invalid_limit_values(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        before = self.show(SKU1)

        for bad in (
            "",       # 空字符串
            "0",      # 零
            "0000",   # 前导零的零
            "-1",     # 负数
            "1.5",    # 小数
            "+1",     # 带正号
            " 1",     # 左端空白
            "1 ",     # 右端空白
            "abc",    # 非数字
            "0x1",    # 非十进制
            "1e2",    # 科学计数法
            "1001",   # 超过上限
            "9999",
            "999999999999999999999999",  # 远超上限
        ):
            with self.subTest(limit=bad):
                err = self.run_rejected(
                    "show", "--sku", SKU1, "--limit", bad
                )
                self.assertIn("--limit", err)
                # 每次失败后商品信息与流水都与之前一致。
                self.assertEqual(self.show(SKU1), before)

    def test_missing_limit_value(self):
        self.add_product(SKU1, NAME1)
        # --limit 缺值：argparse 以退出码 2 拒绝，stdout 为空。
        code, out, err = self.run_cli("show", "--sku", SKU1, "--limit")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--limit", err)

    def test_invalid_limit_reported_before_missing_sku(self):
        self.add_product(SKU1, NAME1)
        # 非法 --limit 与不存在的 SKU 同时出现：先报告参数错误，不进行查询。
        err = self.run_rejected(
            "show", "--sku", "NOT-EXIST", "--limit", "0"
        )
        self.assertIn("--limit", err)
        # 前导空白等非法值同样在 SKU 查询之前被拒绝。
        err2 = self.run_rejected(
            "show", "--sku", "NOT-EXIST", "--limit", " 2"
        )
        self.assertIn("--limit", err2)

    def test_valid_limit_with_empty_or_missing_sku(self):
        self.add_product(SKU1, NAME1)
        # 参数合法但 SKU 去掉空白后为空：退出码 2，stderr 说明 SKU 为空。
        code, out, err = self.run_cli(
            "show", "--sku", "   ", "--limit", "1"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("SKU", err)
        # 参数合法但商品不存在：退出码 2，stderr 说明商品不存在。
        code, out, err = self.run_cli(
            "show", "--sku", "NOT-EXIST", "--limit", "1"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("NOT-EXIST", err)


if __name__ == "__main__":
    unittest.main()
