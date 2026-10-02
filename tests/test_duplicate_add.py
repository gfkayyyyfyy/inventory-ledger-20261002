"""重复登记（add 相同 SKU）的数据保护回归测试。

以 README 公开的 `python -m inventory` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证重复登记被拒绝（退出码 2、stdout 为空、
stderr 说明商品已存在）且已有商品与流水不受任何影响。

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
        """运行应被业务规则拒绝的命令，返回 stderr 文本。"""
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, f"命令 {args} 应以退出码 2 拒绝")
        self.assertEqual(out, "", f"命令 {args} 被拒绝时 stdout 应为空")
        self.assertTrue(err.strip(), f"命令 {args} 被拒绝时 stderr 应说明原因")
        return err

    def run_duplicate_rejected(self, *args):
        """运行应被判定为重复登记的 add 命令，校验退出码与错误要点。"""
        err = self.run_rejected(*args)
        # 只核对关键信息（涉及哪个 SKU、原因是已存在），不依赖整段文案。
        self.assertIn(SKU, err)
        self.assertIn("已存在", err)
        return err

    def show(self, sku=SKU):
        return self.run_ok("show", "--sku", sku)

    def add_demo_product(self):
        payload = self.run_ok("add", "--sku", SKU, "--name", NAME)
        self.assertEqual(payload, {"sku": SKU, "name": NAME, "quantity": 0})

    def assert_movements_well_formed(self, movements):
        """流水编号唯一且按升序返回（不要求连续），字段齐全。"""
        ids = [m["id"] for m in movements]
        self.assertEqual(len(ids), len(set(ids)), "流水编号应唯一")
        self.assertEqual(ids, sorted(ids), "流水应按编号升序返回")
        for m in movements:
            self.assertIn(m["type"], ("receive", "issue"))
            self.assertIsInstance(m["quantity"], int)
            self.assertIsInstance(m["balance"], int)


class TestDuplicateAddAfterMovements(InventoryCLITestCase):
    """主场景：已有入库/出库流水的商品被重复登记，数据必须原样保留。"""

    def test_duplicate_add_does_not_change_data(self):
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        self.run_ok("issue", "--sku", SKU, "--qty", "3")

        # 失败前的完整查询结果：余额 7，两条流水。
        before = self.show()
        self.assertEqual(before["sku"], SKU)
        self.assertEqual(before["name"], NAME)
        self.assertEqual(before["quantity"], 7)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in before["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        self.assert_movements_well_formed(before["movements"])

        # 相同 SKU、不同名称：拒绝，stderr 指出该 SKU 已存在。
        self.run_duplicate_rejected("add", "--sku", SKU, "--name", "另一个名称")
        # 再次独立查询：名称、余额、完整流水（含编号）与失败前完全一致，
        # 名称未被覆盖，也没有追加虚假流水。
        self.assertEqual(self.show(), before)

        # 两端带空白的同一 SKU：去空白后仍视为重复，同样拒绝。
        self.run_duplicate_rejected("add", "--sku", "  DEMO-1  ", "--name", "空白绕过")
        self.assertEqual(self.show(), before)

        # 保持原名称再次登记：同样拒绝。
        self.run_duplicate_rejected("add", "--sku", SKU, "--name", NAME)
        self.assertEqual(self.show(), before)

        # 失败之后正常入库 2 件应成功，余额变为 9。
        received = self.run_ok("receive", "--sku", SKU, "--qty", "2")
        self.assertEqual(received, {"sku": SKU, "name": NAME, "quantity": 9})

        after = self.show()
        self.assertEqual(after["name"], NAME)
        self.assertEqual(after["quantity"], 9)
        # 仅新增一条入库流水，先前两条原样保留。
        self.assertEqual(len(after["movements"]), 3)
        self.assertEqual(after["movements"][:2], before["movements"])
        new_movement = after["movements"][2]
        self.assertEqual(new_movement["type"], "receive")
        self.assertEqual(new_movement["quantity"], 2)
        self.assertEqual(new_movement["balance"], 9)
        # 编号只校验唯一且递增，不要求连续。
        self.assertGreater(new_movement["id"], before["movements"][-1]["id"])
        self.assert_movements_well_formed(after["movements"])


class TestDuplicateAddFreshProduct(InventoryCLITestCase):
    """刚登记、尚无入出库记录的商品被重复登记，仍为零余额和空流水。"""

    def test_duplicate_add_on_fresh_product(self):
        self.add_demo_product()
        before = self.show()
        self.assertEqual(before["quantity"], 0)
        self.assertEqual(before["movements"], [])

        self.run_duplicate_rejected("add", "--sku", SKU, "--name", "重复登记")

        after = self.show()
        self.assertEqual(after, before)
        self.assertEqual(after["quantity"], 0)
        self.assertEqual(after["movements"], [])


class TestSkuCaseSensitivity(InventoryCLITestCase):
    """SKU 区分大小写：demo-1 是独立商品，不影响 DEMO-1 的数据。"""

    def test_lowercase_variant_registers_independently(self):
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        self.run_ok("issue", "--sku", SKU, "--qty", "3")
        before = self.show()

        # 同一台账中登记小写 demo-1 应成功，拥有独立的零余额和空流水。
        lower_sku = "demo-1"
        lower_name = "小写螺母"
        added = self.run_ok("add", "--sku", lower_sku, "--name", lower_name)
        self.assertEqual(added, {"sku": lower_sku, "name": lower_name, "quantity": 0})

        lower = self.show(lower_sku)
        self.assertEqual(lower, {
            "sku": lower_sku,
            "name": lower_name,
            "quantity": 0,
            "movements": [],
        })

        # DEMO-1 的名称、余额和完整流水均不受小写商品登记的影响。
        self.assertEqual(self.show(), before)


if __name__ == "__main__":
    unittest.main()
