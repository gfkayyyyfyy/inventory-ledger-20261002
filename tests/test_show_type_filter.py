"""show 子命令 --type 筛选的回归测试。

以 README 公开的 `python -m inventory --db 文件` 命令入口为验收对象，
使用真实 SQLite 临时文件。验证目标：筛选只影响返回的流水，
不改变商品当前库存，也不新增、删除或改写已保存的流水记录。

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

SKU_1 = "DEMO-1"
NAME_1 = "演示螺母"
SKU_2 = "DEMO-2"
NAME_2 = "演示螺栓"


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
        """运行应被拒绝的命令，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), f"命令 {args} 被拒绝时 stderr 应说明原因")
        return err

    def add_product(self, sku, name):
        payload = self.run_ok("add", "--sku", sku, "--name", name)
        self.assertEqual(payload, {"sku": sku, "name": name, "quantity": 0})

    def move(self, command, sku, qty):
        return self.run_ok(command, "--sku", sku, "--qty", str(qty))

    def show(self, sku, mtype=None):
        args = ["show", "--sku", sku]
        if mtype is not None:
            args += ["--type", mtype]
        return self.run_ok(*args)

    def assert_product_info(self, payload, sku, name, quantity):
        """商品名称、SKU 与当前数量不随筛选变化。"""
        self.assertEqual(payload["sku"], sku)
        self.assertEqual(payload["name"], name)
        self.assertEqual(payload["quantity"], quantity)

    def assert_movements_well_formed(self, movements):
        """流水编号唯一且按升序返回（不要求连续），字段齐全。"""
        ids = [m["id"] for m in movements]
        self.assertEqual(len(ids), len(set(ids)), "流水编号应唯一")
        self.assertEqual(ids, sorted(ids), "流水应按编号升序返回")
        for m in movements:
            self.assertEqual(set(m.keys()), {"id", "type", "quantity", "balance"})
            self.assertIn(m["type"], ("receive", "issue"))
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)

    def movement_triples(self, payload):
        return [(m["type"], m["quantity"], m["balance"]) for m in payload["movements"]]

    def movements_by_id(self, payload):
        return {m["id"]: m for m in payload["movements"]}


class TestShowTypeFilter(InventoryCLITestCase):
    """跨商品主场景：筛选只返回该商品对应类型的流水，库存与记录不变。"""

    def setUp(self):
        super().setUp()
        # 先登记 DEMO-1、DEMO-2，再依次：
        # DEMO-1 入库 10、DEMO-2 入库 4、DEMO-1 出库 3、DEMO-1 再次入库 2。
        self.add_product(SKU_1, NAME_1)
        self.add_product(SKU_2, NAME_2)
        self.move("receive", SKU_1, 10)
        self.move("receive", SKU_2, 4)
        self.move("issue", SKU_1, 3)
        self.move("receive", SKU_1, 2)

    def test_unfiltered_show_returns_all_movements_with_gap_preserved(self):
        """不带筛选返回 DEMO-1 的三条流水，跨商品操作造成的编号间隔保留。"""
        full = self.show(SKU_1)
        self.assert_product_info(full, SKU_1, NAME_1, 9)
        self.assertEqual(
            self.movement_triples(full),
            [("receive", 10, 10), ("issue", 3, 7), ("receive", 2, 9)],
        )
        self.assert_movements_well_formed(full["movements"])

        # DEMO-2 的入库发生在 DEMO-1 第一条与第二条流水之间：
        # DEMO-1 的编号序列里应留出恰好一个编号的间隔，
        # 该编号属于 DEMO-2，不依赖编号从哪个绝对值开始。
        d1_ids = [m["id"] for m in full["movements"]]
        d2 = self.show(SKU_2)
        self.assert_product_info(d2, SKU_2, NAME_2, 4)
        self.assertEqual(self.movement_triples(d2), [("receive", 4, 4)])
        d2_ids = [m["id"] for m in d2["movements"]]
        self.assertEqual(len(d2_ids), 1)
        self.assertEqual(d1_ids[1] - d1_ids[0], 2, "跨商品编号间隔应保留")
        self.assertEqual(d1_ids[2] - d1_ids[1], 1)
        self.assertEqual(d2_ids[0], d1_ids[0] + 1)

    def test_receive_filter_matches_full_records_exactly(self):
        """--type receive 返回两条入库流水，编号、数量、余额与完整查询一致。"""
        full = self.show(SKU_1)
        filtered = self.show(SKU_1, "receive")

        self.assert_product_info(filtered, SKU_1, NAME_1, 9)
        self.assertEqual(
            self.movement_triples(filtered),
            [("receive", 10, 10), ("receive", 2, 9)],
        )
        self.assert_movements_well_formed(filtered["movements"])

        full_by_id = self.movements_by_id(full)
        filtered_ids = [m["id"] for m in filtered["movements"]]
        # 筛选结果必须是完整结果的子集且顺序一致，不能重排或重新编号。
        self.assertEqual(filtered_ids, [full["movements"][0]["id"],
                                        full["movements"][2]["id"]])
        for m in filtered["movements"]:
            self.assertEqual(m, full_by_id[m["id"]])  # 余额沿用记录值，不重算

    def test_issue_filter_matches_full_record_exactly(self):
        """--type issue 只返回数量 3、余额 7 的一条出库流水。"""
        full = self.show(SKU_1)
        filtered = self.show(SKU_1, "issue")

        self.assert_product_info(filtered, SKU_1, NAME_1, 9)
        self.assertEqual(self.movement_triples(filtered), [("issue", 3, 7)])
        self.assert_movements_well_formed(filtered["movements"])

        full_by_id = self.movements_by_id(full)
        self.assertEqual(len(filtered["movements"]), 1)
        self.assertEqual(filtered["movements"][0],
                         full_by_id[filtered["movements"][0]["id"]])

    def test_filter_never_mixes_in_other_product_movements(self):
        """DEMO-1 的筛选结果不能混入 DEMO-2 的流水。"""
        d2_ids = {m["id"] for m in self.show(SKU_2)["movements"]}
        for mtype in ("receive", "issue"):
            with self.subTest(type=mtype):
                ids = {m["id"] for m in self.show(SKU_1, mtype)["movements"]}
                self.assertTrue(ids.isdisjoint(d2_ids))

    def test_filtered_queries_are_read_only_and_repeatable(self):
        """成功查询前后，两个商品的完整流水完全一致；重复查询结果相同。"""
        before_1 = self.show(SKU_1)
        before_2 = self.show(SKU_2)

        for mtype in (None, "receive", "issue"):
            with self.subTest(type=mtype):
                first = self.show(SKU_1, mtype)
                second = self.show(SKU_1, mtype)
                self.assertEqual(first, second)

        # 查询不新增、删除或改写任何记录，当前库存也不变。
        self.assertEqual(self.show(SKU_1), before_1)
        self.assertEqual(self.show(SKU_2), before_2)
        self.assertEqual(self.show(SKU_1)["quantity"], 9)
        self.assertEqual(self.show(SKU_2)["quantity"], 4)


class TestShowTypeFilterEmpty(InventoryCLITestCase):
    """筛选的空结果边界：退出码 0、movements 为 []，数量与未筛选流水不变。"""

    def test_registered_product_without_movements(self):
        self.add_product(SKU_1, NAME_1)
        before = self.show(SKU_1)
        self.assertEqual(before["movements"], [])
        self.assertEqual(before["quantity"], 0)

        for mtype in ("receive", "issue"):
            with self.subTest(type=mtype):
                result = self.show(SKU_1, mtype)
                self.assert_product_info(result, SKU_1, NAME_1, 0)
                self.assertEqual(result["movements"], [])
                # 空结果查询同样是只读的，重复执行结果一致。
                self.assertEqual(self.show(SKU_1, mtype), result)

        # 不带筛选仍返回全部已有流水（仍为空），记录无任何变化。
        self.assertEqual(self.show(SKU_1), before)

    def test_receive_only_product_queried_with_issue_filter(self):
        self.add_product(SKU_1, NAME_1)
        self.add_product(SKU_2, NAME_2)
        self.move("receive", SKU_1, 10)
        self.move("receive", SKU_2, 4)  # 制造跨商品编号间隔
        self.move("receive", SKU_1, 2)
        before = self.show(SKU_1)
        self.assertEqual(before["quantity"], 12)

        empty = self.show(SKU_1, "issue")
        self.assert_product_info(empty, SKU_1, NAME_1, 12)
        self.assertEqual(empty["movements"], [])
        self.assertEqual(self.show(SKU_1, "issue"), empty)

        # 不带筛选仍返回全部两条入库流水，编号与余额原样保留。
        after = self.show(SKU_1)
        self.assertEqual(after, before)
        self.assertEqual(
            self.movement_triples(after),
            [("receive", 10, 10), ("receive", 2, 12)],
        )
        self.assert_movements_well_formed(after["movements"])
        # receive 筛选在同一数据集上仍能取出全部流水。
        self.assertEqual(self.show(SKU_1, "receive"), before)


class TestShowTypeInvalid(InventoryCLITestCase):
    """--type adjust 不属于 receive/issue：退出码 2，stdout 为空，数据不变。"""

    def test_invalid_type_rejected_without_changing_records(self):
        self.add_product(SKU_1, NAME_1)
        self.move("receive", SKU_1, 10)
        self.move("issue", SKU_1, 3)
        before = self.show(SKU_1)

        err = self.run_rejected("show", "--sku", SKU_1, "--type", "adjust")
        # stderr 需说明类型无效，并指出收到的值与合法取值。
        self.assertIn("adjust", err)
        self.assertIn("receive", err)
        self.assertIn("issue", err)

        # 被拒绝的查询前后，完整查询得到的商品信息与全部流水完全一致。
        after = self.show(SKU_1)
        self.assertEqual(after, before)
        self.assertEqual(after["quantity"], 7)
        self.assertEqual(
            self.movement_triples(after),
            [("receive", 10, 10), ("issue", 3, 7)],
        )

    def test_invalid_type_rejected_even_without_movements(self):
        self.add_product(SKU_1, NAME_1)
        before = self.show(SKU_1)
        self.run_rejected("show", "--sku", SKU_1, "--type", "adjust")
        self.assertEqual(self.show(SKU_1), before)


if __name__ == "__main__":
    unittest.main()
