# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 阶段化培养方案

培养方案除期末总量外，还可配置若干**阶段**（时间窗 + 学时门槛 + 类别门槛），并支持阶段间结转与类别间补偿：

- **规则版本**：`PUT /api/plans/{plan}/rules/{rule_version}` 创建或替换规则版本（阶段时间窗不得重叠）；`POST .../rules/{rule_version}/activate` 将其设为方案的激活版本；`GET .../rules`、`GET .../rules/{rule_version}` 查询。任一阶段关账后该版本即不可再改（409）。
- **业务时间分桶**：重放时签到区间按业务时间与各阶段窗口求交（跨阶段签到会被切开）；请假修正只有显式携带 `business_time` 才计入对应阶段，否则只影响总量。
- **结转与补偿**：阶段盈余可按 `carry_out_cap_seconds` 上限转出，后续阶段缺口可按 `carry_in_cap_seconds` 上限结转；`compensations` 定义类别间补偿（盈余类别抵偿缺口类别，受 `max_seconds` 限制）。学生进度 `GET .../students/{id}/progress` 返回每阶段的 `gap_seconds`（缺口）与 `carry_in_sources`（可结转来源）。
- **阶段冻结（关账）**：`POST /api/plans/{plan}/stages/{stage_id}/freeze` 在当前事件水位对阶段求值并锁定快照（幂等，并发下只有一个写入者）。迟到事件可更新未冻结阶段，但不能穿透已关账边界；`GET .../stages/{stage_id}/freeze` 读取快照，可用 `?rule_version=` 查看历史规则版本下的关账。
- **批量预警**：`GET /api/plans/{plan}/warnings`（可选 `?stage_id=`）列出所有存在学时或类别缺口的学生-阶段组合。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及跨阶段签到、规则版本切换、阶段内负向调整和并发关账；运行过程中不需要单独的数据库或网络服务。
