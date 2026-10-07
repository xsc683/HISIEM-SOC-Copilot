# 可观测性基础

应用使用 OpenTelemetry API 与 OTLP 产出诊断用的 trace 与 metric。**遥测默认关闭**，且**永不作为任何判定的输入**——不参与授权、持久化重试/幂等、调查状态、响应决策或业务结果。

## 应用侧配置

开启 SDK 与标准的 FastAPI / HTTPX / SQLAlchemy / psycopg 埋点，需设置以下环境变量：

```dotenv
OBS_TRACING_ENABLED=true
OBS_SERVICE_NAME=hisiem-soc-copilot
OBS_OTLP_ENDPOINT=http://127.0.0.1:4317
OBS_TRACE_SAMPLE_RATIO=1.0
OBS_METRIC_EXPORT_INTERVAL_MILLIS=60000
```

`OBS_OTLP_ENDPOINT` 是 OTLP/gRPC 接收端地址。`OBS_TRACE_SAMPLE_RATIO` 的取值范围是 `0..1`，默认 `1.0`。**该开关保持 false 即不建 SDK、不埋点**。若 exporter 或埋点装配失败，应用**回退为「遥测关闭」并继续其业务生命周期**。

每个应用 lifespan 在**最后一个活跃 lifespan 关闭时**移除自己的埋点并强制 flush 共享 provider。**provider 是进程级、不做 shutdown**——这样后起的 lifespan 仍可复用 OpenTelemetry 的全局 provider，而不触发被禁止的 provider 替换。

## Collector

`infra/otel-collector/collector.yaml` 是一份最小的、与厂商无关的 Collector 配置：接受 OTLP/gRPC 与 OTLP/HTTP，批处理遥测，**删除敏感 trace 字段与高基数 metric 维度**，再导出到运行时给定的 OTLP 目标：

```text
OTEL_EXPORTER_OTLP_ENDPOINT=<collector-or-backend-host>:4317
```

若所选目标需要认证材料，**通过 Collector 的部署密钥机制提供**；**不要把凭据提交进仓库，也不要放进应用的遥测配置**。让应用指向 Collector 的接收端，让 Collector 的导出端指向**另一处**上游目标——避免把自己导出回自己的接收端。Collector 端口应绑定在可信网络边界内，并套用组织的访问控制。

## 数据与指标策略

**SDK 不采集**：HTTP 头、SQL 绑定参数、SQL 注释、prompt/response 正文、告警或事件载荷、嵌入向量。出站 HTTP URL **降到 origin**；入站的 URL/查询串、凭据/Cookie 与对端地址属性在 **span 导出前**擦除。**Collector 重复一遍敏感字段删除，作为纵深防御。**

指标来自**固定注册表**：**只接受显式白名单内的名字、键与有界取值**；含未知维度的观测会被丢弃。**调查 id、租户 id、trace/span id、请求 id、告警 id、用户 id、工具调用 id 与执行命令 id 都不是 metric 标签。** 持久队列深度**不产出**——当前 outbox API 没有暴露安全的聚合度量。

稳定的语义 span 有九个：`investigation.run`、`graph.invoke`、`tool.execute`、`knowledge.retrieve`、`investigation.persist`、`llm.call`、`response.submit`、`response.observe`、`durable.dispatch`。

持久 outbox 行**可以携带一个经校验的 W3C `traceparent`**；worker 起一条新 trace 并链到生产者的上下文。**上下文缺失或格式错误即忽略。** 业务事件载荷与 `correlation_id` 的语义不变。
