"""show --limit 流水条数限定的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证 --limit 在 --sku/--type/--after-id
筛选之后按原始 id 升序取前 N 条：只影响返回的 movements，不改变商品
当前数量，不新增、删除或改写已保存的流水，不重新编号或重算余额；
可与 --type、--after-id 一起用于翻页。

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

    def show(self, sku, mtype=..., after_id=..., limit=...):
        """值为 ... 时不传对应参数；None 对 after_id/limit 同样表示不传。"""
        args = ["show", "--sku", sku]
        if mtype is not ...:
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
    """用户验收主场景：新建台账，登记 DEMO-1 后入库 10、出库 3、入库 2。"""

    def setUp(self):
        super().setUp()
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

    def test_limit_first_page(self):
        # --type receive --limit 1：只返回编号 1，quantity/balance 均为 10，
        # 商品 quantity 仍为当前状态 9。
        page = self.show(SKU1, mtype="receive", limit=1)
        self.assertEqual(page["sku"], SKU1)
        self.assertEqual(page["name"], NAME1)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 10, 10)]
        )

    def test_limit_pagination_with_after_id(self):
        # 沿用上一次的参数并加入 --after-id 1：只返回编号 3（编号 2 是出库，
        # 已被 --type receive 排除），quantity 为 2、balance 为 9。
        first = self.show(SKU1, mtype="receive", limit=1)
        last_id = first["movements"][0]["id"]
        page = self.show(SKU1, mtype="receive", after_id=last_id, limit=1)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]), [(3, "receive", 2, 9)]
        )
        # 再往后没有入库流水：空数组，当前数量照常返回。
        tail = self.show(SKU1, mtype="receive", after_id=3, limit=1)
        self.assertEqual(tail["quantity"], 9)
        self.assertEqual(tail["movements"], [])

    def test_repeated_queries_consistent(self):
        # 重复查询结果一致。
        self.assertEqual(
            self.show(SKU1, mtype="receive", limit=1),
            self.show(SKU1, mtype="receive", limit=1),
        )
        self.assertEqual(
            self.show(SKU1, mtype="receive", after_id=1, limit=1),
            self.show(SKU1, mtype="receive", after_id=1, limit=1),
        )

    def test_without_limit_returns_all_matches(self):
        # 取消 --limit 后返回两个入库流水；完整查询返回全部三条流水。
        receives = self.show(SKU1, mtype="receive")
        self.assertEqual(
            movement_tuples(receives["movements"]),
            [(1, "receive", 10, 10), (3, "receive", 2, 9)],
        )
        full = self.show(SKU1)
        self.assertEqual(full["quantity"], 9)
        self.assertEqual(
            movement_tuples(full["movements"]),
            [(1, "receive", 10, 10), (2, "issue", 3, 7), (3, "receive", 2, 9)],
        )

    def test_limit_does_not_mutate_data(self):
        before = self.show(SKU1)
        self.show(SKU1, mtype="receive", limit=1)
        self.show(SKU1, mtype="receive", after_id=1, limit=1)
        self.show(SKU1, limit=2)
        self.assertEqual(self.show(SKU1), before)


class TestLimitFiltering(InventoryCLITestCase):
    """跨商品交错编号时，名额只在当前 SKU 已筛选流水内计数。"""

    def setUp(self):
        super().setUp()
        self.add_product(SKU1, NAME1)
        self.add_product(SKU2, NAME2)
        # DEMO-1 入库 10（id 1）；DEMO-2 入库 4（id 2）；
        # DEMO-1 出库 3（id 3）、再次入库 2（id 4）。
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        self.run_ok("receive", "--sku", SKU2, "--qty", "4")
        self.run_ok("issue", "--sku", SKU1, "--qty", "3")
        self.run_ok("receive", "--sku", SKU1, "--qty", "2")

    def test_other_products_do_not_consume_quota(self):
        # limit 2 取 DEMO-1 全部三条流水中的前两条（id 1、3），
        # 编号 2 属于 DEMO-2，不占名额也不混入结果。
        page = self.show(SKU1, limit=2)
        self.assertEqual(page["quantity"], 9)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7)],
        )

    def test_limit_after_type_and_after_id(self):
        # 先按 --after-id 2 筛选（剩 id 3、4），再取前 1 条：id 3。
        page = self.show(SKU1, after_id=2, limit=1)
        self.assertEqual(
            movement_tuples(page["movements"]), [(3, "issue", 3, 7)]
        )
        page2 = self.show(SKU1, mtype="receive", after_id=1, limit=1)
        self.assertEqual(
            movement_tuples(page2["movements"]), [(4, "receive", 2, 9)]
        )
        # limit 大于筛选后匹配条数：全部返回。
        many = self.show(SKU1, mtype="receive", limit=1000)
        self.assertEqual(
            [m["id"] for m in many["movements"]], [1, 4]
        )

    def test_limit_fewer_matches_return_all(self):
        page = self.show(SKU1, limit=10)
        self.assertEqual(
            movement_tuples(page["movements"]),
            [(1, "receive", 10, 10), (3, "issue", 3, 7), (4, "receive", 2, 9)],
        )
        self.assertEqual(self.show(SKU1, limit=10), self.show(SKU1))

    def test_limit_keeps_original_ids_and_balances(self):
        # 流水保留原始编号、数量和余额，不重新编号或重算余额。
        full = {m["id"]: m for m in self.show(SKU1)["movements"]}
        page = self.show(SKU1, limit=2)
        for m in page["movements"]:
            self.assertEqual(m, full[m["id"]])

    def test_limit_other_sku_independent(self):
        # DEMO-2 只有编号 2 一条流水，limit 不串到 DEMO-1。
        demo2 = self.show(SKU2, limit=1)
        self.assertEqual(demo2["quantity"], 4)
        self.assertEqual(
            movement_tuples(demo2["movements"]), [(2, "receive", 4, 4)]
        )


class TestLimitEdgeValues(InventoryCLITestCase):
    """合法边界：前导零、上限 1000、无流水/无匹配时空数组。"""

    def test_leading_zeros_equivalent(self):
        self.add_product(SKU1, NAME1)
        for qty in ("10", "3", "2"):
            self.run_ok("receive", "--sku", SKU1, "--qty", qty)
        self.assertEqual(
            self.show(SKU1, limit="0002")["movements"],
            self.show(SKU1, limit=2)["movements"],
        )
        # 0001 与 1 等价。
        one = self.show(SKU1, limit=1)
        lead = self.show(SKU1, limit="0001")
        self.assertEqual(one, lead)
        self.assertEqual(len(one["movements"]), 1)

    def test_upper_bound_1000_valid(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "5")
        page = self.show(SKU1, limit=1000)
        self.assertEqual(page["quantity"], 5)
        self.assertEqual(len(page["movements"]), 1)

    def test_limit_one_exact_count(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "5")
        page = self.show(SKU1, limit=1)
        self.assertEqual(
            movement_tuples(page["movements"]), [(1, "receive", 5, 5)]
        )

    def test_empty_movements_with_limit(self):
        # 已登记但没有流水：limit 下仍为空数组，数量为当前值 0。
        self.add_product(SKU1, NAME1)
        for n in (1, 1000):
            with self.subTest(limit=n):
                page = self.show(SKU1, limit=n)
                self.assertEqual(page["quantity"], 0)
                self.assertEqual(page["movements"], [])

    def test_no_match_after_bound_with_limit(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "5")
        page = self.show(SKU1, after_id=999, limit=1)
        self.assertEqual(page["quantity"], 5)
        self.assertEqual(page["movements"], [])


class TestInvalidLimitRejected(InventoryCLITestCase):
    """非法 --limit：退出码 2、stdout 为空、stderr 含 --limit 与原因、数据不变。"""

    def test_invalid_limit_values(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        self.run_ok("issue", "--sku", SKU1, "--qty", "3")
        before = self.show(SKU1)

        for bad in (
            "",        # 空字符串
            "0",       # 零
            "0000",    # 前导零的零
            "-1",      # 负数
            "1.5",     # 小数
            "+1",      # 带正号
            " 1",      # 前端空白
            "1 ",      # 后端空白
            " 1 ",     # 两端空白
            "abc",     # 非数字
            "1e3",     # 科学计数法
            "0x1",     # 非十进制
            "1001",    # 超过上限
            "99999",   # 远超上限
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

    def test_invalid_limit_takes_precedence_over_missing_sku(self):
        # 非法 --limit 与不存在的 SKU 同时出现：先报告参数错误，
        # 不打开/查询数据库。
        code, out, err = self.run_cli(
            "show", "--sku", "NO-SUCH-SKU", "--limit", "0"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--limit", err)

        code, out, err = self.run_cli(
            "show", "--sku", "NO-SUCH-SKU", "--limit", "1001"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--limit", err)

    def test_valid_limit_but_sku_missing_or_empty(self):
        self.add_product(SKU1, NAME1)
        # 参数合法但 SKU 不存在：商品不存在错误（退出码 2、stdout 为空）。
        code, out, err = self.run_cli(
            "show", "--sku", "NO-SUCH-SKU", "--limit", "1"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("NO-SUCH-SKU", err)
        # SKU 去掉两端空白后为空：说明 SKU 为空。
        code, out, err = self.run_cli("show", "--sku", "   ", "--limit", "1")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_invalid_limit_with_other_filters_still_rejected(self):
        self.add_product(SKU1, NAME1)
        # 非法 --limit 与 --type/--after-id 同用时，参数错误优先，不进行查询。
        code, out, err = self.run_cli(
            "show", "--sku", SKU1, "--type", "receive",
            "--after-id", "1", "--limit", "x",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--limit", err)

    def test_limit_does_not_change_existing_commands(self):
        # add/receive/issue 不接受 --limit；show 之外的行为保持兼容。
        self.add_product(SKU1, NAME1)
        code, out, err = self.run_cli(
            "receive", "--sku", SKU1, "--qty", "1", "--limit", "1"
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
