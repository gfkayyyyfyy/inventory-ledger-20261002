"""出库数量边界的回归测试。

以 README 公开的 `python -m inventory` 命令入口为验收对象，
使用真实 SQLite 临时文件，验证命令结束后再次查询得到的持久化结果。

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

    def show(self):
        return self.run_ok("show", "--sku", SKU)

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


class TestIssueBoundary(InventoryCLITestCase):
    """主场景：入库 10、出库 3 后，对超量、等量、零余额出库的边界行为。"""

    def test_issue_boundary_lifecycle(self):
        self.add_demo_product()

        # 初始状态：数量为零，流水为空。
        initial = self.show()
        self.assertEqual(initial["quantity"], 0)
        self.assertEqual(initial["movements"], [])

        # 入库 10 件、出库 3 件，均返回退出码 0 与商品 JSON。
        received = self.run_ok("receive", "--sku", SKU, "--qty", "10")
        self.assertEqual(received, {"sku": SKU, "name": NAME, "quantity": 10})
        issued = self.run_ok("issue", "--sku", SKU, "--qty", "3")
        self.assertEqual(issued, {"sku": SKU, "name": NAME, "quantity": 7})

        # 持久化结果：数量 7，流水依次为入库 10 后余额 10、出库 3 后余额 7。
        stocked = self.show()
        self.assertEqual(stocked["sku"], SKU)
        self.assertEqual(stocked["name"], NAME)
        self.assertEqual(stocked["quantity"], 7)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in stocked["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        self.assert_movements_well_formed(stocked["movements"])

        # 申请出库 8 件（超过余额 7）：退出码 2，stdout 为空，
        # stderr 说明出库数量超过当前余额。
        err = self.run_rejected("issue", "--sku", SKU, "--qty", "8")
        self.assertIn("超过", err)

        # 失败后再次查询，商品信息和完整流水（含已有流水编号）与失败前一致。
        after_reject = self.show()
        self.assertEqual(after_reject, stocked)

        # 出库 7 件（等于余额）应成功，余额变为零。
        emptied = self.run_ok("issue", "--sku", SKU, "--qty", "7")
        self.assertEqual(emptied, {"sku": SKU, "name": NAME, "quantity": 0})

        # 仅新增一条数量为 7、操作后余额为 0 的出库流水，此前流水原样保留。
        drained = self.show()
        self.assertEqual(drained["quantity"], 0)
        self.assertEqual(drained["movements"][:-1], stocked["movements"])
        new_movements = drained["movements"][len(stocked["movements"]):]
        self.assertEqual(len(new_movements), 1)
        self.assertEqual(new_movements[0]["type"], "issue")
        self.assertEqual(new_movements[0]["quantity"], 7)
        self.assertEqual(new_movements[0]["balance"], 0)
        self.assert_movements_well_formed(drained["movements"])

        # 余额为零时再出库 1 件，仍按超量出库拒绝，查询结果保持不变。
        err = self.run_rejected("issue", "--sku", SKU, "--qty", "1")
        self.assertIn("超过", err)
        self.assertEqual(self.show(), drained)


class TestInvalidIssueQuantity(InventoryCLITestCase):
    """无效出库数量：0、-1、1.5、abc 统一拒绝且数据不变。"""

    def test_invalid_quantities_rejected(self):
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        before = self.show()
        self.assertEqual(before["quantity"], 10)

        for bad_qty in ("0", "-1", "1.5", "abc"):
            with self.subTest(qty=bad_qty):
                err = self.run_rejected("issue", "--sku", SKU, "--qty", bad_qty)
                self.assertIn("数量", err)
                # 每次失败后余额和流水都与之前一致。
                self.assertEqual(self.show(), before)


class TestShowIsReadOnly(InventoryCLITestCase):
    """连续查询两次结果一致：查询本身不写流水、不改余额。"""

    def test_repeated_show_is_stable(self):
        self.add_demo_product()
        self.run_ok("receive", "--sku", SKU, "--qty", "10")
        self.run_ok("issue", "--sku", SKU, "--qty", "3")

        first = self.show()
        second = self.show()
        self.assertEqual(first, second)
        self.assertEqual(
            [(m["type"], m["quantity"], m["balance"]) for m in first["movements"]],
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        self.assert_movements_well_formed(first["movements"])


if __name__ == "__main__":
    unittest.main()
