# HISIEM SOC Copilot — 产品定位基线

## 1. 产品名称

**HISIEM SOC Copilot**

定位关键词：

**AI SOC · Security Investigation · Evidence Grounding · Tool-Using Agent · Human-in-the-loop**

## 2. 一句话产品定位

**HISIEM SOC Copilot 是一个面向 SOC 安全分析师的 AI 调查与响应协作系统，通过 Agent 自主规划调查过程、调用安全工具收集证据、检索安全知识并验证攻击假设，最终生成可追溯的调查结论与响应建议；涉及高风险操作时必须经过策略校验和人工审批。**

## 3. 产品边界

HISIEM SOC Copilot 不定位为：

- SIEM 平台；HISIEM 继续承担安全数据、检测、告警和案件管理职责。
- SOAR 平台；HISIEM SOAR 继续负责确定性响应工作流执行。
- 通用聊天机器人或安全知识问答系统。
- 自动执行所有安全操作的完全自治 SOC。
- 安全分析师替代品。

HISIEM SOC Copilot 定位为：

- AI 辅助安全调查与决策系统。
- Tool-using Security Agent。
- Evidence-driven Investigation System。

核心边界：

> HISIEM 负责安全数据、检测、告警、案件和确定性响应能力；SOC Copilot 负责基于这些能力进行智能调查、推理、证据组织和响应决策辅助。

## 4. 目标用户

### Primary User

**SOC Analyst / Security Analyst**

典型工作包括：

- 接收 SIEM Alert。
- 查看相关日志。
- 判断是否误报。
- 判断攻击是否成功。
- 查询 IOC 与 Threat Intelligence。
- 分析用户、主机、IP 等实体。
- 对照 MITRE ATT&CK。
- 查询内部 Runbook。
- 形成调查结论。
- 创建或更新 Case。
- 给出处置建议。

SOC Copilot 的目标不是替代上述职责，而是降低其中重复的信息查询、上下文切换和初步分析成本。

### Secondary User

**Senior SOC Analyst / Incident Responder**

主要职责包括：

- 审核调查结论。
- 查看和验证 Evidence。
- 修正 Agent 判断。
- 审批高风险 Response Action。
- 继续深入调查。

## 5. 核心用户问题

传统 SIEM 告警调查的主要矛盾是：**检测结果是结构化的，但调查过程高度人工化。**

例如收到 SSH 暴力破解告警后，分析师通常需要手动完成：

```text
查看 Alert
    ↓
查询相关 Event
    ↓
扩大时间窗口
    ↓
检查后续成功登录
    ↓
查询 Source IP
    ↓
查看 Target User
    ↓
查看 Host 行为
    ↓
查询 Threat Intelligence
    ↓
查询 MITRE ATT&CK
    ↓
阅读 Runbook
    ↓
判断攻击是否成功
    ↓
整理 Evidence
    ↓
形成 Verdict
    ↓
决定如何处置
```

主要问题包括：

### 5.1 信息分散

调查需要跨 Alert、Logs、Cases、Threat Intelligence、MITRE ATT&CK、Runbooks 与 SOAR 等多个系统和知识源，分析师需要频繁切换上下文。

### 5.2 调查路径动态变化

同一种 Alert 的后续调查步骤取决于已经获得的 Evidence。

例如发现暴力破解后，需要继续判断是否存在后续成功登录；如果存在，则进一步检查权限提升、横向移动或其他异常行为。因此调查不能完全被固定工作流预定义。

### 5.3 调查结论缺乏可追溯性

安全判断不能只是一段自然语言结论。系统需要能够明确回答：

- 结论基于哪些日志？
- 使用了哪些 IOC 或安全知识？
- 调用了哪些工具？
- 哪些 Evidence 支撑了 Verdict？

### 5.4 响应动作存在高风险

模型不能因为一次推理就直接执行账号禁用、主机隔离、IP 封禁等高影响操作。此类行为必须经过明确的 Policy 与 Human Approval Boundary。

## 6. 产品核心价值

HISIEM SOC Copilot 的核心价值是：

> **把 SOC 调查从人工的信息检索与上下文拼接过程，转化为一个由 Agent 驱动、Evidence 可追溯、人类可控制的调查流程。**

核心价值链：

```text
Alert
  ↓
Agent Investigation
  ↓
Evidence
  ↓
Hypothesis
  ↓
Verification
  ↓
Verdict
  ↓
Response Recommendation
  ↓
Human Decision
```

## 7. Agent 适用性

该场景适合使用 Agent，是因为安全调查具有以下特征：

### 7.1 Dynamic Planning

调查步骤无法全部预定义。Agent 需要根据 Alert、当前 Evidence 和 Investigation Goal 决定下一步。

### 7.2 Tool Selection

Agent 需要根据调查需要选择不同的安全工具和数据能力，而不是由用户逐步手动指定调用对象。

### 7.3 Iterative Investigation

调查过程是一个持续的 Reason → Act → Observe → Update Hypothesis → Reason Again 循环，而不是一次性模型生成。

### 7.4 Stateful Execution

一次调查需要持续维护 Plan、Evidence、Hypothesis、Tool Calls、Approval 和 External Execution 等状态。

### 7.5 Human Intervention

高风险 Action 必须支持暂停调查、等待人工决策，并在获得审批结果后继续执行。

## 8. V1 核心场景

V1 只解决一个完整业务闭环：**Security Alert Investigation**。

目标：分析一个 HISIEM Alert，判断它代表什么、攻击是否成功、影响范围是什么，以及应该如何响应。

典型输入：

```text
alert_id
```

典型调查流程：

```text
Alert Hydration
      ↓
Initial Context
      ↓
Investigation Planning
      ↓
Tool-based Investigation
      ↓
Evidence Collection
      ↓
Knowledge Retrieval
      ↓
Hypothesis Verification
      ↓
Security Verdict
      ↓
Response Recommendation
```

首个代表性场景采用 SSH Brute Force / Account Compromise Investigation。

## 9. 目标输出

一次成功的 Investigation 不只是自然语言回答，而是结构化 Investigation Result。

至少包含：

- **Verdict**：安全事件最终判断。
- **Confidence**：系统对结论的置信表达，不替代 Evidence。
- **Evidence**：来自日志、工具、威胁情报和知识检索的可追溯证据。
- **Findings**：由 Evidence 支撑的事实判断。
- **MITRE ATT&CK Mapping**：相关技术与战术映射。
- **Recommended Actions**：建议的响应动作。
- **Approval Requirement**：明确哪些操作需要人工审批。

每一个关键 Finding 都必须能够映射到具体 Evidence。

## 10. 产品核心原则

### 10.1 Evidence before Verdict

Agent 不能先形成结论再寻找支持材料。核心路径为：

```text
Observation
    ↓
Evidence
    ↓
Finding
    ↓
Hypothesis
    ↓
Verdict
```

### 10.2 External Data Is Untrusted

Log、Alert、检索文档和 Tool Result 都属于 Data，不属于 Instruction。

### 10.3 LLM Does Not Own Side Effects

模型只能产生 Action Proposal。真正执行必须受 Policy、Authorization 和 Human Approval 约束。

### 10.4 Structured State over Free-form Text

Investigation 中的核心状态应具有明确结构和领域模型，而不是长期依赖自由文本或非约束字典。

### 10.5 Agent Autonomy Must Be Bounded

Agent 必须受到执行步数、工具调用次数、Token、超时、权限和策略等约束。

### 10.6 Important Decisions Must Be Observable

系统应能够解释重要的调查行为，包括工具调用、Evidence 生成、Verdict 形成以及 Approval 触发原因。

### 10.7 Human Remains the Authority for High-risk Actions

Agent 可以调查、建议和解释，高风险安全响应必须保留人工决策边界。

## 11. HISIEM 与 SOC Copilot 的领域边界

### HISIEM Owns

- Event
- Alert
- Case
- Detection Rule
- Tenant
- User / RBAC
- SOAR Playbook
- SOAR Execution
- Security Platform State

### SOC Copilot Owns

- Agent Run
- Investigation State
- Investigation Plan
- Evidence Projection
- Hypothesis
- Finding
- Verdict
- Agent Memory
- Tool Call Trace
- Response Proposal
- Approval Request
- Agent Evaluation
- Agent Observability

总体关系：

```text
                 HISIEM
                   │
        Security Platform / Source of Truth
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
      Events     Alerts      Cases
        │                     │
        ├──── Detection ──────┤
        │                     │
        └──────── SOAR ───────┘
                   │
                   │ Integration Boundary
                   ▼
            HISIEM SOC Copilot
                   │
              Investigation
                   │
           ┌───────┼───────┐
           ▼       ▼       ▼
        Evidence Knowledge Memory
           │
           ▼
       Hypothesis
           │
           ▼
         Verdict
           │
           ▼
   Response Recommendation
           │
           ▼
      Policy / HITL
           │
           ▼
       HISIEM SOAR
```

## 12. V1 Non-goals

V1 明确不解决：

- 通用安全知识问答。
- 完整 Threat Hunting 平台。
- 自动漏洞扫描。
- 自动 Malware Analysis。
- 自主攻击测试。
- 自动关闭所有 Alert。
- 全自动 Incident Response。
- 复杂 Multi-Agent SOC。
- 跨多个 SIEM 产品适配。
- 替代 HISIEM Case Management。
- 替代 HISIEM SOAR。

如果一个功能不能直接帮助完成 **Alert → Investigation → Evidence → Verdict → Response** 闭环，则默认不进入 V1。

## 13. V1 成功标准

V1 是否完成，不以页面数量、模型数量或代码量判断，而以是否能够完成以下真实闭环判断：

```text
HISIEM creates alert
        ↓
SOC Copilot receives alert reference
        ↓
Agent independently retrieves alert context
        ↓
Agent plans investigation
        ↓
Agent calls multiple tools
        ↓
Agent obtains Evidence
        ↓
Agent retrieves relevant security knowledge
        ↓
Agent updates and verifies hypothesis
        ↓
Agent generates Evidence-grounded Verdict
        ↓
Agent proposes response
        ↓
High-risk action requires human approval
        ↓
Approved action can be delegated to SOAR
        ↓
Full process is traceable and evaluable
```

## 14. 产品差异化原则

HISIEM SOC Copilot 的重点不在更大的模型、更多 Agent、更复杂 UI 或更多外部 API，而在以下四点：

### 14.1 Evidence-grounded Investigation

每个关键安全结论具有可验证的 Evidence Chain。

### 14.2 Agentic Investigation

Agent 根据当前 Evidence 动态决定下一步，而不是机械执行预定义脚本。

### 14.3 Secure Tool Use

工具调用需要受到 Schema、Permission、Policy、Budget、Audit 和 Approval 等边界约束。

### 14.4 Evaluated Agent Behavior

Agent 行为必须可以针对工具使用、检索、Evidence、Verdict、Groundedness、Latency 和 Cost 等维度进行评估。

## 15. 产品价值基线

所有后续产品、架构和实现决策都必须能够回到以下目标：

> **让安全分析师能够更快完成一场可信、可追溯、可控制的安全事件调查。**
