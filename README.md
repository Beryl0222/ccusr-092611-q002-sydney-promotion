# 悉尼文旅推介协作库

面向悉尼中国文化中心“江苏十三城”推介项目的本地服务，保存展项版本、
翻译稿、授权窗口、发布配额与可追溯事件。核心写入使用 SQLite 事务
（`BEGIN IMMEDIATE` + 写锁串行化），服务接口不依赖页面，便于运营人员
在现场或后台系统中核对状态。

## 业务能力

- **机构隔离**：展项与翻译稿只有负责机构（及主办方 `center`）可写；
  签发授权、设置配额、确认翻译稿仅限主办方。
- **版本与并行编辑**：写操作携带 `expected_version`；并行改动按字段做
  三方合并——不同字段自动合并，同字段不同取值整单拒绝（409）。
  已定稿展项再被修改会自动回到 `pending` 等待重新确认。
- **翻译稿流**：`submitted → confirmed / changes_requested`，重新提交
  带版本校验，等待对方确认的修改不会覆盖他方更新。
- **授权窗口**：授权含生效/失效时间与使用范围（web/print/screen/social）。
  过期在任意访问时自动结算为 `expired` 且独立提交；过期或被撤回的授权
  不能再生成发布包。
- **幂等发布与配额**：发布请求带 `request_key`，重试同一批返回同一发布包，
  配额只扣一次；配额不足整批失败，不留半包。
- **离线交接**：每个机构有 outbox 与确认游标，合作方离线/服务重启后可从
  最后确认位置继续拉取，撤回等通知不丢。
- **最终清单**：按城市、语言生成清单，每条含展项/翻译版本、有效授权机构
  与内容哈希，整份清单带 `manifest_hash`，双方可离线复算核验。

## 目录

- `src/sydney_promotion/domain.py`：十三城、语言、授权范围、状态与时间约定。
- `src/sydney_promotion/service.py`：事务、版本合并、授权、配额幂等、交接与清单。
- `src/sydney_promotion/api.py`：本地 HTTP 接口（`X-Org-Id` 标识机构）。
- `tests/`：权限、窗口、版本、幂等、恢复与清单测试（28 个）。

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/records` | 建展项（可带 city/kind/title/payload） |
| GET | `/records/{id}` | 查展项 |
| POST | `/records/{id}/transition` | 状态迁移（draft→pending→approved→closed） |
| POST | `/records/{id}/edits` | 带 expected_version 的字段编辑/三方合并 |
| PUT | `/records/{id}/translations/{lang}` | 提交/重交翻译稿 |
| POST | `/records/{id}/translations/{lang}/review` | 主办方确认或退回 |
| POST | `/grants` / POST `/grants/{id}/revoke` | 签发/撤回授权 |
| GET | `/grants?record_id=&org_id=` | 查授权 |
| POST | `/quotas` | 设置机构发布配额 |
| POST | `/releases` | 生成发布包（request_key 幂等） |
| GET | `/packages`、`/packages/{id}` | 查发布包 |
| POST | `/handshakes` | 打开交接通道 |
| GET | `/handshakes/{id}` | 拉取游标之后的交接事件 |
| POST | `/handshakes/{id}/ack`、`/close` | 确认游标 / 关闭交接 |
| GET | `/checklist?city=&language=&at=` | 按城市语言的可核验最终清单 |

写请求体可带 `request_key` 做幂等重放；服务端时间可通过发布请求的
`at` 字段或清单的 `at` 参数指定，便于验证窗口期行为。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests

## 启动

    PYTHONPATH=src python3 -m sydney_promotion.api
    # 持久化到文件：SYDNEY_DB=/path/to/promotion.db
