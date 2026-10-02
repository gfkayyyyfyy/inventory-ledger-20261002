"""show --type 流水筛选的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证筛选只影响返回的流水：
不改变商品当前数量，不新增、删除或改写已保存的流水。

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

    def show(self, sku, mtype=None):
        args = ["show", "--sku", sku]
        if mtype is not None:
            args += ["--type", mtype]
        return self.run_ok(*args)

    def add_product(self, sku, name):
        payload = self.run_ok("add", "--sku", sku, "--name", name)
        self.assertEqual(payload, {"sku": sku, "name": name, "quantity": 0})

    def assert_movements_well_formed(self, movements):
        """流水编号唯一且按升序返回（不要求连续），字段齐全。"""
        ids = [m["id"] for m in movements]
        self.assertEqual(len(ids), len(set(ids)), "流水编号应唯一")
        self.assertEqual(ids, sorted(ids), "流水应按编号升序返回")
        for m in movements:
            self.assertIn(m["type"], ("receive", "issue"))
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)


def movement_tuples(movements):
    """提取 (type, quantity, balance)，便于只比对业务内容。"""
    return [(m["type"], m["quantity"], m["balance"]) for m in movements]


class TestShowTypeFilter(InventoryCLITestCase):
    """主场景：跨商品操作后，receive/issue 筛选只过滤流水、不改动任何数据。"""

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

    def test_filter_only_filters_movements(self):
        # 筛选前的完整查询作为基准，筛选结束后必须与它完全一致。
        before = self.show(SKU1)
        self.assertEqual(before["sku"], SKU1)
        self.assertEqual(before["name"], NAME1)
        self.assertEqual(before["quantity"], 9)
        self.assertEqual(
            movement_tuples(before["movements"]),
            [("receive", 10, 10), ("issue", 3, 7), ("receive", 2, 9)],
        )
        self.assert_movements_well_formed(before["movements"])
        full_by_id = {m["id"]: m for m in before["movements"]}
        demo1_ids = [m["id"] for m in before["movements"]]

        # DEMO-2 的入库流水插在 DEMO-1 两条流水之间，造成编号间隔。
        demo2 = self.show(SKU2)
        self.assertEqual(demo2["quantity"], 4)
        self.assertEqual(
            movement_tuples(demo2["movements"]), [("receive", 4, 4)]
        )
        demo2_id = demo2["movements"][0]["id"]

        # 只验证编号之间的相对关系，不依赖绝对编号从几开始：
        # DEMO-1 的相对编号为 0、2、3，DEMO-2 占据 1 的间隔位置。
        base_id = demo1_ids[0]
        self.assertEqual([i - base_id for i in demo1_ids], [0, 2, 3])
        self.assertEqual(demo2_id - base_id, 1)

        # 筛选 receive：两条入库流水，数量与操作后余额分别为 10/10 和 2/9。
        receives = self.show(SKU1, "receive")
        self.assertEqual(receives["sku"], SKU1)
        self.assertEqual(receives["name"], NAME1)
        self.assertEqual(receives["quantity"], 9)  # 当前数量不随筛选变化
        self.assertEqual(
            movement_tuples(receives["movements"]),
            [("receive", 10, 10), ("receive", 2, 9)],
        )
        receive_ids = [m["id"] for m in receives["movements"]]
        self.assertEqual(receive_ids, sorted(receive_ids))
        # 编号保留跨商品间隔（相对编号 0、3），不重新编号、不按筛选结果重排。
        self.assertEqual([i - base_id for i in receive_ids], [0, 3])
        # 每条筛选记录都与完整查询中同编号记录完全一致（含原始余额）。
        for m in receives["movements"]:
            self.assertEqual(m, full_by_id[m["id"]])
        # 第二条入库余额仍是操作当时的 9，而非按筛选后数量重算的值。
        self.assertEqual(receives["movements"][-1]["balance"], 9)
        # 不混入 DEMO-2 的流水。
        self.assertNotIn(demo2_id, receive_ids)

        # 筛选 issue：仅一条数量 3、操作后余额 7 的出库流水。
        issues = self.show(SKU1, "issue")
        self.assertEqual(issues["sku"], SKU1)
        self.assertEqual(issues["name"], NAME1)
        self.assertEqual(issues["quantity"], 9)
        self.assertEqual(
            movement_tuples(issues["movements"]), [("issue", 3, 7)]
        )
        issue_ids = [m["id"] for m in issues["movements"]]
        self.assertEqual([i - base_id for i in issue_ids], [2])
        self.assertEqual(len(issues["movements"]), 1)
        self.assertEqual(issues["movements"][0], full_by_id[issue_ids[0]])
        self.assertNotIn(demo2_id, issue_ids)

        # 对 DEMO-2 的筛选同样不会串到 DEMO-1 的流水。
        demo2_receives = self.show(SKU2, "receive")
        self.assertEqual(
            [m["id"] for m in demo2_receives["movements"]], [demo2_id]
        )
        self.assertEqual(
            movement_tuples(demo2_receives["movements"]), [("receive", 4, 4)]
        )
        self.assertEqual(self.show(SKU2, "issue")["movements"], [])

        # 重复执行同一筛选查询，结果完全相同。
        self.assertEqual(self.show(SKU1, "receive"), receives)
        self.assertEqual(self.show(SKU1, "issue"), issues)

        # 一系列筛选查询之后：商品信息与全部流水没有新增、删除或改写。
        self.assertEqual(self.show(SKU1), before)
        self.assertEqual(self.show(SKU2), demo2)


class TestTypeFilterEmpty(InventoryCLITestCase):
    """筛选无匹配流水：退出码 0、movements 为 []，当前数量与已存流水不受影响。"""

    def test_registered_without_movements(self):
        self.add_product(SKU1, NAME1)
        before = self.show(SKU1)
        self.assertEqual(before["quantity"], 0)
        self.assertEqual(before["movements"], [])

        # 已登记但没有任何流水：按 issue 查询返回空数组，数量仍为实际值 0。
        filtered = self.show(SKU1, "issue")
        self.assertEqual(filtered["sku"], SKU1)
        self.assertEqual(filtered["name"], NAME1)
        self.assertEqual(filtered["quantity"], 0)
        self.assertEqual(filtered["movements"], [])

        # 重复查询结果一致；不带筛选时仍返回（空的）全部流水。
        self.assertEqual(self.show(SKU1, "issue"), filtered)
        self.assertEqual(self.show(SKU1), before)

    def test_receive_only_product_filtered_by_issue(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "5")
        before = self.show(SKU1)
        self.assertEqual(before["quantity"], 5)
        self.assertEqual(
            movement_tuples(before["movements"]), [("receive", 5, 5)]
        )

        # 只有入库流水时按 issue 查询：退出码 0、空数组，当前数量保持 5。
        filtered = self.show(SKU1, "issue")
        self.assertEqual(filtered["quantity"], 5)
        self.assertEqual(filtered["movements"], [])

        # 重复查询结果一致；不带筛选时仍返回全部已有入库流水，记录原样保留。
        self.assertEqual(self.show(SKU1, "issue"), filtered)
        self.assertEqual(self.show(SKU1), before)


class TestInvalidTypeRejected(InventoryCLITestCase):
    """不支持的 --type 取值：退出码 2、stdout 为空、stderr 说明类型无效、数据不变。"""

    def test_adjust_type_rejected_and_data_unchanged(self):
        self.add_product(SKU1, NAME1)
        self.run_ok("receive", "--sku", SKU1, "--qty", "10")
        self.run_ok("issue", "--sku", SKU1, "--qty", "3")
        before = self.show(SKU1)
        self.assertEqual(before["quantity"], 7)

        # --type adjust 不是受支持的筛选类型。
        code, out, err = self.run_cli("show", "--sku", SKU1, "--type", "adjust")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--type", err)
        self.assertIn("adjust", err)

        # 被拒绝的查询不改动商品信息与任何流水。
        self.assertEqual(self.show(SKU1), before)

        # 重复执行仍被同样拒绝；随后合法筛选与不带筛选的查询一切照常。
        code2, out2, err2 = self.run_cli(
            "show", "--sku", SKU1, "--type", "adjust"
        )
        self.assertEqual((code2, out2, err2), (code, out, err))
        self.assertEqual(
            movement_tuples(self.show(SKU1, "receive")["movements"]),
            [("receive", 10, 10)],
        )
        self.assertEqual(
            movement_tuples(self.show(SKU1, "issue")["movements"]),
            [("issue", 3, 7)],
        )
        self.assertEqual(self.show(SKU1), before)


if __name__ == "__main__":
    unittest.main()
