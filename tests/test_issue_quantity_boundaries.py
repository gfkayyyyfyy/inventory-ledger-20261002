"""出库数量边界的回归测试。

以 README 公开的 ``python -m inventory`` 命令为唯一入口，使用临时目录中的
真实 SQLite 文件，通过命令结束后的再次查询验证持久化结果。仅使用 Python
标准库，可从项目根目录通过 ``python -m unittest discover`` 重复执行。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKU = "DEMO-1"
NAME = "演示螺母"


class InventoryCliTestBase(unittest.TestCase):
    """每个用例使用独立临时数据库，不触碰任何已有台账。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inventory-test-")
        self.db_path = os.path.join(self.tmpdir, "ledger.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_cli(self, *args):
        """执行 python -m inventory --db <临时库> ...，返回完整进程结果。"""
        return subprocess.run(
            [sys.executable, "-m", "inventory", "--db", self.db_path, *args],
            cwd=REPO_ROOT,
            capture_output=True,
        )

    def assert_success_json(self, result):
        """成功命令：退出码 0，标准输出为且仅为一个可解析的 JSON 对象。"""
        self.assertEqual(
            result.returncode,
            0,
            f"expected exit code 0, got {result.returncode}; "
            f"stderr={result.stderr.decode('utf-8')!r}",
        )
        stdout = result.stdout.decode("utf-8")
        non_empty_lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(non_empty_lines), 1, f"stdout 应只有一个 JSON 对象: {stdout!r}")
        payload = json.loads(non_empty_lines[0])
        self.assertIsInstance(payload, dict)
        self.assertEqual(result.stderr, b"")
        return payload

    def assert_error_exit_2(self, result, reason_fragment):
        """业务/参数错误：退出码 2，标准输出为空，标准错误说明原因。"""
        self.assertEqual(
            result.returncode,
            2,
            f"expected exit code 2, got {result.returncode}; "
            f"stdout={result.stdout.decode('utf-8')!r} "
            f"stderr={result.stderr.decode('utf-8')!r}",
        )
        self.assertEqual(result.stdout, b"", "失败命令的标准输出必须为空")
        stderr = result.stderr.decode("utf-8")
        self.assertIn(reason_fragment, stderr)
        self.assertTrue(stderr.strip())

    def add_product(self):
        payload = self.assert_success_json(
            self.run_cli("add", "--sku", SKU, "--name", NAME)
        )
        self.assertEqual(payload, {"sku": SKU, "name": NAME, "quantity": 0})
        return payload

    def receive(self, qty):
        payload = self.assert_success_json(
            self.run_cli("receive", "--sku", SKU, "--qty", str(qty))
        )
        self.assertEqual(payload["sku"], SKU)
        self.assertEqual(payload["name"], NAME)
        return payload

    def issue_ok(self, qty):
        payload = self.assert_success_json(
            self.run_cli("issue", "--sku", SKU, "--qty", str(qty))
        )
        self.assertEqual(payload["sku"], SKU)
        self.assertEqual(payload["name"], NAME)
        return payload

    def show(self):
        payload = self.assert_success_json(self.run_cli("show", "--sku", SKU))
        self.assertEqual(payload["sku"], SKU)
        self.assertEqual(payload["name"], NAME)
        self.assertIsInstance(payload["movements"], list)
        return payload

    @staticmethod
    def movement_triples(payload):
        """只比较流水的类型、数量、操作后余额。"""
        return [
            (m["type"], m["quantity"], m["balance"]) for m in payload["movements"]
        ]

    def assert_movement_ids_unique_ascending(self, payload):
        ids = [m["id"] for m in payload["movements"]]
        self.assertEqual(len(ids), len(set(ids)), "流水编号必须唯一")
        self.assertEqual(ids, sorted(ids), "流水必须按编号升序返回")
        self.assertTrue(all(isinstance(i, int) for i in ids))


class IssueQuantityBoundaryTests(InventoryCliTestBase):
    def test_initial_product_is_zero_with_empty_movements(self):
        self.add_product()

        payload = self.show()
        self.assertEqual(payload["quantity"], 0)
        self.assertEqual(payload["movements"], [])

    def test_receive_ten_issue_three_leaves_seven_with_two_movements(self):
        self.add_product()
        self.assertEqual(self.receive(10)["quantity"], 10)
        self.assertEqual(self.issue_ok(3)["quantity"], 7)

        payload = self.show()
        self.assertEqual(payload["quantity"], 7)
        self.assertEqual(
            self.movement_triples(payload),
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        self.assert_movement_ids_unique_ascending(payload)

    def test_issue_more_than_balance_is_rejected_and_state_persists_unchanged(self):
        self.add_product()
        self.receive(10)
        self.issue_ok(3)

        before = self.show()

        result = self.run_cli("issue", "--sku", SKU, "--qty", "8")
        self.assert_error_exit_2(result, "超过当前余额")

        after = self.show()
        self.assertEqual(after, before)
        self.assertEqual(after["quantity"], 7)
        self.assertEqual(
            self.movement_triples(after),
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        # 已有流水编号在失败后保持不变。
        self.assertEqual([m["id"] for m in after["movements"]],
                         [m["id"] for m in before["movements"]])

    def test_issue_exact_balance_succeeds_and_appends_zero_balance_movement(self):
        self.add_product()
        self.receive(10)
        self.issue_ok(3)
        before = self.show()

        payload = self.issue_ok(7)
        self.assertEqual(payload["quantity"], 0)

        after = self.show()
        self.assertEqual(after["quantity"], 0)
        # 仅新增一条数量为 7、操作后余额为 0 的出库流水。
        self.assertEqual(len(after["movements"]), len(before["movements"]) + 1)
        self.assertEqual(
            self.movement_triples(after),
            [("receive", 10, 10), ("issue", 3, 7), ("issue", 7, 0)],
        )
        new_movement = after["movements"][-1]
        self.assertEqual(new_movement["type"], "issue")
        self.assertEqual(new_movement["quantity"], 7)
        self.assertEqual(new_movement["balance"], 0)
        self.assertGreater(new_movement["id"], before["movements"][-1]["id"])
        self.assert_movement_ids_unique_ascending(after)

    def test_issue_at_zero_balance_is_rejected_and_state_unchanged(self):
        self.add_product()
        self.receive(10)
        self.issue_ok(3)
        self.issue_ok(7)

        before = self.show()
        self.assertEqual(before["quantity"], 0)

        result = self.run_cli("issue", "--sku", SKU, "--qty", "1")
        self.assert_error_exit_2(result, "超过当前余额")

        after = self.show()
        self.assertEqual(after, before)
        self.assertEqual(after["quantity"], 0)
        self.assertEqual(
            self.movement_triples(after),
            [("receive", 10, 10), ("issue", 3, 7), ("issue", 7, 0)],
        )

    def test_invalid_quantities_are_rejected_without_state_change(self):
        self.add_product()
        self.receive(10)
        self.issue_ok(3)
        baseline = self.show()

        for invalid_qty in ("0", "-1", "1.5", "abc"):
            with self.subTest(qty=invalid_qty):
                result = self.run_cli(
                    "issue", "--sku", SKU, "--qty", invalid_qty
                )
                self.assert_error_exit_2(result, "数量")

                after = self.show()
                self.assertEqual(after, baseline)
                self.assertEqual(after["quantity"], 7)
                self.assertEqual(
                    self.movement_triples(after),
                    [("receive", 10, 10), ("issue", 3, 7)],
                )

    def test_repeated_shows_return_identical_state_and_write_nothing(self):
        self.add_product()
        self.receive(10)
        self.issue_ok(3)

        first = self.show()
        second = self.show()
        self.assertEqual(second, first)
        self.assert_movement_ids_unique_ascending(second)


if __name__ == "__main__":
    unittest.main()
