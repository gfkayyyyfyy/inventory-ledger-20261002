"""余额更新与流水写入“同生共死”的事务回滚回归测试。

README 承诺：每次入库/出库的余额（products.quantity）更新与流水
（movements）写入在同一事务内同时生效或同时不生效，任何失败都不留下
“只改了余额”或“多了一条失败流水”的中间状态。

本模块以 README 公开的命令入口（`python -m inventory --db ...`）为验收
对象，在真实 SQLite 临时台账上模拟“余额 UPDATE 已执行、流水 INSERT
尚未成功”时遭遇 sqlite3.OperationalError 的中途写入失败，验证：

- 失败操作退出码 1、stdout 为空、stderr 只含数据库写入失败原因而无异常堆栈；
- 重新打开同一台账，商品名称、数量与全部流水的编号、类型、数量、余额逐项
  不变，对照商品也保持原样（事务整体回滚，而非数量非法或出库超量的提前
  拒绝——后者退出码为 2 且发生在任何写入之前）；
- 解除失败条件后，经同一业务入口重试同一操作成功：退出码 0、返回原有
  商品 JSON，只新增一条数量 2、余额正确的流水，新编号大于既有编号
  （不要求连续）；再次重开仍能查到，查询本身不增加流水。

故障注入只存在于测试中：在一个随临时目录删除的启动脚本内，把子进程里的
inventory.storage.sqlite3.connect 替换为连接代理工厂，代理把全部调用转发
给真实 sqlite3.Connection，仅让本次出入库事务内的 movements INSERT 抛出
一次 sqlite3.OperationalError。失败条件不写入台账、不保存为任何设置；
参数解析、商品查找、余额计算、事务边界与错误处理全部由现有产品代码执行，
产品代码本身不做任何修改。

每个用例使用独立临时目录与台账，仅依赖 Python 标准库，可重复执行：

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
CONTROL_SKU = "DEMO-2"
CONTROL_NAME = "演示垫片"

# 测试专用启动脚本：通过连接代理在流水 INSERT 处注入一次
# sqlite3.OperationalError。__PROJECT_ROOT__ 在写入临时文件时替换为项目
# 根目录，保证脚本从任何工作目录运行都能 import inventory。
FAULT_RUNNER_TEMPLATE = '''\
"""测试专用脚本：模拟余额更新后流水写入失败，随临时目录一起删除。"""
import runpy
import sys

sys.path.insert(0, "__PROJECT_ROOT__")

import inventory.storage as storage

_real_connect = storage.sqlite3.connect


class InjectedMovementWriteFailure(storage.sqlite3.OperationalError):
    """仅用于测试：流水写入途中的数据库故障。"""


class FaultConnection:
    """把调用转发给真实连接，仅让本事务的 movements INSERT 失败一次。"""

    def __init__(self, real):
        self._real = real
        self._fault_pending = True

    def execute(self, sql, params=None):
        if self._fault_pending and sql.lstrip().upper().startswith(
            "INSERT INTO MOVEMENTS"
        ):
            # 此刻 products 的余额 UPDATE 已在同一事务内执行完毕，
            # 而流水尚未写入；抛出 OperationalError 后，由产品代码的
            # 事务回滚（with self.conn）与数据库错误处理接管。
            self._fault_pending = False
            raise InjectedMovementWriteFailure(
                "injected fault: movements insert failed after balance update"
            )
        if params is None:
            return self._real.execute(sql)
        return self._real.execute(sql, params)

    def executescript(self, script):
        return self._real.executescript(script)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()

    def __enter__(self):
        # sqlite3 连接的上下文管理器负责提交/回滚，不关闭连接；
        # 原样委托给真实连接，保证产品代码的事务语义不被代理改变。
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def _connect_with_fault(*args, **kwargs):
    return FaultConnection(_real_connect(*args, **kwargs))


# 仅替换本测试进程内 storage 模块看到的 connect；台账文件不记录任何状态。
storage.sqlite3.connect = _connect_with_fault

try:
    runpy.run_module("inventory", run_name="__main__")
except SystemExit as exc:
    sys.exit(exc.code)
'''


def movement_rows(payload):
    """提取完整流水 (id, type, quantity, balance)，逐项比对用。"""
    return [
        (m["id"], m["type"], m["quantity"], m["balance"])
        for m in payload["movements"]
    ]


class TransactionRollbackTestCase(unittest.TestCase):
    """每个用例使用独立临时目录、临时台账与临时故障脚本，互不影响。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "ledger.db")
        # 故障脚本只存在于临时目录，运行结束即删除，不接触仓库内文件。
        self.runner_path = Path(self._tmp.name) / "fault_runner.py"
        self.runner_path.write_text(
            FAULT_RUNNER_TEMPLATE.replace(
                "__PROJECT_ROOT__", str(PROJECT_ROOT)
            ),
            encoding="utf-8",
        )

    def run_cli(self, *args):
        """经 README 公开的普通入口运行一条命令。"""
        proc = subprocess.run(
            [sys.executable, "-m", "inventory", "--db", self.db_path, *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run_cli_with_movement_fault(self, *args):
        """经同一业务入口运行，但在流水 INSERT 处注入一次中途写失败。"""
        proc = subprocess.run(
            [sys.executable, str(self.runner_path), "--db", self.db_path, *args],
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
        """用全新进程重新打开同一台账查询（show 为只读，不产生流水）。"""
        return self.run_ok("show", "--sku", sku)

    def seed_ledger(self):
        """登记 DEMO-1：演示螺母，入库 10 再出库 3；另登记 DEMO-2 入库 4。

        返回两者失败发生前的完整 show 快照。此时 DEMO-1 数量合法（2 在
        1 至上限内）且库存足够（余额 7），后续失败只能来自数据库中途
        写入错误，不可能是数量非法或出库超量的提前拒绝。
        """
        self.assertEqual(
            self.run_ok("add", "--sku", SKU, "--name", NAME),
            {"sku": SKU, "name": NAME, "quantity": 0},
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", SKU, "--qty", "10"),
            {"sku": SKU, "name": NAME, "quantity": 10},
        )
        self.assertEqual(
            self.run_ok("issue", "--sku", SKU, "--qty", "3"),
            {"sku": SKU, "name": NAME, "quantity": 7},
        )
        self.assertEqual(
            self.run_ok("add", "--sku", CONTROL_SKU, "--name", CONTROL_NAME),
            {"sku": CONTROL_SKU, "name": CONTROL_NAME, "quantity": 0},
        )
        self.assertEqual(
            self.run_ok("receive", "--sku", CONTROL_SKU, "--qty", "4"),
            {"sku": CONTROL_SKU, "name": CONTROL_NAME, "quantity": 4},
        )

        demo1 = self.show(SKU)
        self.assertEqual(demo1["sku"], SKU)
        self.assertEqual(demo1["name"], NAME)
        self.assertEqual(demo1["quantity"], 7)
        self.assertEqual(
            movement_rows(demo1),
            [(1, "receive", 10, 10), (2, "issue", 3, 7)],
        )

        demo2 = self.show(CONTROL_SKU)
        self.assertEqual(demo2["name"], CONTROL_NAME)
        self.assertEqual(demo2["quantity"], 4)
        self.assertEqual(movement_rows(demo2), [(3, "receive", 4, 4)])
        return demo1, demo2

    def assert_ledger_unchanged(self, demo1_before, demo2_before):
        """失败后用新进程重开台账：两个商品的名称、数量与全部流水逐项不变。"""
        demo1 = self.show(SKU)
        demo2 = self.show(CONTROL_SKU)
        # DEMO-1：名称与数量仍是原值。
        self.assertEqual(demo1["sku"], SKU)
        self.assertEqual(demo1["name"], NAME)
        self.assertEqual(demo1["quantity"], demo1_before["quantity"])
        # 全部流水的编号、类型、数量和余额逐项不变（含 id，不重排不重算）。
        self.assertEqual(
            [m["id"] for m in demo1["movements"]],
            [m["id"] for m in demo1_before["movements"]],
        )
        self.assertEqual(movement_rows(demo1), movement_rows(demo1_before))
        # 整体快照同样一致：不能出现仅余额变动或新增失败流水的状态。
        self.assertEqual(demo1, demo1_before)
        # 对照商品 DEMO-2 的数量与流水保持原样。
        self.assertEqual(demo2, demo2_before)

    def run_rollback_and_retry_scenario(self, mtype, new_quantity, new_balance):
        demo1_before, demo2_before = self.seed_ledger()
        previous_max_id = max(
            m["id"]
            for m in demo1_before["movements"] + demo2_before["movements"]
        )

        # 一、余额更新后、流水写入时遭遇 OperationalError。
        code, out, err = self.run_cli_with_movement_fault(
            mtype, "--sku", SKU, "--qty", "2"
        )
        # 数据库读写错误：退出码 1（区别于业务/参数错误的退出码 2），
        # 说明这是写入中途失败而不是提前拒绝。
        self.assertEqual(code, 1, f"{mtype} 中途写失败应以退出码 1 结束")
        self.assertEqual(out, "", "写失败时 stdout 应为空")
        self.assertIn("写入数据库失败", err, "stderr 应包含数据库写入失败原因")
        self.assertNotIn("Traceback", err, "stderr 不应包含异常堆栈")

        # 二、重开同一台账：余额与流水随事务整体回滚，对照商品不受影响。
        self.assert_ledger_unchanged(demo1_before, demo2_before)

        # 三、解除失败条件（改用不带故障注入的普通入口），在同一台账
        # 重试完全相同的操作：成功并只留下一条新流水。
        payload = self.run_ok(mtype, "--sku", SKU, "--qty", "2")
        self.assertEqual(
            payload,
            {"sku": SKU, "name": NAME, "quantity": new_quantity},
        )

        demo1 = self.show(SKU)
        self.assertEqual(demo1["name"], NAME)
        self.assertEqual(demo1["quantity"], new_quantity)
        # 原有流水（编号、类型、数量、余额）完整保留在前面。
        old_movements = demo1_before["movements"]
        self.assertEqual(
            demo1["movements"][: len(old_movements)], old_movements
        )
        # 仅新增一条对应类型、数量 2、操作后余额正确的流水。
        new_movements = demo1["movements"][len(old_movements):]
        self.assertEqual(len(new_movements), 1)
        created = new_movements[0]
        self.assertEqual(
            (created["type"], created["quantity"], created["balance"]),
            (mtype, 2, new_balance),
        )
        # 新编号大于既有编号即可，不要求连续（失败事务可能影响编号序列）。
        self.assertGreater(created["id"], previous_max_id)
        ids = [m["id"] for m in demo1["movements"]]
        self.assertEqual(len(ids), len(set(ids)), "流水编号应唯一")
        self.assertEqual(ids, sorted(ids), "流水应按编号升序返回")
        # 对照商品在成功操作后依然不变。
        self.assertEqual(self.show(CONTROL_SKU), demo2_before)

        # 四、再次重新打开台账，成功结果已持久化；重复查询不增加流水。
        reopened = self.show(SKU)
        self.assertEqual(reopened, demo1)
        queried_again = self.show(SKU)
        self.assertEqual(queried_again, reopened)
        self.assertEqual(
            len(queried_again["movements"]), len(demo1["movements"])
        )
        self.assertEqual(self.show(CONTROL_SKU), demo2_before)


class TestReceiveAtomicRollback(TransactionRollbackTestCase):
    """入库：7 + 2 时流水写入失败 → 回滚仍为 7；重试成功后为 9。"""

    def test_receive_balance_update_rolls_back_when_movement_insert_fails(self):
        self.run_rollback_and_retry_scenario(
            "receive", new_quantity=9, new_balance=9
        )


class TestIssueAtomicRollback(TransactionRollbackTestCase):
    """出库：7 - 2 时流水写入失败 → 回滚仍为 7；重试成功后为 5。"""

    def test_issue_balance_update_rolls_back_when_movement_insert_fails(self):
        self.run_rollback_and_retry_scenario(
            "issue", new_quantity=5, new_balance=5
        )


if __name__ == "__main__":
    unittest.main()
