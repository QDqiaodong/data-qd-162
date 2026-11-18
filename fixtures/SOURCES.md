本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- `input.json`：v1 逐件计费样例（schema_version 1）。
- `plan_input.json`：v2 批量托运分配样例（schema_version 2），含三条 dispatch rule、三件包裹与多候选服务，用于演示候选证据、组账单与最低收费/燃油的整组计算。
- `temporal_input.json`：v3 费率修订权威时间线自制样例（schema_version 3）。ground/Z1 上 A→B 与 A→C 两条 supersedes 链在 [60,75) 形成 B/C 同价分叉歧义，B 在 [30,90) 把围长上限收紧到 900 使时点 50 的运单 oversize；air/Z9 的修订只在 [0,40) 可见，之后为无记录 missing 段。样例同时覆盖 resolved/ambiguous/missing/oversize 四种状态、半开端点与规范合段，结果为 rated=3、oversize=1、missing=1、ambiguous=1、`settled_total_cents=600`、`total_cents=null`。
