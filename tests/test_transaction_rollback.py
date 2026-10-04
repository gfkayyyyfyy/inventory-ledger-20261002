"""事务回滚回归测试：余额更新与流水写入同时生效或同时不生效。

README 业务规则承诺：每次入库/出库的余额更新与流水写入在同一事务内
同时生效或同时不生效。本文件针对商品已存在、数量合法且库存充足的
正常操作（不是数量非法或出库超量的提前拒绝），模拟数据库在
“余额已更新、流水尚未成功写入”的中途抛出 sqlite3.OperationalError，
以现有业务入口验证：

- 失败操作退出码 1，标准输出为空，标准错误包含数据库写入失败原因
  且不出现异常堆栈；
- 事务整体回滚：重新打开同一数据库后，商品名称、数量与全部流水的
  编号、类型、数量、余额逐项不变，对照商品同样不变，不存在仅余额
  变动或新增失败流水的状态；
- 解除失败条件后在同一台账重试同一操作成功：退出码 0，返回原有
  商品 JSON，只新增一条类型、数量与操作后余额正确的流水，新编号
  大于既有编号（不要求连续）；
- 再次重开后仍能查询到成功结果，查询本身不新增流水。

失败条件由测试在内存中包装 sqlite3 连接注入（仅对流水 INSERT 抛错），
不修改产品代码，也不作为台账设置保存。每个用例使用独立临时数据库，
仅依赖 Python 标准库，重复执行互不影响。

从项目根目录执行：

    python -m unittest discover
"""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inventory import cli
from inventory import storage

# 注入失败时使用的模拟原因，会出现在 DatabaseError 的提示中。
FAILURE_REASON = "模拟流水写入失败"


class _FailOnMovementInsert:
    """包装真实连接：仅在流水 INSERT 时抛 OperationalError，其余全部委托。

    产品代码 move() 在同一事务内先 UPDATE 余额、再 INSERT 流水；本包装
    让 UPDATE 正常执行、INSERT 失败，从而复现“余额更新后、流水写入前”
    的中途故障。包装只存在于测试进程内存中，不写入台账。
    """

    def __init__(self, real):
        self._real = real

    def execute(self, sql, parameters=()):
        if sql.lstrip().upper().startswith("INSERT INTO MOVEMENTS"):
            raise sqlite3.OperationalError(FAILURE_REASON)
        return self._real.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


class TransactionRollbackTestCase(unittest.TestCase):
    """每个用例使用独立的临时数据库，互不依赖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")

    def run_cli(self, *args):
        """以 README 公开的命令行入口执行，返回 (退出码, stdout, stderr)。

        每次调用都重新打开并在结束时关闭同一数据库文件，等价于
        重新启动一次 `python -m inventory --db ...`。
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            code = cli.run(["--db", self.db_path, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def run_cli_failing_movement_insert(self, *args):
        """同上，但本次调用在流水 INSERT 时抛 sqlite3.OperationalError。"""
        real_connect = sqlite3.connect

        def flaky_connect(path):
            return _FailOnMovementInsert(real_connect(path))

        with mock.patch.object(storage.sqlite3, "connect", new=flaky_connect):
            return self.run_cli(*args)

    def run_ok(self, *args):
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"命令 {args} 应成功，stderr: {err}")
        self.assertEqual(err, "")
        return json.loads(out)

    def show(self, sku):
        return self.run_ok("show", "--sku", sku)

    def seed(self):
        """DEMO-1 演示螺母入库 10 再出库 3（数量 7、两条流水）；DEMO-2 入库 4 对照。"""
        self.run_ok("add", "--sku", "DEMO-1", "--name", "演示螺母")
        self.run_ok("receive", "--sku", "DEMO-1", "--qty", "10")
        self.run_ok("issue", "--sku", "DEMO-1", "--qty", "3")
        self.run_ok("add", "--sku", "DEMO-2", "--name", "对照螺丝")
        self.run_ok("receive", "--sku", "DEMO-2", "--qty", "4")

    def check_rollback_scenario(self, command, expected_quantity):
        """receive/issue 共用场景：中途失败整体回滚，解除后重试成功。"""
        self.seed()

        # 预备状态：DEMO-1 数量 7，receive 10 / issue 3 两条原始流水。
        before1 = self.show("DEMO-1")
        self.assertEqual(before1["name"], "演示螺母")
        self.assertEqual(before1["quantity"], 7)
        self.assertEqual(len(before1["movements"]), 2)
        self.assertEqual(
            [
                (m["type"], m["quantity"], m["balance"])
                for m in before1["movements"]
            ],
            [("receive", 10, 10), ("issue", 3, 7)],
        )
        self.assertLess(
            before1["movements"][0]["id"], before1["movements"][1]["id"]
        )
        # 对照商品 DEMO-2：数量 4，一条入库流水。
        before2 = self.show("DEMO-2")
        self.assertEqual(before2["quantity"], 4)
        self.assertEqual(
            [
                (m["type"], m["quantity"], m["balance"])
                for m in before2["movements"]
            ],
            [("receive", 4, 4)],
        )

        # 商品存在、数量合法且库存充足，但流水写入中途失败。
        code, out, err = self.run_cli_failing_movement_insert(
            command, "--sku", "DEMO-1", "--qty", "2"
        )
        self.assertEqual(code, 1, "数据库写入失败应以退出码 1 结束")
        self.assertEqual(out, "", "失败时标准输出应为空")
        self.assertIn("写入数据库失败", err)
        self.assertIn(FAILURE_REASON, err, "标准错误应包含数据库写入失败原因")
        self.assertNotIn("Traceback", err, "不应输出异常堆栈")

        # 重新打开同一数据库：余额与流水同时不生效，逐项与之前一致，
        # 不存在仅余额变动或新增失败流水的状态；对照商品也不受影响。
        self.assertEqual(self.show("DEMO-1"), before1)
        self.assertEqual(self.show("DEMO-2"), before2)

        # 解除失败条件后，在同一台账重试同一操作成功。
        payload = self.run_ok(command, "--sku", "DEMO-1", "--qty", "2")
        self.assertEqual(
            payload,
            {"sku": "DEMO-1", "name": "演示螺母", "quantity": expected_quantity},
        )

        # 重开后能查到成功结果：原有流水逐项不变，只新增一条流水。
        after1 = self.show("DEMO-1")
        self.assertEqual(after1["name"], "演示螺母")
        self.assertEqual(after1["quantity"], expected_quantity)
        self.assertEqual(len(after1["movements"]), 3)
        self.assertEqual(after1["movements"][:2], before1["movements"])
        new_movement = after1["movements"][2]
        self.assertGreater(
            new_movement["id"],
            before1["movements"][-1]["id"],
            "新流水编号应大于既有编号（不要求连续）",
        )
        self.assertEqual(new_movement["type"], command)
        self.assertEqual(new_movement["quantity"], 2)
        self.assertEqual(new_movement["balance"], expected_quantity)
        # 对照商品保持原样。
        self.assertEqual(self.show("DEMO-2"), before2)

        # 再次重开查询结果一致，查询本身不新增流水。
        self.assertEqual(self.show("DEMO-1"), after1)


class TestReceiveRollback(TransactionRollbackTestCase):
    """入库：余额更新后、流水写入前遭遇 sqlite3.OperationalError。"""

    def test_receive_rolls_back_then_retry_succeeds(self):
        # 失败重试后：7 + 2 = 9。
        self.check_rollback_scenario("receive", 9)


class TestIssueRollback(TransactionRollbackTestCase):
    """出库：余额更新后、流水写入前遭遇 sqlite3.OperationalError。"""

    def test_issue_rolls_back_then_retry_succeeds(self):
        # 失败重试后：7 - 2 = 5。
        self.check_rollback_scenario("issue", 5)


if __name__ == "__main__":
    unittest.main()
