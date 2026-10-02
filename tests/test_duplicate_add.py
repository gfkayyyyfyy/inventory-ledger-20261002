"""重复登记（add）数据保护的回归测试。

以 README 公开的 `python -m inventory --db ...` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证 SKU 重复登记被拒绝时：

- 退出码 2、stdout 为空、stderr 包含该 SKU 并说明商品已存在；
- 不覆盖原商品名称、不改变余额、不追加任何流水；
- SKU 去掉两端空白后与已有 SKU 相同也算重复；
- SKU 区分大小写，demo-1 与 DEMO-1 是各自独立的商品。

只比较解析后的业务数据（json.loads 后的 dict），不依赖 JSON 键序、
空白格式或整段错误文案，因此能检出名称被覆盖、被插入虚假流水、
或把不同大小写误判为重复等改动。

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
OTHER_NAME = "另一个名称"
LOWER_SKU = "demo-1"
LOWER_NAME = "演示螺母小写"
FRESH_SKU = "DEMO-EMPTY"
FRESH_NAME = "演示垫片"


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

    def show(self, sku):
        return self.run_ok("show", "--sku", sku)

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

    def assert_duplicate_add_rejected(self, dup_sku, dup_name, existing_sku, baseline):
        """重复登记必须：退出码 2、stdout 为空、stderr 含已有 SKU 与已存在说明，
        且独立查询结果与失败前完全一致（名称、余额、完整流水均不变）。"""
        code, out, err = self.run_cli(
            "add", "--sku", dup_sku, "--name", dup_name
        )
        self.assertEqual(
            code, 2, f"重复登记 {dup_sku!r} 应以退出码 2 拒绝"
        )
        self.assertEqual(out, "", "重复登记被拒绝时 stdout 应为空")
        # 只校验关键信息，不依赖整段错误文案。
        self.assertIn(existing_sku, err, "stderr 应包含已登记的 SKU")
        self.assertIn("已存在", err, "stderr 应说明商品已存在")
        # 再次独立查询：商品名称、余额和完整 movements 与失败前相同。
        self.assertEqual(self.show(existing_sku), baseline)


def movement_tuples(movements):
    """提取 (type, quantity, balance)，便于只比对业务内容。"""
    return [(m["type"], m["quantity"], m["balance"]) for m in movements]


class TestDuplicateAddProtectsData(InventoryCLITestCase):
    """主场景：有余额有流水的商品被多次重复登记，数据始终不受影响。"""

    def test_duplicate_add_after_receive_and_issue(self):
        # 登记 DEMO-1 / 演示螺母，入库 10、出库 3。
        self.add_product(SKU, NAME)
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10")["quantity"], 10
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "3")["quantity"], 7
        )

        # 保存一次完整 show 的结果：余额 7，两条流水，编号唯一且递增。
        baseline = self.show(SKU)
        self.assertEqual(baseline["sku"], SKU)
        self.assertEqual(baseline["name"], NAME)
        self.assertEqual(baseline["quantity"], 7)
        self.assertEqual(len(baseline["movements"]), 2)
        self.assertEqual(
            movement_tuples(baseline["movements"]),
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        self.assert_movements_well_formed(baseline["movements"])
        original_ids = [m["id"] for m in baseline["movements"]]

        # 三种重复登记方式都应被同样拒绝，且不能覆盖名称或追加流水：
        # 1) 相同 SKU、不同名称；
        # 2) SKU 两端带空白（去空白后仍是同一个 SKU）、不同名称；
        # 3) 完全相同的 SKU 与名称再次登记。
        variants = [
            (SKU, OTHER_NAME),
            ("  " + SKU + "  ", OTHER_NAME),
            ("\t " + SKU + " \n", NAME),
            (SKU, NAME),
        ]
        for dup_sku, dup_name in variants:
            with self.subTest(dup_sku=dup_sku, dup_name=dup_name):
                self.assert_duplicate_add_rejected(
                    dup_sku, dup_name, SKU, baseline
                )
                # 已有流水的编号、类型、数量和余额逐一保留。
                after = self.show(SKU)
                self.assertEqual(
                    [m["id"] for m in after["movements"]], original_ids
                )
                self.assertEqual(
                    movement_tuples(after["movements"]),
                    [("receive", 10, 10), ("issue", 3, 7)],
                )

        # 拒绝不会污染后续正常业务：再入库 2 成功，余额变为 9。
        received = self.run_ok("receive", "--sku", SKU, "--qty", "2")
        self.assertEqual(received, {"sku": SKU, "name": NAME, "quantity": 9})

        final = self.show(SKU)
        self.assertEqual(final["name"], NAME)
        self.assertEqual(final["quantity"], 9)
        # 仅新增一条入库流水，先前两条（含编号与余额）原样保留。
        self.assertEqual(len(final["movements"]), 3)
        self.assertEqual(final["movements"][:2], baseline["movements"])
        new_movement = final["movements"][2]
        self.assertEqual(
            (
                new_movement["type"],
                new_movement["quantity"],
                new_movement["balance"],
            ),
            ("receive", 2, 9),
        )
        # 新编号只要求比之前更大、整体唯一递增，不要求编号连续。
        self.assertGreater(new_movement["id"], original_ids[-1])
        self.assert_movements_well_formed(final["movements"])


class TestDuplicateAddWithoutMovements(InventoryCLITestCase):
    """刚登记、尚无入出库记录时重复登记：仍为零余额和空流水。"""

    def test_duplicate_add_fresh_product(self):
        self.add_product(FRESH_SKU, FRESH_NAME)

        baseline = self.show(FRESH_SKU)
        self.assertEqual(
            baseline,
            {
                "sku": FRESH_SKU,
                "name": FRESH_NAME,
                "quantity": 0,
                "movements": [],
            },
        )

        # 不同名称重复登记、带空白 SKU 重复登记、原样重复登记，全部拒绝。
        for dup_sku, dup_name in [
            (FRESH_SKU, OTHER_NAME),
            ("  " + FRESH_SKU + "  ", FRESH_NAME),
            (FRESH_SKU, FRESH_NAME),
        ]:
            with self.subTest(dup_sku=dup_sku, dup_name=dup_name):
                self.assert_duplicate_add_rejected(
                    dup_sku, dup_name, FRESH_SKU, baseline
                )
                after = self.show(FRESH_SKU)
                self.assertEqual(after["name"], FRESH_NAME)
                self.assertEqual(after["quantity"], 0)
                self.assertEqual(after["movements"], [])


class TestSkuCaseSensitive(InventoryCLITestCase):
    """SKU 区分大小写：demo-1 与 DEMO-1 各自独立，互不影响。"""

    def test_lowercase_sku_is_independent_product(self):
        # DEMO-1 有余额和流水，作为受影响与否的对照。
        self.add_product(SKU, NAME)
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        self.run_ok("issue", "--sku", SKU, "--qty", "3")
        upper_before = self.show(SKU)
        self.assertEqual(upper_before["quantity"], 7)
        self.assertEqual(len(upper_before["movements"]), 2)

        # 仅大小写不同的 demo-1 必须登记成功：退出码 0、单个 JSON 对象。
        added = self.run_ok("add", "--sku", LOWER_SKU, "--name", LOWER_NAME)
        self.assertEqual(
            added, {"sku": LOWER_SKU, "name": LOWER_NAME, "quantity": 0}
        )

        # 新商品拥有独立的零余额与空流水。
        lower = self.show(LOWER_SKU)
        self.assertEqual(lower["sku"], LOWER_SKU)
        self.assertEqual(lower["name"], LOWER_NAME)
        self.assertEqual(lower["quantity"], 0)
        self.assertEqual(lower["movements"], [])

        # DEMO-1 的名称、余额和完整流水不受影响。
        self.assertEqual(self.show(SKU), upper_before)

        # 小写商品自身同样受重复登记保护。
        code, out, err = self.run_cli(
            "add", "--sku", LOWER_SKU, "--name", OTHER_NAME
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn(LOWER_SKU, err)
        self.assertIn("已存在", err)
        self.assertEqual(self.show(LOWER_SKU), lower)
        # 小写商品的失败登记同样不波及大写商品。
        self.assertEqual(self.show(SKU), upper_before)


if __name__ == "__main__":
    unittest.main()
