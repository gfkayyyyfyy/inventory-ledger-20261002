# 小型仓储库存台账

建设面向小型仓库的本地库存管理产品，逐步覆盖商品与 SKU、库位、入库出库、库存调整、盘点差异、操作流水、低库存清单和 CSV 交换。

计划采用：Python 3 标准库 / sqlite3 / csv / argparse。

当前已实现：单仓库、整数数量的本地库存台账，无需第三方包或联网。库位、盘点、调整、低库存及 CSV 交换暂不在范围内。

## 使用方式

通过 `python -m inventory --db 文件路径` 访问台账。数据库父目录需事先存在；指定文件不存在时自动建立台账，后续命令使用同一路径读写，数据持久保存。

```bash
# 登记商品（初始数量为零）
python -m inventory --db ./ledger.db add --sku DEMO-1 --name 演示螺母

# 入库 / 出库
python -m inventory --db ./ledger.db receive --sku DEMO-1 --qty 10
python -m inventory --db ./ledger.db issue   --sku DEMO-1 --qty 3

# 查询商品名称、当前数量与完整流水
python -m inventory --db ./ledger.db show --sku DEMO-1
```

### 输出约定

- 成功：退出码 0，标准输出为单个 JSON 对象。
  - `add`/`receive`/`issue`：`{"sku", "name", "quantity"}`
  - `show`：额外包含 `movements` 数组，按流水编号升序；流水对象为 `{"id", "type", "quantity", "balance"}`，无流水时为 `[]`。
- 参数或业务错误（空 SKU/名称、重复登记、商品不存在、无效数量、出库超过余额）：退出码 2，原因写入标准错误，标准输出为空，数据不改变。
- 数据库无法打开或读写：退出码 1，原因写入标准错误。

### 业务规则

- SKU 与名称去掉两端空白后不能为空；所有命令按去空白后的 SKU 精确查找，SKU 区分大小写。
- 数量只接受大于零的整数（拒绝零、负数、小数、非数字）。
- 每次入库/出库新增一条流水，记录唯一递增编号、类型（`receive`/`issue`）、本次正整数数量和操作后余额；余额更新与流水写入在同一事务内同时生效或同时不生效。登记和查询不产生流水。
- 出库数量等于余额时允许成功，余额变为零。
- 不同数据库文件的数据各自独立。

## 测试

在项目根目录执行：

```bash
python -m unittest discover
```

测试以 `python -m inventory` 命令为入口，每个用例使用独立临时目录中的真实 SQLite 文件，结束后自动清理，不依赖也不会触碰已有台账。全部通过时输出：

```text
.......
----------------------------------------------------------------------
Ran 7 tests in <用时>

OK
```
