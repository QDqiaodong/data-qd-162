# data-qd-162 · 包裹费率与附加费核算

Dimensional-weight parcel rating with exact tariff and surcharge evidence。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；建议 Python 3.12。使用 uv 建立独立环境，构建工具锁版本。首次安装无公共缓存时需要访问包索引；业务运行不访问网络。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
# v1 逐件计费（schema_version 1，入口与结果保持不变）
.venv/bin/parcel-rating --source fixtures/input.json --output run/report.json
# v2 批量托运分配（schema_version 2）
.venv/bin/parcel-rating --plan --source fixtures/plan_input.json --output run/plan_report.json
.venv/bin/python -m unittest discover -s tests -v
```

修改源码后重新安装。v1 样例应得到 `rated_count=2`、`oversize_count=0`、`total_cents=1742`。
v2 样例 `fixtures/plan_input.json` 对三件包裹、三条 dispatch rule 给出完整候选证据、选中分配与组账单（当前样例总价 `total_cents=3370`）。Python 接口为 `parcel_rating.model.plan(data)`，其返回结构与 CLI 的 `result` 完全一致。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

## 批量分配要点

计划模式顶层恰为 `tariffs`、`shipments`、`dispatch_rules`；dispatch rule 至多 4 条，shipment 至多 12 件，候选 `services` 非空且不重复，每个 `(service, zone)` 必须同时存在费率与规则。围长超限只令该候选 `oversize`，不提前丢弃包裹。组容量按成员**计费重之和**与件数约束；非空组按 `B=max(S,minimum_base_cents)` 计最低收费，燃油在组层面只 half-up 取整一次，保险逐件求和。求解器对每条规则枚举全部容量可行子集并做备忘录搜索，穷举所有合法分配以保证全局最优；同价依次按更少非空组、包裹 ID Unicode 升序构造的 service 向量字典序最小决胜。无完整可行分配时 `feasible=false`、`total_cents=null`、退出码仍为 0；非法输入退出 2。算法、字段与可重算性的完整说明见 [数据契约](docs/contract.md)。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加，输入数组重排不改变规范化 `result`（SHA256 仍对应当次原始字节）。测试使用临时目录，运行产物在忽略的run/。

src/parcel_rating/model.py 为业务模型（`analyze` 逐件计费、`plan` 批量分配），common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。
