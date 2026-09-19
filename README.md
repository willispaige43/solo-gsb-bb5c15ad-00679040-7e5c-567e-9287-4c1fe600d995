# 封签链校验 API（Seal Chain Validation）

用于展品运输交接场景的封签链校验服务。客户端提交按时间排列的 **封装 / 交接 / 开封 / 重封** 事件序列，服务按状态机逐事件校验，返回最终保管人、当前封签、逐事件状态及全部违规事件的位置与原因；同时提供**批量封签占用检查**接口，对多条事件链做跨链 `seal_id` 占用冲突检测；提供**离线片段合并重建**接口，将同一 `chain_id` 的多个离线片段按 `event_id` 去重、按 `prev_event_id` 重建唯一序列后再校验；提供**开封暴露审计**接口，以有效开封 / 重封事件推导开封区间，相对授权开窗窗口做区间差，输出未授权暴露时长；提供**交接计划符合性审计**接口，把有效封装 / 重封 / 交接事件提取为实际到任记录，与按序计划节点做保管人相同、顺序不交叉的全局最优匹配；提供**温控责任审计**接口，把温度样本按前值保持展开为越界区间，与有效到任事件生成的保管人区间求交，按保管人归集高温 / 低温越界时长；并提供**分段校验**接口，把超长事件链拆成若干段依次校验，段间通过带签名的续传令牌传递状态机状态，结果与整链一次校验完全一致。

## 校验规则（状态机）

| # | 规则 |
|---|------|
| 1 | 未封装状态下不得交接（handover）或开封（unseal） |
| 2 | 已封装状态下不得重复封装 / 重封，须先开封 |
| 3 | 开封后必须使用**新的、未使用过的**封签重封（reseal） |
| 4 | 已使用过的封签不得复用 |
| 5 | 事件时间戳必须严格递增 |

**违规事件不改变状态，也不阻止后续事件的判定**（时间递增性始终与最近一个*有效*事件比较）。

## 快速启动

单条命令（依赖随镜像构建完成，启动即就绪）：

```bash
docker compose up --build
```

- API 地址：`http://localhost:8000`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`GET http://localhost:8000/health`

### 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HOST_PORT` | `8000` | 宿主机映射端口，如 `HOST_PORT=9000 docker compose up --build` |
| `SEAL_CHAIN_TOKEN_SECRET` | `seal-chain-dev-secret` | 分段校验续传令牌的 HMAC-SHA256 签名密钥。**仅用于本地开发的默认值，生产环境务必覆盖**，如 `SEAL_CHAIN_TOKEN_SECRET=$(openssl rand -hex 32) docker compose up --build`；变更密钥后旧令牌全部失效（验签失败返回 `422`） |

容器内服务固定监听 `0.0.0.0:8000`。

## API 说明

### `POST /api/v1/seal-chain/validate`

**请求体**

```json
{
  "events": [
    {"type": "seal",     "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
    {"type": "unseal",   "timestamp": "2026-09-18T12:00:00Z"},
    {"type": "reseal",   "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Bob"}
  ]
}
```

- `type`：`seal`（封装）/ `handover`（交接）/ `unseal`（开封）/ `reseal`（重封）
- `timestamp`：ISO 8601 时间，需严格递增
- `seal_id`：`seal` / `reseal` 必填，且不得与历史封签重复
- `custodian`：`seal` / `reseal` / `handover` 必填（交接时为接收方）

**响应示例**（上例中若在第 2、3 步之间插入一次未封装状态下的交接）：

```json
{
  "valid": false,
  "sealed": true,
  "final_custodian": "Bob",
  "current_seal": "S002",
  "events": [
    {"index": 0, "type": "seal", "status": "ok", "reason": null},
    {"index": 1, "type": "handover", "status": "violation", "reason": "未封装状态下不得交接"}
  ],
  "violations": [
    {"index": 1, "reason": "未封装状态下不得交接"}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `valid` | 整条链是否无违规 |
| `sealed` | 处理完所有有效事件后是否处于封装状态 |
| `final_custodian` | 最终保管人 |
| `current_seal` | 当前有效封签，未封装时为 `null` |
| `events[]` | 逐事件状态（`ok` / `violation` + 原因）；`cross_chain_conflict` 在单链接口恒为 `false` |
| `violations[]` | 所有违规事件的位置（`index`，从 0 开始）与原因 |

> 事件结构不合法（如未知 `type`、缺少 `timestamp`）返回 `422`；语义违规（状态机层面）则在响应的 `violations` 中逐条列出。

### 示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/validate \
  -H 'Content-Type: application/json' \
  -d '{"events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
      ]}'
```

### `POST /api/v1/seal-chain/batch-validate`

批量封签占用检查。请求包含多条带 `chain_id` 的事件链，服务处理分两步：

1. **分别按上述状态机规则校验每条链**，得到各链原校验结果；
2. **按输入顺序建立全批次封签占用表**：同一 `seal_id` 被**不同链**的**有效** seal/reseal 事件（单链校验中 `status = ok`）使用时，两条链对应的事件均标记 `cross_chain_conflict: true`。

冲突不改变各链自身状态（链的 `valid` / `sealed` / `violations` 等与单链接口一致），但只要存在冲突，批次的 `valid` 必为 `false`。同一条链内复用封签属于状态机违规（规则 3/4），不计入跨链冲突；单链校验中不合法的事件不构成封签占用。

**请求体**

```json
{
  "chains": [
    {
      "chain_id": "chain-a",
      "events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
      ]
    },
    {
      "chain_id": "chain-b",
      "events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Carol"},
        {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Dave"}
      ]
    }
  ]
}
```

- `chains[]`：按批次输入顺序排列；`chain_id` 在批次内不可重复（重复返回 `422`），允许空批次。

**响应示例**（对应上例）

```json
{
  "valid": false,
  "chains": [
    {
      "chain_id": "chain-a",
      "valid": true,
      "sealed": true,
      "final_custodian": "Alice",
      "current_seal": "S001",
      "events": [
        {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": true}
      ],
      "violations": []
    },
    {
      "chain_id": "chain-b",
      "valid": true,
      "sealed": true,
      "final_custodian": "Dave",
      "current_seal": "S001",
      "events": [
        {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": true},
        {"index": 1, "type": "handover", "status": "ok", "reason": null, "cross_chain_conflict": false}
      ],
      "violations": []
    }
  ],
  "conflicts": [
    {"seal_id": "S001", "chain_id": "chain-a", "event_index": 0, "first_chain_id": "chain-a"},
    {"seal_id": "S001", "chain_id": "chain-b", "event_index": 0, "first_chain_id": "chain-a"}
  ],
  "summary": [
    {"chain_id": "chain-a", "valid": true, "conflicts": 1},
    {"chain_id": "chain-b", "valid": true, "conflicts": 1}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `valid` | 批次是否通过：各链自身均无违规**且**不存在任何跨链冲突 |
| `chains[]` | 每链的原校验结果（结构同单链接口，附 `chain_id`），按**输入顺序**返回；冲突事件的 `events[].cross_chain_conflict` 为 `true` |
| `conflicts[]` | 冲突项，每个被标记的事件一条：`seal_id`、所属 `chain_id`、链内 `event_index`（从 0 开始）、按输入顺序首次占用该封签的 `first_chain_id`；首次占用事件自身的锚点项 `chain_id` 与 `first_chain_id` 相同 |
| `summary[]` | 按 `chain_id` 字典序稳定排序的汇总：每链自身 `valid` 与冲突事件数 `conflicts` |

### 批量接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/batch-validate \
  -H 'Content-Type: application/json' \
  -d '{"chains": [
        {"chain_id": "chain-a", "events": [
          {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
        ]},
        {"chain_id": "chain-b", "events": [
          {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Carol"}
        ]}
      ]}'
```

### `POST /api/v1/seal-chain/reconcile-validate`

离线片段合并重建。同一条链（同一 `chain_id`）在离线状态下被拆成多个片段分别记录，本接口将其合并为唯一序列后再校验。处理分四步：

1. **按 `event_id` 去重**：同一 `event_id` 的多个拷贝业务内容（`type` / `timestamp` / `seal_id` / `custodian` / `prev_event_id`）完全相同时仅保留一份；内容存在分歧时记入 `conflicts` 冲突明细，冲突事件不参与后续重建。
2. **按 `prev_event_id` 重建唯一序列**：仅基于无冲突事件，显式检测**多头**（`prev_event_id` 为 `null` 的链头必须恰好一个）、**缺失前驱**、**分叉**（多个事件共用同一前驱）、**环**；**绝不按时间戳猜测顺序**。
3. **仅重建成功时**（无冲突、恰好一个链头、无缺失前驱、无分叉、无环）按重建序列调用现有单链校验器，时间戳严格递增等规则仍由该校验器判定；否则 `sequence` 与 `validation` 为 `null`。
4. 无论是否重建成功，都返回每个 `event_id` 的**来源片段编号列表**。

**请求体**

```json
{
  "chain_id": "chain-a",
  "fragments": [
    {
      "events": [
        {"event_id": "e1", "prev_event_id": null, "type": "seal",     "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"event_id": "e2", "prev_event_id": "e1", "type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
      ]
    },
    {
      "events": [
        {"event_id": "e2", "prev_event_id": "e1", "type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
        {"event_id": "e3", "prev_event_id": "e2", "type": "unseal",   "timestamp": "2026-09-18T12:00:00Z"}
      ]
    }
  ]
}
```

- `chain_id`：链标识，所有片段必须属于同一条链
- `fragments[]`：离线片段，**片段编号即其在请求中的下标（从 0 开始）**
- 事件字段在单链事件基础上增加：`event_id`（必填，判重依据）、`prev_event_id`（前驱事件标识，`null` 表示链头）

**响应示例**（对应上例，两个片段的 `e2` 完全一致，被合并）

```json
{
  "chain_id": "chain-a",
  "reconciled": true,
  "conflicts": [],
  "structure": {"heads": ["e1"], "missing_predecessors": [], "forks": [], "cycles": []},
  "sequence": ["e1", "e2", "e3"],
  "validation": {
    "valid": true,
    "sealed": false,
    "final_custodian": "Bob",
    "current_seal": null,
    "events": [
      {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 1, "type": "handover", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 2, "type": "unseal", "status": "ok", "reason": null, "cross_chain_conflict": false}
    ],
    "violations": []
  },
  "event_sources": [
    {"event_id": "e1", "fragments": [0]},
    {"event_id": "e2", "fragments": [0, 1]},
    {"event_id": "e3", "fragments": [1]}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `reconciled` | 是否成功重建唯一序列：无内容冲突、恰好一个链头、无缺失前驱、无分叉、无环 |
| `conflicts[]` | 同一 `event_id` 内容分歧的明细：`differing_fields` 列出分歧字段，`variants[]` 给出每种内容变体及其来源片段编号 |
| `structure.heads[]` | `prev_event_id` 为 `null` 的链头事件，必须恰好一个才能重建 |
| `structure.missing_predecessors[]` | 前驱不在无冲突事件集中的事件（前驱未出现，或因冲突被排除） |
| `structure.forks[]` | 分叉：`prev_event_id` 及其全部后继事件 |
| `structure.cycles[]` | 环：环内每个事件的 `prev_event_id` 依次指向列表下一事件，末尾指回开头 |
| `sequence` | 重建成功时的 `event_id` 序列，否则为 `null` |
| `validation` | 仅重建成功时调用现有单链校验器的结果（结构同单链接口，`index` 对应 `sequence` 中的位置），否则为 `null` |
| `event_sources[]` | 每个 `event_id` 的来源片段编号列表（升序去重），按首次出现顺序排列；冲突事件也包含在内 |

> 结构诊断（`structure`）只基于无冲突事件；时间戳只交给单链校验器按规则 5 判定，重建阶段不按时间戳排序。事件结构不合法（如缺少 `event_id`）返回 `422`。

### 合并重建接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/reconcile-validate \
  -H 'Content-Type: application/json' \
  -d '{"chain_id": "chain-a", "fragments": [
        {"events": [
          {"event_id": "e1", "prev_event_id": null, "type": "seal",
           "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
        ]},
        {"events": [
          {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
           "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
        ]}
      ]}'
```

### `POST /api/v1/seal-chain/exposure-audit`

开封暴露审计。在单链状态机校验的基础上，回答“**展品处于开封状态的时间里，有多少落在授权开窗窗口之外**”。处理分四步：

1. **调用现有单链校验器**，响应中原样保留 `validation`（结构同 `/validate`，含逐事件状态与违规明细）；
2. **推导开封区间**：仅以 `validation.events` 中 `status = ok` 的 unseal / reseal 事件配对——有效 unseal 开启一个半开区间 `[start, end)`，其后第一个有效 reseal 将其闭合；末次开封后没有有效重封时，末点延伸至 `audit_until`（`audit_until` 恰好等于开封时间时退化为零长度区间，不计时长）。违规事件（包括状态机层面不合法的 unseal / reseal）一律忽略；
3. **合并授权窗口**：`allowed_windows` 可乱序、嵌套；先按起点排序，再合并所有相交或首尾相接（`a.end == b.start`，半开区间下该点两侧均被覆盖）的窗口；
4. **逐段做区间差**：对每段开封区间与合并后的窗口求差——落在窗口内的分段计入 `covered`，其余计入 `uncovered`；一段开封跨多个窗口时会被切成多段。全部分段按**时间升序**返回，并携带产生该段的有效开封 / 重封事件下标。

- `audit_until`：审计截止时间（ISO 8601）。**早于最后一个有效事件时间时返回 `422`**（违规事件不推进有效时间基准）；恰好相等时允许。
- `allowed_windows[]`：授权开窗窗口，`start` 必须严格早于 `end`（否则 `422`）；允许空列表，表示整段开封均未授权。窗口与事件时间戳不得混用带时区与不带时区的时间（否则 `422`）。
- 链本身存在状态机违规**不影响**审计接口返回 `200`，违规情况仍只体现在 `validation` 中。

**请求体**

```json
{
  "events": [
    {"type": "seal",   "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "unseal", "timestamp": "2026-09-18T11:00:00Z"},
    {"type": "reseal", "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Alice"},
    {"type": "unseal", "timestamp": "2026-09-18T16:00:00Z"}
  ],
  "audit_until": "2026-09-18T20:00:00Z",
  "allowed_windows": [
    {"start": "2026-09-18T12:00:00Z", "end": "2026-09-18T12:30:00Z"},
    {"start": "2026-09-18T17:00:00Z", "end": "2026-09-18T19:00:00Z"}
  ]
}
```

**响应示例**（对应上例：两段开封分别为 `[11:00,13:00)` 与 `[16:00,20:00)`，末段未重封延伸至 `audit_until`）

```json
{
  "audit_until": "2026-09-18T20:00:00Z",
  "validation": {
    "valid": true,
    "sealed": false,
    "final_custodian": "Alice",
    "current_seal": null,
    "events": [
      {"index": 0, "type": "seal",   "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 1, "type": "unseal", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 2, "type": "reseal", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 3, "type": "unseal", "status": "ok", "reason": null, "cross_chain_conflict": false}
    ],
    "violations": []
  },
  "covered": [
    {"start": "2026-09-18T12:00:00Z", "end": "2026-09-18T12:30:00Z",
     "seconds": 1800.0, "unseal_index": 1, "reseal_index": 2},
    {"start": "2026-09-18T17:00:00Z", "end": "2026-09-18T19:00:00Z",
     "seconds": 7200.0, "unseal_index": 3, "reseal_index": null}
  ],
  "uncovered": [
    {"start": "2026-09-18T11:00:00Z", "end": "2026-09-18T12:00:00Z",
     "seconds": 3600.0, "unseal_index": 1, "reseal_index": 2},
    {"start": "2026-09-18T12:30:00Z", "end": "2026-09-18T13:00:00Z",
     "seconds": 1800.0, "unseal_index": 1, "reseal_index": 2},
    {"start": "2026-09-18T16:00:00Z", "end": "2026-09-18T17:00:00Z",
     "seconds": 3600.0, "unseal_index": 3, "reseal_index": null},
    {"start": "2026-09-18T19:00:00Z", "end": "2026-09-18T20:00:00Z",
     "seconds": 3600.0, "unseal_index": 3, "reseal_index": null}
  ],
  "unauthorized_seconds": 12600.0
}
```

| 字段 | 说明 |
|------|------|
| `audit_until` | 回显请求中的审计截止时间 |
| `validation` | 现有单链校验器的原始结果（结构同 `/validate`），审计仅采用其中 `status=ok` 的 unseal / reseal |
| `covered[]` | 落在授权窗口内的已覆盖分段（半开 `[start, end)`），按时间升序 |
| `uncovered[]` | 落在授权窗口外的未覆盖分段（半开 `[start, end)`），按时间升序 |
| `covered/uncovered[].seconds` | 分段时长（秒），即 `end - start` |
| `covered/uncovered[].unseal_index` | 产生该段的有效开封事件下标（从 0 开始） |
| `covered/uncovered[].reseal_index` | 对应有效重封事件下标；末次开封未重封、区间延伸至 `audit_until` 时为 `null` |
| `unauthorized_seconds` | 未授权暴露总秒数，等于全部 `uncovered` 分段秒数之和 |

### 开封暴露审计接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/exposure-audit \
  -H 'Content-Type: application/json' \
  -d '{"events": [
          {"type": "seal",
           "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
          {"type": "unseal", "timestamp": "2026-09-18T11:00:00Z"},
          {"type": "reseal",
           "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Alice"}
        ],
        "audit_until": "2026-09-18T20:00:00Z",
        "allowed_windows": [
          {"start": "2026-09-18T12:30:00Z", "end": "2026-09-18T12:00:00Z"}
        ]}'
# start 晚于 end，返回 422
```

### `POST /api/v1/seal-chain/handover-plan-audit`

交接计划符合性审计。在单链状态机校验的基础上，回答“**实际到任记录与按序计划的保管人交接窗口是否吻合**”。处理分三步：

1. **调用现有单链校验器**，响应中原样保留 `validation`（结构同 `/validate`）；
2. **提取实际到任记录**：仅从 `validation.events` 中 `status = ok` 的 **seal / reseal / handover** 事件提取（到任保管人即该事件发生后的在任保管人），并保留其在**全链事件中的下标**；unseal 不是到任记录，违规事件（包括状态机层面不合法的 seal/reseal/handover）一律不参与匹配；
3. **全局最优匹配**：计划节点与实际记录做一对一匹配，要求**保管人相同且按顺序不交叉**（保序）。采用序列对齐式动态规划求全局最优（**不做逐项贪心**），目标依次为：
   1. **最大化匹配数**；
   2. 匹配数相同时，**最小化实际时间偏离对应窗口的总秒数**（`early: window_start - t`；`late: t - window_end`；`on_time: 0`）；
   3. 仍并列时，取**匹配事件下标序列字典序最小**的方案（按计划节点顺序排列的事件下标元组）。

窗口为**闭起点 / 开终点**：`window_start <= t < window_end` 计为 `on_time`，早于 `window_start` 为 `early`，不早于 `window_end` 为 `late`。未匹配的计划节点为 `missing`，未匹配的实际记录列入 `unexpected`。链本身存在状态机违规**不影响**审计接口返回 `200`，违规情况只体现在 `validation` 中；窗口起止时间混用带时区与不带时区的时间、或 `window_start >= window_end` 时返回 `422`。

**请求体**

```json
{
  "events": [
    {"type": "seal",     "timestamp": "2026-09-19T09:50:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "handover", "timestamp": "2026-09-19T11:00:00Z", "custodian": "Bob"},
    {"type": "unseal",   "timestamp": "2026-09-19T11:30:00Z"},
    {"type": "reseal",   "timestamp": "2026-09-19T11:45:00Z", "seal_id": "S002", "custodian": "Bob"}
  ],
  "plan": [
    {"custodian": "Alice", "window_start": "2026-09-19T10:00:00Z", "window_end": "2026-09-19T11:00:00Z"},
    {"custodian": "Bob",   "window_start": "2026-09-19T11:00:00Z", "window_end": "2026-09-19T12:00:00Z"}
  ]
}
```

- `plan[]`：按计划顺序排列的节点，每个节点含 `custodian`、`window_start`、`window_end`（窗口半开，起点必须严格早于终点）；允许空列表。

**响应示例**（对应上例：Alice 早到 10 分钟，Bob 准时；合法 reseal 后在任保管人仍为 Bob，无后续 Bob 节点，故下标 3 为 unexpected；unseal 不计入到任记录）

```json
{
  "validation": {
    "valid": true,
    "sealed": true,
    "final_custodian": "Bob",
    "current_seal": "S002",
    "events": [
      {"index": 0, "type": "seal",     "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 1, "type": "handover", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 2, "type": "unseal",   "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 3, "type": "reseal",   "status": "ok", "reason": null, "cross_chain_conflict": false}
    ],
    "violations": []
  },
  "plan_results": [
    {
      "index": 0, "custodian": "Alice",
      "window_start": "2026-09-19T10:00:00Z", "window_end": "2026-09-19T11:00:00Z",
      "status": "matched", "event_index": 0,
      "actual_timestamp": "2026-09-19T09:50:00Z",
      "timing": "early", "deviation_seconds": 600.0
    },
    {
      "index": 1, "custodian": "Bob",
      "window_start": "2026-09-19T11:00:00Z", "window_end": "2026-09-19T12:00:00Z",
      "status": "matched", "event_index": 1,
      "actual_timestamp": "2026-09-19T11:00:00Z",
      "timing": "on_time", "deviation_seconds": 0.0
    }
  ],
  "unexpected": [
    {"event_index": 3, "type": "reseal",
     "timestamp": "2026-09-19T11:45:00Z", "custodian": "Bob"}
  ],
  "summary": {"matched": 2, "missing": 0, "unexpected": 1,
              "early": 1, "on_time": 1, "late": 0}
}
```

| 字段 | 说明 |
|------|------|
| `validation` | 现有单链校验器的原始结果（结构同 `/validate`），审计仅采用其中 `status=ok` 的 seal/reseal/handover |
| `plan_results[]` | 每个计划节点的结果，按请求 `plan` 顺序；含回显的 `custodian` / `window_start` / `window_end` |
| `plan_results[].status` | `matched`（已匹配实际记录）或 `missing`（无实际到任记录） |
| `plan_results[].event_index` | 匹配事件在**全链事件**中的下标（从 0 开始，含违规事件位置）；`missing` 时为 `null` |
| `plan_results[].timing` | `early` / `on_time` / `late`；`missing` 时为 `null` |
| `plan_results[].deviation_seconds` | 实际时间偏离窗口的秒数（早到取距起点、迟到取距终点，准时为 0）；`missing` 时为 `null` |
| `unexpected[]` | 未匹配任何计划节点的实际到任记录，按事件下标升序；含 `event_index` / `type` / `timestamp` / `custodian` |
| `summary` | `matched` / `missing` / `unexpected` 数量，以及已匹配记录中 `early` / `on_time` / `late` 的数量 |

### 交接计划符合性审计接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/handover-plan-audit \
  -H 'Content-Type: application/json' \
  -d '{"events": [
          {"type": "seal", "timestamp": "2026-09-19T10:00:00Z",
           "seal_id": "S001", "custodian": "Alice"},
          {"type": "handover", "timestamp": "2026-09-19T11:30:00Z",
           "custodian": "Bob"}
        ],
        "plan": [
          {"custodian": "Alice", "window_start": "2026-09-19T10:00:00Z",
           "window_end": "2026-09-19T11:00:00Z"},
          {"custodian": "Bob", "window_start": "2026-09-19T11:00:00Z",
           "window_end": "2026-09-19T12:00:00Z"}
        ]}'
# Alice 准时、Bob 迟到 1800 秒：matched=2, late=1
```

### `POST /api/v1/seal-chain/temperature-audit`

温控责任审计。在单链状态机校验的基础上，回答“**越界（高温 / 低温）时间该由哪位保管人负责**”。处理分五步：

1. **调用现有单链校验器**，响应中原样保留 `validation`（结构同 `/validate`）；
2. **生成保管人区间**：仅以 `validation.events` 中 `status = ok` 的 **seal / reseal / handover** 事件生成保管人半开区间——第 k 个有效到任事件在 `t_k` 使该保管人到任，区间为 `[t_k, t_{k+1})`，最后一个区间延伸至正无穷；unseal 与违规事件不产生区间，首个有效到任事件之前的时间不属于任何保管人；
3. **展开温度样本**：样本时间戳必须**严格递增**（否则 `422`）。相邻样本 i、i+1 的间隔**不超过** `max_gap_seconds` 时按前值保持形成半开区间 `[t_i, t_{i+1})`（温度取样本 i 的值，间隔恰好等于阈值仍形成区间）；间隔超出阈值视为**数据空洞**，期间不计时；最后一个样本不形成区间；
4. **判定越界**：样本温度落在 `[min_temperature, max_temperature]`（**闭区间**）之外即为越界——高于上限为高温（`high`），低于下限为低温（`low`），偏差为超出阈值的度数；
5. **归集**：越界区间与保管人区间求交，**跨交接点必须拆分**；落在任何保管人区间之外的部分计入**未归属**（`custodian` 为 `null`）。按保管人汇总高温 / 低温秒数，并给出全链及每个保管人的最大偏差（可追溯到样本下标）。

- `samples[]`：温度样本，`timestamp` + `temperature`（不允许 NaN / 无穷，否则 `422`）；允许空列表。
- `min_temperature` / `max_temperature`：允许温度范围（闭区间），`min_temperature` 必须严格小于 `max_temperature`（否则 `422`）。
- `max_gap_seconds`：前值保持允许的最大采样间隔（秒），必须为**正数**（否则 `422`）。
- 样本与事件时间戳不得混用带时区与不带时区的时间（否则 `422`）。链本身存在状态机违规**不影响**审计接口返回 `200`，违规情况只体现在 `validation` 中。

**请求体**

```json
{
  "events": [
    {"type": "seal",     "timestamp": "2026-09-19T09:00:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "handover", "timestamp": "2026-09-19T11:00:00Z", "custodian": "Bob"}
  ],
  "samples": [
    {"timestamp": "2026-09-19T08:30:00Z", "temperature": 10.0},
    {"timestamp": "2026-09-19T08:50:00Z", "temperature": 6.5},
    {"timestamp": "2026-09-19T09:30:00Z", "temperature": 9.5},
    {"timestamp": "2026-09-19T10:00:00Z", "temperature": 7.0},
    {"timestamp": "2026-09-19T10:50:00Z", "temperature": 9.0},
    {"timestamp": "2026-09-19T11:10:00Z", "temperature": 6.0},
    {"timestamp": "2026-09-19T12:30:00Z", "temperature": 1.0},
    {"timestamp": "2026-09-19T12:45:00Z", "temperature": 6.0}
  ],
  "min_temperature": 2.0,
  "max_temperature": 8.0,
  "max_gap_seconds": 1800
}
```

**响应示例**（对应上例：`[08:30,08:50)` 未归属高温；`[10:50,11:10)` 跨 11:00 交接点拆分为 Alice / Bob 两段；三处间隔超 1800 秒的空洞不计时；`validation` 从略）

```json
{
  "min_temperature": 2.0,
  "max_temperature": 8.0,
  "max_gap_seconds": 1800.0,
  "custodians": [
    {"custodian": "Alice", "high_seconds": 2400.0, "low_seconds": 0.0,
     "max_deviation": {"sample_index": 2, "kind": "high", "temperature": 9.5, "deviation": 1.5}},
    {"custodian": "Bob", "high_seconds": 600.0, "low_seconds": 900.0,
     "max_deviation": {"sample_index": 4, "kind": "high", "temperature": 9.0, "deviation": 1.0}}
  ],
  "violations": [
    {"custodian": null, "kind": "high",
     "start": "2026-09-19T08:30:00Z", "end": "2026-09-19T08:50:00Z",
     "seconds": 1200.0, "sample_index": 0, "temperature": 10.0, "deviation": 2.0},
    {"custodian": "Alice", "kind": "high",
     "start": "2026-09-19T09:30:00Z", "end": "2026-09-19T10:00:00Z",
     "seconds": 1800.0, "sample_index": 2, "temperature": 9.5, "deviation": 1.5},
    {"custodian": "Alice", "kind": "high",
     "start": "2026-09-19T10:50:00Z", "end": "2026-09-19T11:00:00Z",
     "seconds": 600.0, "sample_index": 4, "temperature": 9.0, "deviation": 1.0},
    {"custodian": "Bob", "kind": "high",
     "start": "2026-09-19T11:00:00Z", "end": "2026-09-19T11:10:00Z",
     "seconds": 600.0, "sample_index": 4, "temperature": 9.0, "deviation": 1.0},
    {"custodian": "Bob", "kind": "low",
     "start": "2026-09-19T12:30:00Z", "end": "2026-09-19T12:45:00Z",
     "seconds": 900.0, "sample_index": 6, "temperature": 1.0, "deviation": 1.0}
  ],
  "unattributed_high_seconds": 1200.0,
  "unattributed_low_seconds": 0.0,
  "unattributed_seconds": 1200.0,
  "max_deviation": {"sample_index": 0, "kind": "high", "temperature": 10.0, "deviation": 2.0},
  "data_holes": [
    {"start": "2026-09-19T08:50:00Z", "end": "2026-09-19T09:30:00Z",
     "seconds": 2400.0, "prev_sample_index": 1, "next_sample_index": 2},
    {"start": "2026-09-19T10:00:00Z", "end": "2026-09-19T10:50:00Z",
     "seconds": 3000.0, "prev_sample_index": 3, "next_sample_index": 4},
    {"start": "2026-09-19T11:10:00Z", "end": "2026-09-19T12:30:00Z",
     "seconds": 4800.0, "prev_sample_index": 5, "next_sample_index": 6}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `validation` | 现有单链校验器的原始结果（结构同 `/validate`），审计仅采用其中 `status=ok` 的 seal/reseal/handover |
| `min_temperature` / `max_temperature` / `max_gap_seconds` | 回显请求中的允许温度范围与最大采样间隔 |
| `custodians[]` | 按保管人汇总的高温 / 低温越界秒数，按其在有效到任事件中**首次出现的顺序**排列；同一保管人多次到任合并汇总；任内无越界时 `max_deviation` 为 `null` |
| `violations[]` | 全部越界分段（半开 `[start, end)`，已在交接点拆分），按时间升序；`custodian` 为 `null` 的分段未归属 |
| `violations[].sample_index` | 产生该分段的温度样本下标（从 0 开始），即前值保持区间 `[t_i, t_{i+1})` 的左端样本 |
| `violations[].deviation` | 超出允许范围的偏差（>0）：高温为 `temperature - max_temperature`，低温为 `min_temperature - temperature` |
| `unattributed_high_seconds` / `unattributed_low_seconds` / `unattributed_seconds` | 未归属到任何保管人的高温 / 低温 / 合计越界秒数（首个有效到任事件之前，或全链无有效到任事件） |
| `max_deviation` | 全部越界分段（含未归属）中的最大偏差，含 `sample_index` / `kind` / `temperature` / `deviation`；偏差并列时取样本下标更小者；无任何越界时为 `null` |
| `data_holes[]` | 数据空洞（相邻样本间隔超过 `max_gap_seconds`），按时间升序，含前后样本下标；空洞期间不计时 |

### 温控责任审计接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/temperature-audit \
  -H 'Content-Type: application/json' \
  -d '{"events": [
          {"type": "seal", "timestamp": "2026-09-19T09:00:00Z",
           "seal_id": "S001", "custodian": "Alice"},
          {"type": "handover", "timestamp": "2026-09-19T11:00:00Z",
           "custodian": "Bob"}
        ],
        "samples": [
          {"timestamp": "2026-09-19T10:50:00Z", "temperature": 9.0},
          {"timestamp": "2026-09-19T11:10:00Z", "temperature": 6.0}
        ],
        "min_temperature": 2.0, "max_temperature": 8.0, "max_gap_seconds": 1800}'
# 越界区间 [10:50,11:10) 跨交接点拆分：Alice 600 秒 + Bob 600 秒高温
```

### `POST /api/v1/seal-chain/validate-chunk`

分段校验超长事件链。把一条长链拆成若干段依次提交：**首段不带 `continuation_token`**，响应返回不透明的**续传令牌**；后续段携带上一响应返回的令牌，即可从上一段结束时的状态机状态继续校验。**逐事件判定与把所有分段拼接后调用 `/api/v1/seal-chain/validate` 完全一致**，响应中的 `index` 为全链偏移（从 0 开始）。

**续传令牌**

- 令牌完整保存：封装状态（含是否曾封装）、保管人、当前及已用封签、最近有效事件时间与累计事件数（另含累计违规数，用于累计摘要）；
- 格式为 `base64url(负载JSON) + "." + base64url(HMAC-SHA256 签名)`，负载键序固定、封签列表排序，签名密钥取自环境变量 `SEAL_CHAIN_TOKEN_SECRET`——**确定性签名**：同一状态 + 同一密钥必然得到同一令牌；
- 令牌在处理本段事件**之前**校验：**任何篡改（签名不匹配）、版本不支持或状态字段非法（类型非法 / 自相矛盾的状态）均返回 `422`，本段事件不会被处理**，也不会签发新令牌——用上一响应的原令牌重发本段即可。

**请求体（首段）**

```json
{
  "events": [
    {"type": "seal",     "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
  ]
}
```

**响应示例（首段）**

```json
{
  "events": [
    {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": false},
    {"index": 1, "type": "handover", "status": "ok", "reason": null, "cross_chain_conflict": false}
  ],
  "violations": [],
  "summary": {
    "event_count": 2,
    "violation_count": 0,
    "valid": true,
    "sealed": true,
    "custodian": "Bob",
    "current_seal": "S001"
  },
  "continuation_token": "eyJjdXJyZW50X3NlYWwiOiJTMDAxIiwiY3VzdG9kaWFuIjoiQm9iIiwiZXZlbnRfY291bnQiOjIsImV2ZXJfc2VhbGVkIjp0cnVlLCJsYXN0X3RpbWVzdGFtcCI6IjIwMjYtMDktMThUMTE6MDA6MDArMDA6MDAiLCJzZWFsZWQiOnRydWUsInVzZWRfc2VhbHMiOlsiUzAwMSJdLCJ2IjoxLCJ2aW9sYXRpb25fY291bnQiOjB9.RRZPGN2jN-GmdhWnI2ji82ON-ZA7TUiFvYd-N3k_P9k"
}
```

**请求体（后续段，携带上一响应的令牌）**

```json
{
  "events": [
    {"type": "unseal", "timestamp": "2026-09-18T12:00:00Z"},
    {"type": "reseal", "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Bob"}
  ],
  "continuation_token": "eyJjdXJyZW50X3NlYWwiOiJTMDAxIiwiY3VzdG9kaWFuIjoiQm9iIiwiZXZlbnRfY291bnQiOjIsImV2ZXJfc2VhbGVkIjp0cnVlLCJsYXN0X3RpbWVzdGFtcCI6IjIwMjYtMDktMThUMTE6MDA6MDArMDA6MDAiLCJzZWFsZWQiOnRydWUsInVzZWRfc2VhbHMiOlsiUzAwMSJdLCJ2IjoxLCJ2aW9sYXRpb25fY291bnQiOjB9.RRZPGN2jN-GmdhWnI2ji82ON-ZA7TUiFvYd-N3k_P9k"
}
```

**响应示例（后续段：`index` 为全链偏移，`summary` 为全链累计）**

```json
{
  "events": [
    {"index": 2, "type": "unseal", "status": "ok", "reason": null, "cross_chain_conflict": false},
    {"index": 3, "type": "reseal", "status": "ok", "reason": null, "cross_chain_conflict": false}
  ],
  "violations": [],
  "summary": {
    "event_count": 4,
    "violation_count": 0,
    "valid": true,
    "sealed": true,
    "custodian": "Bob",
    "current_seal": "S002"
  },
  "continuation_token": "eyJjdXJyZW50X3NlYWwiOiJTMDAyIiwiY3VzdG9kaWFuIjoiQm9iIiwiZXZlbnRfY291bnQiOjQsImV2ZXJfc2VhbGVkIjp0cnVlLCJsYXN0X3RpbWVzdGFtcCI6IjIwMjYtMDktMThUMTM6MDA6MDArMDA6MDAiLCJzZWFsZWQiOnRydWUsInVzZWRfc2VhbHMiOlsiUzAwMSIsIlMwMDIiXSwidiI6MSwidmlvbGF0aW9uX2NvdW50IjowfQ.Yf7FQcU-wfNAZQNshH1X6tGeJAnHxwntr_k1TG1xEsI"
}
```

| 字段 | 说明 |
|------|------|
| `events[]` | 本段逐事件判定结果；`index` 为全链偏移，与整链一次校验完全一致 |
| `violations[]` | 本段违规事件的位置（全链偏移）与原因 |
| `summary.event_count` | 全链已累计处理的事件总数（含违规事件），即下一段的起始偏移 |
| `summary.violation_count` | 全链累计违规事件数 |
| `summary.valid` | 全链截至目前是否无任何违规 |
| `summary.sealed` / `custodian` / `current_seal` | 当前封装状态 / 保管人 / 当前有效封签 |
| `continuation_token` | 下一段请求需携带的续传令牌（不透明字符串）；链未校验完时持续接力 |

> 令牌校验失败返回 `422` 的常见原因：令牌被篡改或密钥变更（签名不匹配）、令牌版本不支持、负载状态字段非法。`422` 不会推进任何状态，用上一响应的原令牌重发本段即可继续。事件结构不合法（如未知 `type`）同样返回 `422`。

### 分段校验接口示例调用

```bash
# 首段：不带 continuation_token
curl -s http://localhost:8000/api/v1/seal-chain/validate-chunk \
  -H 'Content-Type: application/json' \
  -d '{"events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
      ]}'
# → 从响应中取出 continuation_token

# 后续段：携带上一响应返回的令牌
curl -s http://localhost:8000/api/v1/seal-chain/validate-chunk \
  -H 'Content-Type: application/json' \
  -d '{"events": [
        {"type": "unseal", "timestamp": "2026-09-18T12:00:00Z"},
        {"type": "reseal", "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Bob"}
      ],
      "continuation_token": "<上一响应返回的 continuation_token>"}'
```

> 服务默认监听宿主机 **8000** 端口（容器内固定 `0.0.0.0:8000`，可用 `HOST_PORT` 覆盖），以上示例中的 `http://localhost:8000` 即该端口。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest -v
uvicorn app.main:app --reload --port 8000
```

## 项目结构

```
app/
  main.py        # FastAPI 入口与路由
  models.py      # 请求 / 响应模型（单链、批量、合并重建、开封暴露审计、交接计划审计、温控责任审计与分段校验）
  validator.py   # 单链状态机校验核心
  batch.py       # 批量封签占用检查（跨链冲突）
  reconcile.py   # 离线片段合并重建（去重 / 结构诊断 / 调用单链校验）
  exposure.py    # 开封暴露审计（开封区间推导 / 窗口合并 / 区间差）
  handover.py    # 交接计划符合性审计（实际到任提取 / 全局最优匹配动态规划）
  temperature.py # 温控责任审计（保管人区间 / 前值保持 / 越界归集）
  chunk.py       # 分段校验（续传令牌签发 / 验签 / 状态还原）
tests/
  test_validator.py
  test_batch.py
  test_reconcile.py
  test_exposure.py
  test_handover.py
  test_temperature.py
  test_chunk.py
Dockerfile
docker-compose.yml
requirements.txt
```
