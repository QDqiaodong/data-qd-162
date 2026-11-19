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
# v3 费率修订权威时间线与时点核算（schema_version 3；--temporal 与 --plan 互斥）
.venv/bin/parcel-rating --temporal --source fixtures/temporal_input.json --output run/temporal_report.json
# v4 外部账单闭合核销与身份歧义证据（schema_version 4；--reconcile 与 --plan/--temporal 两两互斥）
.venv/bin/parcel-rating --reconcile --source fixtures/reconcile_input.json --output run/reconcile_report.json
.venv/bin/python -m unittest discover -s tests -v
```

修改源码后重新安装。v1 样例应得到 `rated_count=2`、`oversize_count=0`、`total_cents=1742`。
v2 样例 `fixtures/plan_input.json` 对三件包裹、三条 dispatch rule 给出完整候选证据、选中分配与组账单（当前样例总价 `total_cents=3370`）。Python 接口为 `parcel_rating.model.plan(data)`，其返回结构与 CLI 的 `result` 完全一致。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

## 费率修订权威时间线（v3 要点）

时间线模式顶层恰为 `revisions`、`shipments`、`as_of_seq`、`window_start`、`window_end`；时间坐标为 `0..10^15` 整数且拒绝 bool，窗口半开 `[window_start,window_end)`。每条修订有全局唯一 `revision_id`、`known_seq`、半开生效区间 `[start,end)`、完整独立 `tariff`（沿用全部 v1 费率字段与校验）和 `supersedes` 父 ID 列表（全请求至多 20000 条边）。父记录必须存在、同 service/zone 且 `known_seq` 严格更小；`supersedes` 只表达权威优先关系，不删除父记录的区间，允许多父合并与独立分叉。可见性只由 `known_seq<=as_of_seq` 决定；祖先闭包在可见修订的完整图上、任何区间裁剪之前计算，因此生效区间不覆盖查询时点（甚至完全在窗口外）的中间节点仍然传递权威。

某时点的 frontier 是覆盖该时点、且不被另一覆盖修订经一条或多条 supersedes 边支配的全部记录，按 revision_id 的 Unicode 码点升序列出：空为 `missing`、单项为 `resolved`、多项为 `ambiguous`——即使费率载荷完全相同也不得按金额或 ID 消歧。每个在 revisions 或 shipments 中出现过的 `(service,zone)` 都得到覆盖整个窗口、无缝无重叠的连续时间线；相邻段仅在 status 与 frontier 完全相同时合并（被支配记录进出覆盖而 frontier 不变时合并；金额相同但身份不同不合并）。运单复用其 `shipped_at` 所处段的同一 frontier，resolved 才按 v1 逐件规则计费，超限为 oversize（已确定、不收费），missing/ambiguous 保留证据且不计费，绝不回退到最早、最新或最便宜费率。四类计数之和等于运单数；只要存在 missing/ambiguous，`total_cents=null`，同时给出已核定部分 `settled_total_cents`；歧义只存在于时间线而无运单落入时不影响账单完整性，空运单账单为 0。

实现按 key 维护排序边界、以整数位掩码一次性计算祖先闭包、按边界事件（绝不逐时间单位枚举）求覆盖集与 frontier，并让每件运单经二分查找复用缓存时间线（绝不为运单重遍历修订图），满足 500 修订/20000 边/10000 运单与 `0..10^15` 坐标。字段、半开边界、规范合段、跨窗口端点与共享 frontier 语义的完整义务见 [数据契约](docs/contract.md)。Python 接口 `parcel_rating.model.temporal(data)` 直接返回 result 且不修改入参；测试含独立小规模逐点/逐对 DFS 可达预言机、联动验收、全规模性能与输入重排不变性。

## 外部账单闭合核销（v4 要点）

核销模式顶层恰为 `rating`、`debits`、`credits`。`rating` 完整沿用 v1 `analyze` 的 tariffs/shipments、字段与数值校验、重复 ID/费率、布尔与缺费率错误，仅把 shipments 上限收窄为 12；同一次既有逐件计费结果中 `status=rated` 的运单即只读应付身份，金额固定取 `total_cents`（rated 且为 0 也是应付，不得省略），`oversize` 运单不产生应付并单列排除身份，绝不能当作零元应付或被借项引用。不向 rating 注入外部账单金额，也不为求闭合调整任何运单、服务或费率；`reconcile(data)` 不修改调用方 `data`。

借项至多 12 条（`debit_id` 唯一、`gross_cents` 为 `0..10^15` 整数、`shipment_candidates` 至多 12 个无重复且必须引用本次 rated 运单），贷项至多 14 条（`credit_id` 唯一、`cents` 为 `1..10^15` 整数、`debit_candidates` 至多 2 个无重复且必须引用声明借项）；两类 ID 为独立命名空间，拒绝 bool/float/字符串金额，候选数组允许为空（合法但无关联可选）。引用不存在或 oversize 身份、重复候选、未知/缺失字段、超上限均为非法输入（退出 2 并保留旧报告）；应付数与借项数不等、总额不等或无完整解释是业务不闭合而非格式错误，仍输出完整报告并退出 0。

一个完整核销解释须联合给出：每张贷项整体、仅一次地冲抵恰一个声明借项（不得拆分/遗漏/复用，借项净额不得为负，可接收多张贷项），以及每条借项到一个声明 rated 运单的双射（每个应付恰被覆盖一次），且借项净额精确等于所配运单的既有 `total_cents`（零净额只配零元应付）。全部为精确整数等式，无容差、无浮点、无按总额补平、无丢弃异常行。贷项归属改变净额从而改变可配运单，唯一覆盖约束又反过来排除某些归属，因此必须联合判断，不能把一个合法解释当成唯一事实。

报告 `schema_version=4`，result 恰含 `expected_shipments`（按 id 排序的只读应付）、`excluded_oversize_ids`、`expected_total_cents`、`net_debit_cents`（两者始终输出，聚合不受单条上限约束）、`reconciled`、`debit_support`、`credit_support`。闭合时每行给出其在**至少一个完整解释中**实际关联的全部身份（Unicode 码点升序、无重复、不含仅局部可行的边），单元素 `forced`、多元素 `ambiguous`；不同行的支持并集不能交叉拼接成不存在的联合见证。不闭合时所有支持数组为空且 `status=blocked`，但应付、排除身份与两个总额仍完整返回，绝不给部分闭合或自选近似解。空 rating/借项/贷项是闭合零账单；只有借项或只有贷项按同一完整解释条件判断。

算法不按金额单位展开、不枚举运单排列、不物化全部解释：枚举有界整张贷项归属（每条至多 2 候选、至多 `2^14`），按整数净额剪去过冲并合并相同净额向量；对每个净额向量把精确等式化为至多 12 节点的二分图完美匹配，用增广路判存在、用残差图强连通分量一次性求出“出现在某个完美匹配中”的全部边（线性、无逐边重解）。每条支持边都由同一联合解释作证，且因遍历了全部可行归属与全部允许边而无遗漏。测试含不调用生产求解函数的独立小规模穷举预言机（自算计费、枚举全部贷项归属与运单双射后投影支持），覆盖总额相等但唯一覆盖失败、信用过冲、无贷项、零元 rated、oversize 排除、空候选、极大金额、单侧歧义、同额多身份与数组/候选重排，并有 12 运单/12 借项/14 贷项真实规模案例。完整义务见 [数据契约](docs/contract.md)。

## 批量分配要点

计划模式顶层恰为 `tariffs`、`shipments`、`dispatch_rules`；dispatch rule 至多 4 条，shipment 至多 12 件，候选 `services` 非空且不重复，每个 `(service, zone)` 必须同时存在费率与规则。围长超限只令该候选 `oversize`，不提前丢弃包裹。组容量按成员**计费重之和**与件数约束；非空组按 `B=max(S,minimum_base_cents)` 计最低收费，燃油在组层面只 half-up 取整一次，保险逐件求和。求解器对每条规则枚举全部容量可行子集并做备忘录搜索，穷举所有合法分配以保证全局最优；同价依次按更少非空组、包裹 ID Unicode 升序构造的 service 向量字典序最小决胜。无完整可行分配时 `feasible=false`、`total_cents=null`、退出码仍为 0；非法输入退出 2。算法、字段与可重算性的完整说明见 [数据契约](docs/contract.md)。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加，输入数组重排不改变规范化 `result`（SHA256 仍对应当次原始字节）。测试使用临时目录，运行产物在忽略的run/。

src/parcel_rating/model.py 为业务模型门面（`analyze` 逐件计费、`plan` 批量分配、`temporal` 转发到时间线实现并复用同一套 v1 逐件规则、`reconcile` 转发到核销实现并以 v1 `analyze` 为只读应付基准），temporal.py 实现 v3 权威修订时间线，reconcile.py 实现 v4 外部账单闭合核销与身份支持证据，common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路（`--plan`/`--temporal`/`--reconcile` 两两互斥，schema_version 1/2/3/4）。fixtures 为明确自制样例，代码与样例采用MIT许可。
