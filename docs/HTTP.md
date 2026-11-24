# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 召回停发登记台（前缀 `/api/recall`）

- `GET /api/recall/board`：当前停发名单、待核事项、召回事件与各批次出货状态。
- `POST /api/recall/lots`：登记一条批次/合批记录，字段 `lot_id`、`parent_lot`、`qty_in`、`qty_out`、`loss_qty`；返回是否变化与重算结果。
- `POST /api/recall/recalls`：登记召回，字段 `recall_id`、`problem_lot`、可选 `event_id`、`reason`；相同 `recall_id` 重试幂等。
- `POST /api/recall/recalls/{recall_id}/close`：解除一起召回，仅释放该召回对应的停发行。
