# 公网审计：筛选、分页、按条删除

状态：待实现。本文件只是计划，不要把它当成已经落地的行为。

来源：oMLX Gateway 控制台已经按这个行为做完，并在本机验证过。StrataLab 只补「公网」页的审计查看和删除，不把 oMLX Gateway 的其它差异带过来。

## 要解决的问题

「公网」页现在只有两件事：按 API Key 看最近 100 条，以及「清空审计与用量」。

- 记录一多就看不到更早的请求。
- 模型、错误原文挤在单元格的 `title` 里，表格上看不到。
- 删除只有整表清空。清空会连 `gateway-state.json` 里的今日/累计请求、tokens、费用一起归零。
- 详情弹窗宽度停在 `min(1100px, 96vw)`，列变多以后请求正文比页面窄。

## 行为

### 查看

筛选放在现有 Key 下拉旁边，点「筛选」或改变下拉后从第 1 页重新查：

| 控件 | 取值 |
|---|---|
| API Key | 现有 `#audit-key`，空表示全部 |
| 状态 | 空、`2xx`、`4xx`、`5xx`、`error`（`status >= 400`） |
| 时间 | 空、今天（本地 0 点起）、近 7 天、近 30 天。传 `since` Unix 秒，不传 `until` |
| 路由 | 子串，匹配 `route` |
| 关键词 | 子串，匹配 `error`、`model`、`requested_model`、`key_name` |

每页 50 条，按 `ts DESC`。标题显示「第 start–end 条，共 total 条。删日志不会改累计用量。」上一页 / 下一页按 `offset` 翻。没有命中时表格写「没有符合条件的记录」。

列：勾选、时间、API Key（名称 + id）、方法 + 路由（保留现有 `SSE`、`低占用` 标记）、模型、状态、耗时、tokens、查看。

- 模型：`requested_model` 与 `model` 不同时写成 `请求模型 → 实际模型`，否则只显示实际模型。
- 状态：2xx–3xx 用成功色，其余用错误色。`error` 直接显示在状态码下面，不再只放在 `title`。
- tokens：第一行 `total_tokens`；第二行保留现有的 `context_tokens`、人民币 / 美元，并加上输入 `prompt_tokens`、输出 `completion_tokens`。

「查看」对每一行都可点，包括没开内容记录的元数据行和 `/v1/models`。弹窗保留现有的展开/收起、图片缩略图和放大。标题补上错误原文；没保存正文时三个预块显示「（空）」。

公网页每 4 秒会调用 `loadGateway()`。这次轮询只更新 KPI、接入信息和 Key 下拉，不要重绘审计表，否则勾选和当前页会被冲掉。审计表只在进入页签、点筛选/刷新、翻页、删除成功后重新加载。

### 删除

三种操作并存：

1. **删除选中**：没有勾选时提示，不发请求。确认后 `POST` `{ "ids": ["..."] }`。只删这些 id。
2. **删除当前筛选**：把当前筛选（不含 `limit` / `offset`）整批删掉，不只是这一页。没有任何筛选条件时拒绝，提示先选条件，避免一次删光。
3. **清空审计与用量**：保持现在的 `POST /api/gateway/clear`。它仍然同时清元数据、内容、图片和 `gateway-state.json` 的全部计数。

前两种只删 `requests` 行。外键 `ON DELETE CASCADE` 会带走 `contents` 和 `images`。不要改 `gateway-state.json`，今日和累计计数保持原值。删完执行 `PRAGMA incremental_vacuum`。

没有筛选条件的按条件删除必须在服务端拒绝，不能只靠页面。`ids` 只接受 12 位小写十六进制，重复的 id 只算一次。

### 宽度

页签本身已经铺满窗口，不要给「公网」页或审计卡片加 `max-width`。请求详情弹窗从 `min(1100px, 96vw)` 放到 `min(1440px, 96vw)`，让新增列和正文跟窗口一样宽。

## 不要改的部分

- 自动保留策略不动：30 天、最多 200,000 行、图片 20 GiB，以及最多每 10 分钟一次的 `store.prune()`。
- 不记录完整 API Key。内容审计仍是设置页的可选项。
- 游戏低占用、`low_resource`、离线 tokenizer 的 `context_tokens` 继续写入，查询原样返回。
- 公网 Gateway 端口仍然不提供这些管理接口。新接口走 `app.py` 里现有的本机来源检查。
- 删除 Key 仍保留该 Key 的历史审计。

## 接口

`GET /api/gateway` 继续负责配置、计数和 Key 列表。审计表改走新接口，不要再依赖它里面的 `recent` 画表。`recent` 可以留着，避免别的调用方坏掉。

`GET /api/gateway/audit`

查询参数：`key_id`、`status`、`route`、`q`、`since`、`until`、`limit`、`offset`。

`limit` 夹在 1–100，默认 50。`offset` 最小 0。`route` 和 `q` 去掉 `%`、`_` 后最多 80 个字符，用参数化 `LIKE`，不要拼接 SQL。

响应：

```json
{ "items": [], "total": 0, "limit": 50, "offset": 0 }
```

`items` 是 `requests` 的整行。

`POST /api/gateway/audit/delete`

- 带非空 `ids`：只按 id 删除，忽略同时带上的筛选。
- 不带 `ids`：按 body 里的筛选删除。筛选为空时返回 400，错误文案说明整表清空要用清空按钮。
- 成功：`{ "ok": true, "deleted": <rowcount> }`。

`GET /api/gateway/content` 在现有字段外补上 `route`、`method`、`requested_model`、`ms`、`error`、`prompt_tokens`、`completion_tokens`、`cached_tokens`、`total_tokens`、`context_tokens`、`cost_cny`、`cost_usd`。

`app.py` 顶部的路由注释和 README「审计与隐私」、接口表要改成：可以筛选和分页；按选中或按筛选删除不改累计用量；只有清空会把计数归零。自动保留策略那段保持原样。

## 落点

- `gateway_db.py`：`query`、`delete_ids`、`delete_matching`。现有 `recent()` 改为走 `query`，避免两套 WHERE。`content()` 扩大 SELECT。
- `gateway.py`：`query_audit()`、`delete_audit()`，转给 store。
- `app.py`：解析筛选、两个新路由，POST 走现有 `origin_ok()`。
- `ui.html`：「公网」页的筛选、分页、勾选、两种删除，以及弹窗宽度。`renderGateway()` 不再画审计表。
- README 的审计小节和接口表。

参考实现在 oMLX Gateway 的 `gateway_db.py`（`_where` / `query` / `delete_ids` / `delete_matching`）、`console.py`（`audit_filters`）、`console.html`（审计卡片）和 `tests/test_audit.py` 的 `test_audit_filter_page_and_delete`。把行为搬过来，不要把 oMLX 的控制台、强制模型或 FRP 关闭流程抄进来。

## 验收

1. 造出 2xx 和 4xx 各一条。`status=2xx` 只含成功的那条，`status=4xx` 加上一个不存在的 `q` 时 `total` 为 0。
2. 聊天请求的 `requested_model` 出现在列表里；低占用模式时仍能看到请求模型到实际模型的箭头和「低占用」。
3. 超过 50 条时可以翻到下一页，再筛选会回到第一页。停在第二页时，4 秒轮询不把勾选清掉，也不跳回第一页。
4. 删除一条选中记录后，内容接口返回 404，图片和内容行一起消失。`/api/gateway` 的 `total_requests` 和 `total_tokens` 与删除前相同。
5. `POST /api/gateway/audit/delete` 空对象返回 400，数据库行数不变。
6. 按 `status=4xx` 删除只删除失败行。随后「清空审计与用量」仍会把剩余行和累计计数一起清掉。
7. 跨来源 POST 仍然 403。公网 Gateway 端口访问 `/api/gateway/audit` 仍然 404。
8. 窗口最大化时审计表横向铺满页签，详情弹窗宽于 1100px。浅色和深色下状态色、按钮和弹窗都可读。
