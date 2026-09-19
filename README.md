# League Points Ledger

本地运行的联赛积分账本 API：Python + FastAPI + SQLite。

- **不可变事件**：积分以事件形式追加写入，写后不可改。
- **幂等写入**：每个 `event_id` 只生效一次；相同载荷重放返回原结果（200），相同 `event_id` 不同载荷返回 **409**。
- **原子批量写入**：`POST /events/batch` 一次接收 1～100 个事件，在同一 `BEGIN IMMEDIATE` 事务中校验并落库，按数组顺序分配一段连续 revision；任一事件非法整批回滚，不留事件、不耗 revision。
- **积分情景预演**：`POST /events/preview` 以任一历史 revision 为基线，只读推演 1～100 条草案事件按顺序生效后的排行榜与名次变化——更正链、分叉与并列规则与真实账本一致，不写库、不消耗 revision。
- **更正链**：更正事件通过 `correction_of` 引用已有事件（或同批更早的事件），更正本身可被再次更正（链式）。任一 revision 上，每条更正链只计**当时的末端事件**。
- **递增 revision**：每次成功写入获得严格递增的 revision；可查询当前或任一历史 revision 的排行榜与生效明细。
- **奖励结算预演**：`POST /rewards/preview` 按指定 revision 的排行榜只读试算奖金分配——并列组共享合并后的奖金池、余数按 `participant_id` 升序分配，不写入账本。
- **更正分叉审计**：`GET /audit/correction-forks?revision=` 按指定快照还原更正关系，仅返回存在分叉（同一事件被两个及以上事件直接更正）的更正链，列出根到各生效叶子的完整路径与按参赛者汇总的生效积分；只读，普通线性链不返回。
- **积分阈值区间**：`GET /participants/{participant_id}/threshold-intervals` 逐 revision 重算单个参赛者的真实生效总分，把"总分 ≥ 阈值"的连续 revision 合并为闭区间，返回区间起止与两端积分；更正链分叉、跨参赛者更正造成的升降都会体现，未出现的参赛者按 0 分处理。
- **排行榜快照承诺**：`GET /leaderboard/commitment?revision=` 对榜单按序做确定性编码（rank、participant_id、total_points、生效 event_id 列表），构建 SHA-256 Merkle 树并返回叶数与根摘要；`GET /leaderboard/proof/{participant_id}?revision=` 返回条目、叶索引与逐层兄弟节点，客户端可独立复算同一根。奇数层末节点与自身配对，单叶树根即叶哈希，空榜根固定为 `SHA256("")`，缺席参赛者返回 404。历史快照只依赖该 revision 及之前的事件，不受后续写入影响。
- **并发安全**：写入经 `BEGIN IMMEDIATE` 串行化，revision 在写锁内分配——并发写入（含批量）不会产生重复 revision 或中间态。
- **不透明标识符可回读**：`event_id`/`participant_id` 为不透明字符串，允许包含 `/`、空格、非 ASCII 字符，或与静态路由同名（如 `changes`、`compare`）；携带 ID 的 GET 路由基于原始（未解码）URL 路径匹配，ID 经百分号编码后可精确查询，静态路由行为不变（见「路径中的不透明 ID」一节）。

## 快速启动

```bash
docker compose up --build
```

一条命令完成镜像构建与启动：服务监听容器内 **8000** 端口，并通过 `docker-compose.yml` 的端口映射 `"8000:8000"` 暴露到宿主机同一端口。服务启动后访问 <http://localhost:8000>，交互式文档见 <http://localhost:8000/docs>（如本机 8000 端口被占用，可把映射改为例如 `"8001:8000"`，再访问 <http://localhost:8001>，容器内端口始终是 8000）。

数据持久化在命名卷 `ledger-data` 中（SQLite 文件 `/data/ledger.db`）。

游标分页密钥（见下文「配置」）同样保存在该卷的 `/data/cursor_secret` 文件中：不配置 `LEADERBOARD_CURSOR_SECRET` 时，首次启动自动用安全随机数一次性生成，因此 `docker compose up --build` 无需任何手工配置；文件随卷保留，容器重启后旧游标仍可续读，多个进程同时启动也会通过原子创建共享同一份密钥。

## API

### 写入事件 `POST /events`

```json
{
  "event_id": "match-001-alice",
  "participant_id": "alice",
  "points": 10,
  "correction_of": null
}
```

| 场景 | 状态码 | 说明 |
|---|---|---|
| 首次写入 | `201` | 返回事件及分配的 `revision` |
| 相同 `event_id` + 相同载荷重放 | `200` | 返回原结果，不产生新 revision |
| 相同 `event_id` + 不同载荷 | `409` | 冲突，拒绝写入 |
| `correction_of` 引用不存在的事件 / 自引用 | `422` | 校验失败 |
| `points` 非整数或超出 64 位范围 | `422` | 仅接受有符号 64 位整数（`-2^63`～`2^63-1`）；布尔值、浮点数、数字字符串一律拒绝 |

### 更正事件

```bash
# 原事件
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e1","participant_id":"alice","points":10}'

# 第一次更正（引用 e1）
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e2","participant_id":"alice","points":12,"correction_of":"e1"}'

# 再次更正（引用 e2，形成更正链 e1 -> e2 -> e3）
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d \
  '{"event_id":"e3","participant_id":"alice","points":15,"correction_of":"e2"}'
```

链 `e1 → e2 → e3` 中：revision ≥ e3 时只有 `e3` 计入；在 e3 写入之前、e2 写入之后，只有 `e2` 计入。

### 批量写入 `POST /events/batch`

请求体为事件数组（1～100 个），单个事件的字段与 `POST /events` 完全一致。整批在**一个 `BEGIN IMMEDIATE` 事务**中完成校验与落库：

- revision 按数组顺序分配为一段**连续**编号（首个新事件取 `MAX(revision)+1`，依次递增）；
- 批内 `event_id` 必须唯一；
- `correction_of` 可引用库中已有事件，或本批中**更早**（数组下标更小）的事件；引用本批后续项、自引用或未知事件均为 `422`；
- 任一载荷冲突、非法引用或写入失败都会回滚整批——不留下任何事件，也不消耗任何 revision。

| 场景 | 状态码 | 说明 |
|---|---|---|
| 全部事件均为首次写入 | `201` | 按请求顺序返回各事件及其 revision |
| 全部事件均已存在且载荷逐项一致 | `200` | 幂等重放：按请求顺序返回**原结果**（原始 revision/created_at），不产生新 revision |
| 仅部分 `event_id` 已存在（部分重放） | `409` | 整批拒绝，不写入任何新事件 |
| 任一同 `event_id` 载荷不一致 | `409` | 整批拒绝，不写入任何新事件 |
| 批内 `event_id` 重复、自引用、引用本批后续项或未知事件 | `422` | 整批回滚 |
| 数组为空、超过 100 项、非数组或单事件字段非法（含 `points` 非整数或超出 64 位范围） | `422` | 请求体校验失败，整批回滚 |

```bash
# 一次写入 3 个事件；b2 在同批内更正更早的 b1
curl -X POST localhost:8000/events/batch \
  -H 'Content-Type: application/json' -d '[
    {"event_id":"b1","participant_id":"alice","points":10},
    {"event_id":"b2","participant_id":"alice","points":12,"correction_of":"b1"},
    {"event_id":"b3","participant_id":"bob","points":5}
  ]'
```

成功（全部为新事件）时返回 `201`，响应数组与请求同序，revision 连续：

```json
[
  {"event_id":"b1","participant_id":"alice","points":10,"correction_of":null,"revision":1,"created_at":"2026-09-17T08:00:00.000Z"},
  {"event_id":"b2","participant_id":"alice","points":12,"correction_of":"b1","revision":2,"created_at":"2026-09-17T08:00:00.000Z"},
  {"event_id":"b3","participant_id":"bob","points":5,"correction_of":null,"revision":3,"created_at":"2026-09-17T08:00:00.000Z"}
]
```

重放同一批（载荷逐项一致）返回 `200` 与完全相同的结果；重放时可以改变数组顺序，响应仍按本次请求顺序排列，但每个事件返回的都是原始结果。引用本批后续项会被拒绝：

```bash
# 422：b1 引用了同批中排在后面的 b2；整批不会写入
curl -X POST localhost:8000/events/batch \
  -H 'Content-Type: application/json' -d '[
    {"event_id":"b1","participant_id":"alice","points":10,"correction_of":"b2"},
    {"event_id":"b2","participant_id":"alice","points":12}
  ]'
```

单事件接口 `POST /events` 的行为保持不变，两种接口写入的事件互相可见（幂等重放、更正引用均互通）。

### 积分情景预演 `POST /events/preview`

以指定 revision 为基线，**只读**推演一组草案事件生效后的排行榜：不写库、不产生新 revision，可反复试算"如果接下来发生这些事件，榜单会怎样"。请求体：

| 字段 | 说明 |
|---|---|
| `base_revision` | 必填。基线 revision，`0` 表示空账本；只有 `revision ≤ base_revision` 的事件参与推演（之后的账本事件被忽略）；超过当前最大 revision 返回 `404`，负数/非整数返回 `422` |
| `events` | 必填。1～100 条草案事件，字段与 `POST /events` 完全一致（`points` 同样仅接受有符号 64 位整数，布尔/浮点/数字字符串/越界均为 `422`），按数组顺序依次生效 |

推演规则：

- 草案事件视为追加在基线之后的**虚拟 revision**（`base_revision + 1` 起按数组顺序递增）；生效判定、排序（总分降序、`participant_id` 升序）、更正链只计末端、分叉链各分支叶子均计等规则与 `/leaderboard` 完全一致；
- `correction_of` 可引用**基线中已存在**的事件，或本批中**更早**的草案；引用未知事件、本批后续草案、自引用，或引用 `base_revision` 之后才写入的账本事件，均为 `422`；
- 草案 `event_id` 与账本中**任意**已有事件（含 `base_revision` 之后的事件）重名即 `409`，无论载荷是否一致（预演没有幂等重放）；批内 `event_id` 重复为 `422`；冲突（409）检查先于引用（422）校验。

响应：

- `base_revision`：回显基线 revision；
- `projected_leaderboard`：推演后的排行榜，结构同 `GET /leaderboard`；其 `revision` 为**虚拟值**（`base_revision + 草案条数`），不消耗真实 revision，草案事件在 `effective_events` 中携带各自的虚拟 revision；
- `changes`：推演榜相对基线快照的名次/总分变化，元素结构与排序同 `GET /leaderboard/changes`（仅含发生变化的参赛者，按 `participant_id` 升序；缺席一侧为 `null`，`points_delta` 按 0 计）。

```bash
# 已有数据：rev1 alice+10，rev2 bob+7
curl -X POST localhost:8000/events/batch -H 'Content-Type: application/json' -d '[
  {"event_id":"p1","participant_id":"alice","points":10},
  {"event_id":"p2","participant_id":"bob","points":7}
]'

# 预演：在 rev2 之上追加两条草案——carol 得 20 分；d2 更正基线事件 p1（alice 10 分改 25 分）
curl -X POST localhost:8000/events/preview \
  -H 'Content-Type: application/json' -d '{
    "base_revision": 2,
    "events": [
      {"event_id":"d1","participant_id":"carol","points":20},
      {"event_id":"d2","participant_id":"alice","points":25,"correction_of":"p1"}
    ]
  }'
```

```json
{
  "base_revision": 2,
  "projected_leaderboard": {
    "revision": 4,
    "entries": [
      {
        "rank": 1,
        "participant_id": "alice",
        "total_points": 25,
        "effective_events": [
          {"event_id": "d2", "points": 25, "revision": 4, "correction_of": "p1"}
        ]
      },
      {
        "rank": 2,
        "participant_id": "carol",
        "total_points": 20,
        "effective_events": [
          {"event_id": "d1", "points": 20, "revision": 3, "correction_of": null}
        ]
      },
      {
        "rank": 3,
        "participant_id": "bob",
        "total_points": 7,
        "effective_events": [
          {"event_id": "p2", "points": 7, "revision": 2, "correction_of": null}
        ]
      }
    ]
  },
  "changes": [
    {"participant_id": "alice", "before": {"rank": 1, "total_points": 10}, "after": {"rank": 1, "total_points": 25}, "points_delta": 15},
    {"participant_id": "bob", "before": {"rank": 2, "total_points": 7}, "after": {"rank": 3, "total_points": 7}, "points_delta": 0},
    {"participant_id": "carol", "before": null, "after": {"rank": 2, "total_points": 20}, "points_delta": 20}
  ]
}
```

预演后账本保持不变（当前 revision 仍为 2）；把同样的草案真正写入 `/events` 后，`GET /leaderboard` 与 `GET /leaderboard/changes?from_revision=2&to_revision=4` 的结果与预演完全一致。

### 排行榜 `GET /leaderboard`

```bash
curl localhost:8000/leaderboard                # 当前 revision
curl 'localhost:8000/leaderboard?revision=3'   # 指定历史 revision
```

返回按**总分降序、`participant_id` 升序**排列，并含每个参赛者的生效事件明细：

```json
{
  "revision": 3,
  "entries": [
    {
      "rank": 1,
      "participant_id": "alice",
      "total_points": 15,
      "effective_events": [
        {"event_id": "e3", "points": 15, "revision": 3, "correction_of": "e2"}
      ]
    }
  ]
}
```

查询不存在的 revision（`< 1` 或大于当前最大 revision）返回 `404`。

### 排行榜分页 `GET /leaderboard/page`

面向大榜单的游标分页：游标自包含、无服务器端状态，可并发续读。首次请求固定快照 revision，之后携带 `next_cursor` 续读——即使期间持续有新写入，所有页仍读取游标绑定的同一 revision，不会重复或遗漏。

| 查询参数 | 说明 |
|---|---|
| `revision` | 可选。首次请求固定快照（默认当前 revision）；携带 `cursor` 时若显式给出，必须与游标绑定的 revision 一致，否则 `422` |
| `limit` | 每页条数，`1`–`500`，默认 `50` |
| `cursor` | 上一页响应的 `next_cursor` |

```bash
curl 'localhost:8000/leaderboard/page?limit=2'
curl 'localhost:8000/leaderboard/page?limit=2&cursor=<next_cursor>'
```

```json
{
  "revision": 3,
  "entries": [
    {
      "rank": 1,
      "participant_id": "alice",
      "total_points": 15,
      "effective_events": [
        {"event_id": "e3", "points": 15, "revision": 3, "correction_of": "e2"}
      ]
    }
  ],
  "next_cursor": "eyJ2IjoxLCJyZXYiOjMsInB0cyI6MTUsInBpZCI6ImFsaWNlIn0.<签名>"
}
```

- 排序与 `/leaderboard` 一致（总分降序、`participant_id` 升序），`rank` 跨页连续。
- 末页 `next_cursor` 为 `null`；空榜正常返回 `{"revision": 0, "entries": [], "next_cursor": null}`。
- 游标格式为 `base64url(payload).base64url(HMAC-SHA256)`，payload 含版本号、revision、末条总分与 `participant_id`；篡改、格式错误、版本不支持或与显式 `revision` 冲突均返回 `422`，未知 revision 返回 `404`。签名密钥的来源与轮换见「配置」一节——客户端无法自行签发游标，不能凭伪造游标跳到任意 revision 或排序键读取历史快照。

### 排行榜快照承诺与默克尔证明 `GET /leaderboard/commitment`、`GET /leaderboard/proof/{participant_id}`

对指定 revision 的排行榜快照做确定性编码并构建 **SHA-256 Merkle 树**，返回叶数与根摘要；任一参赛者的包含证明（条目 + 叶索引 + 逐层兄弟节点）可让客户端独立复算出同一根。纯读：历史快照只依赖 `revision ≤ R` 的行，后续写入不会改变既有 revision 的承诺。

```bash
# 快照承诺（revision 可省略，默认当前；revision=0 表示空榜）
curl 'localhost:8000/leaderboard/commitment?revision=3'

# 某参赛者的包含证明
curl 'localhost:8000/leaderboard/proof/alice?revision=3'
```

```json
// GET /leaderboard/commitment?revision=3
{"revision": 3, "leaf_count": 3, "root_hash": "629e2fd7d49e1d04c7e7c40d028082fb2ff73e6ae1f14b7da9d39239538c7b2f"}
```

```json
// GET /leaderboard/proof/bob?revision=3
{
  "revision": 3,
  "leaf_index": 2,
  "entry": {
    "rank": 3,
    "participant_id": "bob",
    "total_points": 7,
    "effective_events": [
      {"event_id": "m2", "points": 7, "revision": 2, "correction_of": null}
    ]
  },
  "proof": [
    {"level": 0, "position": "self",  "sibling": "591579e394d3da72a15857d1d0a3b5ef81245d0a390144e6ee3b4e5dd0195d2c"},
    {"level": 1, "position": "left",  "sibling": "382d8010c596b3266eb3a523ca8d35dca2fb4e94417d6610cabe95c18630ec23"}
  ],
  "root_hash": "629e2fd7d49e1d04c7e7c40d028082fb2ff73e6ae1f14b7da9d39239538c7b2f",
  "leaf_count": 3
}
```

#### 确定性编码规则

1. **叶顺序**：与 `GET /leaderboard` 完全一致——总分降序、`participant_id` 升序；叶索引从 0 开始，等于 `rank - 1`。
2. **叶载荷**：每个条目编码为规范化 JSON（UTF-8；键按字典序；无空白），含且仅含四个字段：

   ```json
   {"event_ids":["m2"],"participant_id":"bob","rank":3,"total_points":7}
   ```

   `event_ids` 是该参赛者的**生效** `event_id` 列表，按 revision 升序（空列表时为 `[]`）。多字节字符以原样 UTF-8 出现（不做 `\uXXXX` 转义）。
3. **叶哈希**：`SHA256(0x00 || 叶载荷)`（单字节前缀 `0x00` 做域分离）。
4. **内部节点**：`SHA256(0x01 || 左孩子 || 右孩子)`，孩子均为 32 字节原始摘要。
5. **奇数层**：该层最后一个节点没有右兄弟时**与自身配对**（证明中对应 `position: "self"`，兄弟摘要即节点自身）。
6. **根**：逐层向上直到只剩一个节点；**单叶树的根就是该叶哈希本身**（`proof` 为空数组）。
7. **空榜**：叶数为 0，根固定为 **SHA-256（空字节串）**，即 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`；空榜下任何参赛者的证明均为 `404`。
8. 所有摘要均以小写十六进制输出（32 字节 → 64 个 hex 字符）。

#### 客户端复算与语义

- 从 `entry` 按规则 2、3 算出叶哈希，置 `idx = leaf_index`；自底向上遍历 `proof`：
  - `position: "left"`（此时 `idx` 为奇数）：`h = SHA256(0x01 || sibling || h)`；
  - `position: "right"`（此时 `idx` 为偶数）：`h = SHA256(0x01 || h || sibling)`；
  - `position: "self"`：`h = SHA256(0x01 || h || h)`；
  - 每上一层 `idx = floor(idx / 2)`。最终 `h` 必须等于响应中的 `root_hash`，也等于同 revision 调用 `/leaderboard/commitment` 的 `root_hash`。
- `proof[].level` 为兄弟节点所在层（叶层为 0），数组按层升序；`sibling` 为兄弟节点的小写 hex 摘要。
- 两个接口对**相同 revision** 必然一致（同一快照同一棵树）；`leaf_count` 等于该 revision 排行榜条目数。
- 参赛者在该 revision 没有条目（尚未出现、或积分事件已被更正改挂给他人）返回 `404`；`revision` 超出当前最大 revision 返回 `404`；缺失/非整数/负数 `revision` 返回 `422`。
- 承诺只固定**生效状态**：同一参与者后续事件被更正后，其叶载荷中的 `event_ids`、总分、名次都会变化，根随之改变；但任一历史 revision 的根保持不变。

### 名次变化 `GET /leaderboard/changes`

比较两个 revision 快照之间的名次/总分变化。两个查询参数均**必填**：

| 查询参数 | 说明 |
|---|---|
| `from_revision` | 起点 revision，允许传 `0` 表示空榜；须满足 `0 ≤ from_revision` |
| `to_revision` | 终点 revision，须满足 `from_revision < to_revision ≤ 当前 revision` |

- 顺序非法（缺参/非整数/负数、`from_revision ≥ to_revision`）返回 `422`；
- 超出当前 revision 范围返回 `404`；
- 响应仅包含**总分或名次发生变化**的参与者，按 `participant_id` 升序输出；
- `before`/`after` 在对应快照中不存在时为 `null`，存在时含 `rank` 与 `total_points`；`points_delta = after.total_points - before.total_points`（缺席一侧按 0 计）；
- 生效规则与排行榜一致：每个 revision 只计更正链当时的末端事件。若更正把某事件从参赛者 A 改挂到 B，同一响应中会同时出现 A 退出（`after: null`）与 B 进入（`before: null`）。

```bash
# 空榜（revision 0）到当前 revision 的全部变化
curl 'localhost:8000/leaderboard/changes?from_revision=0&to_revision=4'
```

```json
{
  "from_revision": 0,
  "to_revision": 4,
  "changes": [
    {
      "participant_id": "alice",
      "before": null,
      "after": {"rank": 2, "total_points": 12},
      "points_delta": 12
    },
    {
      "participant_id": "bob",
      "before": null,
      "after": {"rank": 3, "total_points": 5},
      "points_delta": 5
    },
    {
      "participant_id": "carol",
      "before": null,
      "after": {"rank": 1, "total_points": 20},
      "points_delta": 20
    }
  ]
}
```

更正把事件从 `alice` 改挂到 `dave`（`from_revision=4&to_revision=5`）时：

```json
{
  "from_revision": 4,
  "to_revision": 5,
  "changes": [
    {
      "participant_id": "alice",
      "before": {"rank": 2, "total_points": 12},
      "after": null,
      "points_delta": -12
    },
    {
      "participant_id": "dave",
      "before": null,
      "after": {"rank": 2, "total_points": 12},
      "points_delta": 12
    }
  ]
}
```

### 事件变化 `GET /events/changes`

比较两个 revision 快照之间的**生效事件**差异，按更正链根归组输出。两个查询参数均**必填**：

| 查询参数 | 说明 |
|---|---|
| `from_revision` | 起点 revision，允许传 `0` 表示空账本；须满足 `0 ≤ from_revision` |
| `to_revision` | 终点 revision，须满足 `from_revision < to_revision ≤ 当前 revision` |

- 校验顺序与 `/leaderboard/changes` 一致：顺序非法（缺参/非整数/负数、`from_revision ≥ to_revision`）返回 `422`，超出当前 revision 范围返回 `404`；
- 生效规则与排行榜一致：每个 revision 只计更正链当时的末端事件；每个生效事件沿 `correction_of` 追溯到**链根**（链上首个未被更正的事件）；
- 同一链根下，两个快照中**相同 `event_id`** 的生效事件先相互抵消；抵消后若恰好只剩一进一出，合并为一条 `replaced`，否则每个剩余事件分别记为 `added`（仅在终点生效）或 `removed`（仅在起点生效）；
- 每项携带 `chain_root`（链根 `event_id`）与 `before`/`after` **完整事件**（字段同 `GET /events/{event_id}`；`added` 的 `before`、`removed` 的 `after` 为 `null`）；
- 按 `chain_root`、再按 after（无则 before）的 `revision` 与 `event_id` 升序输出。

```bash
# e2 更正 e1：链 e1 的生效事件由 e1 变为 e2
curl 'localhost:8000/events/changes?from_revision=1&to_revision=2'
```

```json
{
  "from_revision": 1,
  "to_revision": 2,
  "changes": [
    {
      "chain_root": "e1",
      "change_type": "replaced",
      "before": {"event_id": "e1", "participant_id": "alice", "points": 10, "correction_of": null, "revision": 1, "created_at": "2026-09-17T08:00:00.000Z"},
      "after": {"event_id": "e2", "participant_id": "alice", "points": 12, "correction_of": "e1", "revision": 2, "created_at": "2026-09-17T08:00:01.000Z"}
    }
  ]
}
```

### 积分归因 `GET /participants/{participant_id}/score-breakdown`

按更正链拆解某个参赛者在指定快照上的积分来源。

| 查询参数 | 说明 |
|---|---|
| `revision` | 可选。固定快照 revision，默认当前；允许传 `0` 表示空账本 |

- 生效规则与 `/leaderboard` 一致：revision R 上每条更正链只计当时的末端事件；仅当链的**生效末端属于目标参赛者**时该链才计入；
- 响应返回固定的快照 `revision`、`total_points`，以及按链根 `event_id` **升序**排列的 `contributions`（同链根分叉出多个生效末端时各成一项，再按生效事件的 revision、`event_id` 升序）；
- 每项含 `chain_root`（链根 `event_id`）、`effective_event`（该快照的生效事件，完整字段同 `GET /events/{event_id}`）与 `history`（从根到该生效事件的完整链，按 revision 升序）；
- 更正把积分转给其他参赛者后，旧参赛者不再保留该链；新归属者的 `history` 仍完整展示此前归属（含他人事件）；
- `revision` 超过当前最大值返回 `404`，负数或非整数返回 `422`；无贡献（含未知参赛者、空账本）时返回 `total_points: 0` 与空数组。

```bash
curl 'localhost:8000/participants/alice/score-breakdown?revision=3'
```

```json
{
  "revision": 3,
  "total_points": 15,
  "contributions": [
    {
      "chain_root": "e1",
      "effective_event": {"event_id": "e3", "participant_id": "alice", "points": 15, "correction_of": "e2", "revision": 3, "created_at": "2026-09-17T08:00:02.000Z"},
      "history": [
        {"event_id": "e1", "participant_id": "alice", "points": 10, "correction_of": null, "revision": 1, "created_at": "2026-09-17T08:00:00.000Z"},
        {"event_id": "e2", "participant_id": "alice", "points": 12, "correction_of": "e1", "revision": 2, "created_at": "2026-09-17T08:00:01.000Z"},
        {"event_id": "e3", "participant_id": "alice", "points": 15, "correction_of": "e2", "revision": 3, "created_at": "2026-09-17T08:00:02.000Z"}
      ]
    }
  ]
}
```

### 双方对比 `GET /participants/compare`

逐 revision 对比两位参赛者的有效总分走势，把连续且双方分数都相同的快照合并为**闭区间**输出。四个查询参数均**必填**：

| 查询参数 | 说明 |
|---|---|
| `left_id` | 左方参赛者 |
| `right_id` | 右方参赛者 |
| `from_revision` | 起点 revision，允许传 `0` 表示空账本；须满足 `0 ≤ from_revision ≤ to_revision` |
| `to_revision` | 终点 revision，须满足 `to_revision ≤ 当前 revision` |

- 校验顺序与 `/leaderboard/changes` 一致：顺序非法（缺参/非整数/负数、`from_revision > to_revision`）返回 `422`，`to_revision` 超出当前 revision 返回 `404`；
- 对区间内**每个 revision** 按与 `/leaderboard` 相同的生效规则计算双方有效总分（每条更正链只计当时的末端事件）；
- 每个区间含 `start_revision`、`end_revision`（闭区间）、`left_points`、`right_points` 与 `leader`（`left`：左方领先；`right`：右方领先；`tie`：持平，含双方均为 0）；
- 更正把事件从一方改挂到另一方时，原归属方扣分与新归属方加分在**同一个 revision**（即该更正的 revision）同时生效，区间边界随之落在该 revision 上；
- 双方始终无分（含未知参赛者）时不报错，返回覆盖整个查询范围的单个 `tie` 区间。

```bash
# 构造数据：rev1 alice+10，rev2 bob+5，rev3 carol+7（第三方），
#          rev4 bob+5，rev5 bob+5，rev6 alice+10
curl -X POST localhost:8000/events/batch -H 'Content-Type: application/json' -d '[
  {"event_id":"c1","participant_id":"alice","points":10},
  {"event_id":"c2","participant_id":"bob","points":5},
  {"event_id":"c3","participant_id":"carol","points":7},
  {"event_id":"c4","participant_id":"bob","points":5},
  {"event_id":"c5","participant_id":"bob","points":5},
  {"event_id":"c6","participant_id":"alice","points":10}
]'

curl 'localhost:8000/participants/compare?left_id=alice&right_id=bob&from_revision=0&to_revision=6'
```

```json
{
  "left_id": "alice",
  "right_id": "bob",
  "from_revision": 0,
  "to_revision": 6,
  "intervals": [
    {"start_revision": 0, "end_revision": 0, "left_points": 0, "right_points": 0, "leader": "tie"},
    {"start_revision": 1, "end_revision": 1, "left_points": 10, "right_points": 0, "leader": "left"},
    {"start_revision": 2, "end_revision": 3, "left_points": 10, "right_points": 5, "leader": "left"},
    {"start_revision": 4, "end_revision": 4, "left_points": 10, "right_points": 10, "leader": "tie"},
    {"start_revision": 5, "end_revision": 5, "left_points": 10, "right_points": 15, "leader": "right"},
    {"start_revision": 6, "end_revision": 6, "left_points": 20, "right_points": 15, "leader": "left"}
  ]
}
```

其中 rev2→rev3 合并为一个区间：rev3 是第三方 carol 的事件，双方分数均未变化。

### 积分阈值区间 `GET /participants/{participant_id}/threshold-intervals`

逐 revision 重算**单个参赛者**的真实生效总分，把"总分大于等于阈值"的连续 revision 合并为**闭区间**返回。三个查询参数均**必填**：

| 查询参数 | 说明 |
|---|---|
| `threshold` | 积分阈值，任意整数（含负数）；总分 `>= threshold` 即为命中（边界含等号） |
| `from_revision` | 起点 revision，允许传 `0` 表示空账本；须满足 `0 ≤ from_revision ≤ to_revision` |
| `to_revision` | 终点 revision，须满足 `to_revision ≤ 当前 revision` |

- 校验顺序与 `/participants/compare` 一致：顺序非法（缺参/非整数/负数、`from_revision > to_revision`）返回 `422`，`to_revision` 超出当前 revision 返回 `404`（顺序校验先于范围校验）；
- 对区间内**每个 revision** 按与 `/leaderboard` 相同的生效规则重算该参赛者总分（每条更正链只计当时的末端事件，分叉链各分支叶子都计）；
- 总分连续 `≥ threshold` 的 revision 合并为一个闭区间，区间按 `start_revision` **升序**；每个区间含 `start_revision`、`end_revision` 与两端积分 `start_points`、`end_points`（区间内部积分可以继续变化，只要始终不低于阈值）；
- 更正把事件从 A 改挂到 B 时，A 扣分与 B 加分在**同一个 revision**（即该更正的 revision）生效：A 可能因此跌出阈值，B 可能因此升入阈值；更正链分叉导致的加分（各分支叶子都计）同样体现；
- 未在账本中出现过的参赛者按每个 revision **0 分**处理（阈值 `≤ 0` 时仍可能命中）；查询**只读**，不写入账本、不产生新 revision。

```bash
# 构造数据：rev1 alice+10，rev2 bob+100（第三方，不影响 alice），rev3 alice+5，
#          rev4 更正 e1 改挂给 bob（alice 由 15 降为 5），rev5 alice+8（13）
curl -X POST localhost:8000/events/batch -H 'Content-Type: application/json' -d '[
  {"event_id":"t1","participant_id":"alice","points":10},
  {"event_id":"t2","participant_id":"bob","points":100},
  {"event_id":"t3","participant_id":"alice","points":5},
  {"event_id":"t4","participant_id":"bob","points":10,"correction_of":"t1"},
  {"event_id":"t5","participant_id":"alice","points":8}
]'

curl 'localhost:8000/participants/alice/threshold-intervals?threshold=10&from_revision=0&to_revision=5'
```

alice 的逐 revision 总分为 rev0=0、rev1=10、rev2=10、rev3=15、rev4=5（跨参赛者更正使其跌出）、rev5=13，故命中 `[1,3]` 与 `[5,5]`：

```json
{
  "participant_id": "alice",
  "threshold": 10,
  "from_revision": 0,
  "to_revision": 5,
  "intervals": [
    {"start_revision": 1, "end_revision": 3, "start_points": 10, "end_points": 15},
    {"start_revision": 5, "end_revision": 5, "start_points": 13, "end_points": 13}
  ]
}
```

阈值允许为负数或 0（用于识别"不低于 0"的区间）；全程未出现的参赛者恒为 0 分，例如查询 `ghost`、`threshold=0` 时返回覆盖整个范围的单个区间，而 `threshold=1` 时返回空数组。空账本下仅可查 `from_revision=to_revision=0`，其余 revision 返回 `404`。

### 奖励结算预演 `POST /rewards/preview`

按指定 revision 的排行榜**只读**试算奖金分配，不写入账本、不产生新 revision。请求体：

| 字段 | 说明 |
|---|---|
| `revision` | 可选。快照 revision，默认当前；仅接受 JSON 整数或 `null`——布尔值、数字字符串、浮点数（含 `1.0` 这类整数形式）一律 `422`；越界（`< 1` 或大于当前最大 revision）返回 `404` |
| `prize_cents` | 必填。按名次排列的奖金（单位：分），`1`～`100` 个**非负 JSON 整数**——布尔值、数字字符串、浮点数（含整数形式）一律 `422`，错误位置精确到数组下标；`prize_cents[0]` 为第 1 名奖金，第 `1`～`len(prize_cents)` 名构成有奖区 |

结算规则：

- 排行榜按与 `/leaderboard` 相同的生效规则计算（每条更正链只计当时的末端事件）；
- 总分相同者构成一个**并列组**，覆盖连续名次区间 `[rank_start, rank_end]`；
- 只要组内任一名次落入有奖区，就把该组覆盖的**有奖名次奖金合并**成全组奖金池，在全组**均分**；余下的分币按 `participant_id` 升序每人 1 分依次分配，保证发放总额与奖金池相等；
- 完全落在有奖区之外的组不产生获奖者；奖金为 `0` 的名次仍属有奖区（该名次获奖金额为 0）。

响应返回固定的 `revision` 与 `winners` 数组（按名次区间、再按 `participant_id` 升序），每项含 `participant_id`、`rank_start`、`rank_end`、`total_points`、`prize_cents`。

```bash
# 构造数据：alice 15 分，bob 与 carol 并列 10 分
curl -X POST localhost:8000/events/batch -H 'Content-Type: application/json' -d '[
  {"event_id":"r1","participant_id":"alice","points":15},
  {"event_id":"r2","participant_id":"bob","points":10},
  {"event_id":"r3","participant_id":"carol","points":10}
]'

# 试算前三名奖金 1000 / 500 / 250 分的分配（当前 revision）
curl -X POST localhost:8000/rewards/preview \
  -H 'Content-Type: application/json' -d \
  '{"prize_cents":[1000,500,250]}'

# 指定历史 revision 试算
curl -X POST localhost:8000/rewards/preview \
  -H 'Content-Type: application/json' -d \
  '{"revision":1,"prize_cents":[1000,500,250]}'
```

bob 与 carol 并列，组覆盖第 2～3 名，奖金池 500+250=750 由两人均分：

```json
{
  "revision": 3,
  "winners": [
    {"participant_id": "alice", "rank_start": 1, "rank_end": 1, "total_points": 15, "prize_cents": 1000},
    {"participant_id": "bob", "rank_start": 2, "rank_end": 3, "total_points": 10, "prize_cents": 375},
    {"participant_id": "carol", "rank_start": 2, "rank_end": 3, "total_points": 10, "prize_cents": 375}
  ]
}
```

### 更正分叉审计 `GET /audit/correction-forks`

按指定快照还原全部更正关系，审计**更正分叉**：快照内同一事件被至少两个事件**直接更正**即构成分叉（链上每个事件最多有一个 `correction_of` 父节点，因此更正关系是一片森林；分叉点即拥有 ≥2 个子节点的事件）。分叉之后的链式更正仍归入各自分支；每个节点至多一个直接更正的**普通线性链不返回**，独立事件也不返回。

| 查询参数 | 说明 |
|---|---|
| `revision` | 可选。固定快照 revision，默认当前；允许传 `0` 表示空账本（返回空数组）；超过当前最大 revision 返回 `404`，负数或非整数返回 `422` |

- 生效规则与 `/leaderboard` 一致：revision R 上事件 e 生效 ⟺ `e.revision ≤ R` 且不存在 `c.revision ≤ R` 的更正 `c.correction_of = e.event_id`；分叉链的每个分支只计该快照上的**生效叶子**（被更正的根与中间节点都不计）；
- 响应仅包含**存在分叉的链根**，按 `chain_root` 升序；
- 每项含：
  - `chain_root`：链根 `event_id`（沿 `correction_of` 上溯到的、自身未更正任何事件的起点）；
  - `leaves`：从根到每个生效叶子的完整 `event_id` 路径 `path`（含根与叶子，按 revision 升序），以及叶子的 `participant_id`、`points`、`revision`；叶子按 `revision`、再按 `event_id` 升序；
  - `participant_points`：该链生效叶子按 `participant_id` 汇总的积分（同一参赛者在多个分支持有的叶子积分相加；被取代的根/中间节点不参与），按 `participant_id` 升序；
- 接口**只读**，不写入账本、不产生新 revision。

```bash
# 构造分叉：e1 同时被 e2、e3 更正，e3 又被 e4 链式更正（归入 e3 分支）
curl -X POST localhost:8000/events/batch -H 'Content-Type: application/json' -d '[
  {"event_id":"e1","participant_id":"alice","points":10},
  {"event_id":"e2","participant_id":"alice","points":12,"correction_of":"e1"},
  {"event_id":"e3","participant_id":"alice","points":20,"correction_of":"e1"},
  {"event_id":"e4","participant_id":"bob","points":25,"correction_of":"e3"}
]'

curl localhost:8000/audit/correction-forks
```

```json
{
  "revision": 4,
  "forks": [
    {
      "chain_root": "e1",
      "leaves": [
        {"path": ["e1", "e2"], "participant_id": "alice", "points": 12, "revision": 2},
        {"path": ["e1", "e3", "e4"], "participant_id": "bob", "points": 25, "revision": 4}
      ],
      "participant_points": [
        {"participant_id": "alice", "total_points": 12},
        {"participant_id": "bob", "total_points": 25}
      ]
    }
  ]
}
```

空账本或 `revision=0` 返回 `{"revision": 0, "forks": []}`；快照中尚不存在分叉（例如分叉的第二个分支写入之前）时 `forks` 同样为空。

### 路径中的不透明 ID（URL 编码）

`event_id` 与 `participant_id` 都是不透明字符串：写入时不限制字符集（1～128 个字符），可以包含 `/`、空格、非 ASCII 字符，也可以与静态路由同名（如 `changes`、`compare`）。为了让任何合法 ID 都能精确回读，携带 ID 的 GET 路由——`GET /events/{event_id}`、`GET /leaderboard/proof/{participant_id}`、`GET /participants/{participant_id}/score-breakdown`、`GET /participants/{participant_id}/threshold-intervals`——基于**原始（未解码）URL 路径**匹配，再把捕获到的 ID 段做百分号解码：

- ID 中的保留字符按 RFC 3986 百分号编码后放入路径：`/` → `%2F`、`%` → `%25`、空格 → `%20`，非 ASCII 字符使用 UTF-8 百分号编码；
- 与静态路由同名的 ID 把**至少一个字符**编码即可精确命中：`GET /events/%63hanges` 返回名为 `changes` 的事件，而 `GET /events/changes` 仍是事件变化接口（静态路由行为不变）；
- 未编码的 `/` 仍是路径分隔符：`GET /events/a/b` 不会匹配事件 `a/b`（应请求 `/events/a%2Fb`）；
- 不存在的 ID 一律返回 `404`（`score-breakdown` 对未出现的参赛者仍按既有语义返回 0 分与空数组）。

```bash
# 写入：名为 changes 的事件、ID 含斜杠的事件、ID 含斜杠的参赛者
curl -X POST localhost:8000/events/batch -H 'Content-Type: application/json' -d '[
  {"event_id":"changes","participant_id":"alice","points":3},
  {"event_id":"a/b","participant_id":"x/y","points":5}
]'

curl localhost:8000/events/%63hanges     # 名为 changes 的事件（编码后绕过静态路由）
curl localhost:8000/events/a%2Fb         # 名为 a/b 的事件
curl 'localhost:8000/events/changes?from_revision=0&to_revision=2'   # 静态路由：事件变化

curl localhost:8000/leaderboard/proof/x%2Fy                # 参赛者 x/y 的默克尔证明
curl localhost:8000/participants/x%2Fy/score-breakdown     # 参赛者 x/y 的积分归因
curl 'localhost:8000/participants/x%2Fy/threshold-intervals?threshold=5&from_revision=0&to_revision=2'
```

### 其他端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` | `/events` | 全部事件（按 revision 升序） |
| `GET` | `/events/{event_id}` | 单个事件（ID 含保留字符或与静态路由同名时需 URL 编码，见上节） |

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LEDGER_DB_PATH` | `/data/ledger.db` | SQLite 数据库文件路径。其所在目录同时用于存放自动生成的游标密钥文件 `cursor_secret`。 |
| `LEADERBOARD_CURSOR_SECRET` | 留空：自动生成并持久化 | `/leaderboard/page` 游标的 HMAC-SHA256 签名密钥。设置为非空值时始终优先使用；**未设置（或为空）时不使用任何内置固定默认值**，而是在数据库同目录下用安全随机数一次性生成 `cursor_secret`（权限 `0600`）。原子创建保证并发启动的多个进程共享同一份密钥；文件随数据目录持久化，重启后旧游标继续有效。文件无法创建/读取、内容为空或权限异常时，服务**启动失败**而不是退回公开默认值。建议需要自行掌控密钥时覆盖为强随机值（如 `openssl rand -hex 32`）。 |

**覆盖与轮换会令旧游标失效**：设置/更换 `LEADERBOARD_CURSOR_SECRET`，或删除、改写 `cursor_secret` 文件后，以旧密钥签发的游标全部失效，续读返回 `422`（数据本身不受影响，从头重新分页即可）。因此轮换密钥应在可接受进行中的分页续读中断时进行；多副本部署只需给所有副本配置同一个环境变量值。

`docker-compose.yml` 中通过 `LEADERBOARD_CURSOR_SECRET: ${LEADERBOARD_CURSOR_SECRET:-}` 注入：不配置时变量为空，服务在 `ledger-data` 卷（`/data`）中自动生成并保留密钥，`docker compose up --build` 单命令即可启动；可在 shell 环境或 `.env` 文件中覆盖以显式指定。

> 注意：`cursor_secret` 文件由数据目录写权限保护。不要把该文件提交到镜像或公开位置；它与数据库位于同一持久化卷，备份数据库时应一并妥善保管。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
LEDGER_DB_PATH=./ledger.db uvicorn app.main:app --port 8000
```

不设置 `LEADERBOARD_CURSOR_SECRET` 时会在数据库同目录（上例为当前目录）生成 `cursor_secret`，删除它即相当于轮换密钥（旧游标失效，下次启动重新生成）。

## 测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

测试覆盖：幂等重放、409 冲突、更正链末端计数、历史 revision 查询、排行榜排序、50 路并发写入下 revision 唯一且连续，`/leaderboard/page` 游标分页（快照固定、并发续读、篡改/冲突返回 422、末页与空榜），游标密钥（未配置时在数据目录一次性生成 `0600` 随机密钥且不再使用公开默认值、用旧公开默认值或任意猜测密钥伪造游标返回 422、线程与多进程并发初始化共享同一文件无残留临时文件、重启后旧游标仍可续读、空环境变量视同未配置、显式 `LEADERBOARD_CURSOR_SECRET` 优先且轮换后旧游标 422、数据目录不可创建/不可写、密钥文件为空/不可读均启动失败而非退回固定值），`/leaderboard/changes`（空榜对比、仅名次变化、更正链 A 退出/B 进入、参数顺序 422、超范围 404、按 `participant_id` 排序），`/events/changes`（空账本对比、更正合并为 `replaced`、链分叉记 `added`、非一进一出不合并、before/after 完整事件、按链根排序、参数顺序 422、超范围 404、路由不被 `/events/{event_id}` 遮蔽），`/events/batch`（连续 revision 与同序响应、批内唯一、引用更早项/后续项/未知项、全量重放 200、部分重放与异载荷 409、失败整批回滚不耗 revision、批量并发下 revision 块连续），以及 `/participants/{participant_id}/score-breakdown`（空账本与未知参赛者返回 0 与空数组、更正链只计末端且 history 完整、历史 revision 固定快照、revision 0 空快照、更正转移后旧参赛者不再保留该链而新归属者 history 展示此前归属、按链根升序、分叉链每末端各成一项、总分与 `/leaderboard` 一致、默认取当前 revision、负数 422、超范围 404），以及 `/participants/compare`（区间合并与边界、子范围与 `from == to`、更正改挂在同一 revision 双方同时变动、更正链只计末端、分叉链只扣一次、双方无分/未知参赛者返回覆盖全范围的 tie、双方同一人恒为 tie、空账本 0→0、区间无缝覆盖整个范围、逐 revision 与 `/leaderboard` 总分一致、缺参/非整数/负数/`from > to` 返回 422、超范围 404、顺序校验先于范围校验、字面路由不被遮蔽），以及 `/participants/{participant_id}/threshold-intervals`（区间合并与边界含等号、区间两端积分可不同、子范围与 `from == to`、阈值为负/0 与 revision 0、未出现参赛者按 0 分、跨参赛者更正使旧归属方跌出新归属方升入、更正链只计末端、分叉链各分支叶子均计且共享祖先只扣一次、逐 revision 与 `/leaderboard` 总分一致、区间按起点升序、缺参/非整数/负数/`from > to` 返回 422、超范围 404、顺序校验先于范围校验、查询只读不写账本），以及 `/rewards/preview`（空账本返回空获奖者、按名次发奖、并列组合并奖金池均分、余数分币按 `participant_id` 升序分配且总额守恒、组跨有奖区边界只合并区内名次、区外组不获奖、0 奖金名次仍在区内、指定历史 revision 快照、更正链只计末端、越界 revision 返回 404、revision 与奖金项仅接受 JSON 整数——布尔/数字字符串/整数形式浮点数均 422 且错误位置精确到嵌套字段与数组下标、奖金数组 1～100 个非负整数校验 422、显式 `null` revision 取当前快照、非法请求与预演均不写入账本），以及 `/audit/correction-forks`（空账本与 revision 0 返回空、普通线性链与独立事件不返回、分叉列出各分支根到生效叶子的完整路径、分叉后的链式更正归入各自分支、非根节点分叉与多个分叉点共享链根、按参赛者汇总生效积分且被取代节点不计、历史 revision 固定快照、分叉形成前后的快照差异、`chain_root` 与叶子 `(revision, event_id)` 稳定排序、批量写入的分叉可见、默认最新、超范围 404、负数/非整数 422、只读不写账本），以及 `/events/preview`（空账本基线推演、虚拟 revision 与草案生效明细、基线固定快照忽略后续账本事件、base_revision 0 忽略全账本、草案更正基线事件只计末端、批内草案链式更正、草案分叉基线链保留各分支叶子、更正改挂产生退出/进入、并列按 `participant_id` 排序且名次连续、仅名次变化与无变化空 changes、与基线及基线后账本事件重名 409、409 先于 422、引用后续草案/未知事件/基线后账本事件/自引用 422、批内重复 422、越界基线 404、负数/非整数/缺参 422、草案条数 1～100 校验、预演不写库不耗 revision、推演结果与真实写入后的 `/leaderboard` 及 `/leaderboard/changes` 一致），以及 `/leaderboard/commitment` 与 `/leaderboard/proof/{participant_id}`（空榜根固定为 `SHA256("")` 且叶数为 0、空榜任何参赛者 404、叶序等于排名顺序、叶数与 revision 字段、默认取最新 revision、叶索引等于 rank-1、proof 的 entry 与 `/leaderboard` 一致、用独立实现的编码逐层复算每个参赛者的根、奇数层末节点与自身配对（3/5 叶）、各 `position`（left/right/self）与层级兄弟摘要布局、单叶树根即叶哈希且 proof 为空、两叶树无 self 步、UTF-8 参赛者 ID 的载荷编码、缺席/未出生参赛者 404、更正后生效 event_id 与根变化、历史快照在后续写入（含更正改挂）后根与证明保持不变、每个 revision 两接口一致、超范围 404 与负数/非整数 422），以及不透明 ID 回读（名为 `changes` 的事件经 `%63hanges` 精确查询且 `/events/changes` 静态行为不变、含 `/`/`%`/空格/非 ASCII 的 `event_id` 经百分号编码回读、未编码 `/` 仍 404、含 `/` 的 `participant_id` 在证明/积分归因/阈值区间接口经 `%2F` 查询、名为 `compare` 的参赛者、未知编码 ID 仍 404、尾部斜杠重定向保持、OpenAPI 路径与编码说明）。

## 设计要点

- **存储**：单表 `events`（`event_id` 主键，`revision` 唯一），WAL 模式。
- **幂等**：请求载荷序列化为规范化 JSON（键排序）随事件存储；重放时比对，一致返回原结果，不一致返回 409。批量接口仅当全部 `event_id` 均已存在且逐项一致时按原结果返回 200；部分已存在或任一异载荷返回 409。
- **批量原子性**：`/events/batch` 在单个 `BEGIN IMMEDIATE` 事务内完成重复/冲突检查、引用校验与全部插入；`correction_of` 仅可指向库中事件或同批更早下标。revision 取锁内 `MAX(revision)+1` 后按数组顺序连续分配，任何失败均回滚，不留下事件也不消耗 revision。
- **生效判定**：revision R 上事件 e 生效 ⟺ `e.revision ≤ R` 且不存在 `c.revision ≤ R` 的更正事件 `c.correction_of = e.event_id`。
- **单次回放引擎**：`ledger.py` 的 `SnapshotTimeline` 是按 revision 逐格回放的统一引擎：调用方以**一次**范围查询（`revision ≤ to_revision`，按 revision 升序）读入事件集，引擎随后增量回放，绝不为每个 revision 重查数据库。引擎统一维护事件生效（写入当 revision 即生效）、首次被更正（仅首个直接更正当 revision 令其失效；分叉链共享祖先只失效一次，各分支叶子保留）、分叉叶子与链根/根叶路径，以及跨参赛者归属变化（首个更正当 revision 记一条 `OwnershipTransfer`：旧归属扣分、新归属加分），并按需产出指定参赛者总分（`total_for`）、全榜快照（`rank_positions`）或分叉视图。`/participants/compare`、`/participants/{id}/threshold-intervals`、`/leaderboard/changes` 与 `/audit/correction-forks` 全部基于这一个引擎，因此各接口对同 revision 的生效判定、排序与名次天然一致；`revision == 0` 为空引擎空快照。
- **事件差异**：`/events/changes` 复用同一生效判定取两个快照的生效事件，沿 `correction_of` 上溯到链根后按链根归组；同链根下相同 `event_id` 抵消，剩余一进一出合并为 `replaced`，否则逐项记 `added`/`removed`，按 `(chain_root, after/before 的 revision, event_id)` 排序。
- **分叉审计**：`/audit/correction-forks` 复用 `SnapshotTimeline` 的单次范围读回放（`revision ≤ R`）建立 `correction_of` 森林；任一事件在快照内有 ≥2 个直接更正（子节点）即为分叉，收集其链根（由引擎的 `fork_roots()` / `chain_path()` 给出）。每个分叉链列出引擎维护的全部无后任节点（即各分支生效叶子，与 `/leaderboard` 生效判定一致）及沿父链回溯的根→叶完整路径，并把叶子积分按 `participant_id` 汇总；无分叉的线性链不输出。纯读，默认最新，0 为空快照，超范围 404。
- **积分归因**：`/participants/{participant_id}/score-breakdown` 在单个读快照内取 `revision ≤ R` 的全部事件，未被更正的事件即各链生效末端；属于目标参赛者的末端沿 `correction_of` 上溯到链根，生成 `(chain_root, effective_event, history)` 三元组，按链根 `event_id` 升序输出；`total_points` 为各生效末端积分之和，与 `/leaderboard` 同 revision 的总分一致。
- **双方对比**：`/participants/compare` 与 `/participants/{id}/threshold-intervals`、`/leaderboard/changes`、`/audit/correction-forks` 共用 `ledger.py` 中的 `SnapshotTimeline` 引擎：单次读快照取出 `revision ≤ to_revision` 的全部事件（**范围查询只读一次**），之后引擎按 revision 逐格回放，回放过程中不再访问数据库。回放统一维护四类事实——事件写入即生效、事件被**首个**直接更正取代（后到的分叉更正只新增分支、不再重复扣减共享祖先）、更正链分叉时各分支叶子均生效、跨参赛者更正把原归属方扣分与新归属方加分记在同一 revision（`OwnershipTransfer`）。对比接口在逐 revision 回放时读取同一引擎状态的双方总分，相邻且双方分数都相同的快照合并为闭区间，与逐 revision 调用 `/leaderboard` 的结果一致。
- **阈值区间**：`/participants/{participant_id}/threshold-intervals` 复用同一个 `SnapshotTimeline` 单次范围读回放（`/participants/compare`、`/leaderboard/changes` 与 `/audit/correction-forks` 亦共用该引擎），只读取目标参赛者一人的当前有效总分：事件写入时若归属该参赛者则计入其名下，其**首个**直接更正在更正所在 revision 让原事件失效（跨参赛者改挂因此一升一降落在同一 revision；分叉链各分支叶子均有效、共享祖先只失效一次）。回放经过 `[from_revision, to_revision]` 内每个 revision，把总分 `≥ threshold`（含边界）的连续快照合并为闭区间，记录两端积分并按起点升序输出；未出现的参赛者全程为 0。校验顺序：参数顺序 422 → `to_revision` 超范围 404。纯读：只开只读事务，不写账本、不消耗 revision。
- **奖励预演**：`/rewards/preview` 复用 `/leaderboard` 的生效判定与排序，在单个读快照内计算指定 revision 的排行榜（纯读，不写账本）。总分相同者按 `participant_id` 升序构成并列组，覆盖连续名次区间；组与有奖区（第 1～N 名，N 为奖金数组长度）有交集时，将组覆盖的区内名次奖金合并为全组奖金池均分，余数分币按 `participant_id` 升序每人 1 分——发放总额恒等于奖金池。
- **情景预演**：`/events/preview` 在单个读快照内取出 `revision ≤ base_revision` 的基线事件，与草案事件（虚拟 revision 为 `base_revision + 数组下标 + 1`）合并后套用账本同一生效判定——被任一后续事件（含草案）更正的事件不计，分叉链各分支叶子均计；聚合、排序、名次与 `changes` 差异计算直接复用 `/leaderboard` 与 `/leaderboard/changes` 的实现。校验顺序：基线越界 404 → 草案 `event_id` 与账本任意事件重名 409 → 非法引用 422。全程只读：不开写事务、不插入事件、不消耗 revision。
- **并发**：所有写操作在 `BEGIN IMMEDIATE` 事务内先取写锁、再计算 `MAX(revision)+1` 并插入（批量为一段连续块），配合 `busy_timeout`，写者完全串行；读者走 WAL 快照，互不阻塞。
- **游标分页**：`/leaderboard/page` 的游标经 HMAC-SHA256 签名，绑定 revision 与末条排序键 `(total_points, participant_id)`，客户端无法自行签发任意 revision/排序键的游标；续读始终落在游标指定的 revision 上，配合 WAL 快照读，写入并发进行也不会造成分页漂移。密钥优先取 `LEADERBOARD_CURSOR_SECRET`；未配置时在数据库同目录以安全随机数原子生成 `cursor_secret`（临时文件 `0600` + `os.link`，并发启动共享同一值）并持久化，重启后继续验证旧游标；文件缺失且无法创建、为空或权限异常均令启动失败，不存在可被公开利用的内置固定默认值。
- **快照承诺**：`/leaderboard/commitment` 与 `/leaderboard/proof/{participant_id}` 在同一个只读快照事务内复用 `/leaderboard` 的生效判定（`revision ≤ R` 且未被快照内更正取代）、`(-total_points, participant_id)` 排序与连续名次生成条目；叶载荷为规范化 JSON（键排序、无空白、UTF-8，含 `rank`/`participant_id`/`total_points`/按 revision 升序的生效 `event_ids`），叶哈希与内部节点分别以 `0x00`/`0x01` 前缀域分离。奇数层末节点与自身配对，单叶树根即叶哈希，空树根固定为 `SHA256("")`。证明自叶层向上给出兄弟摘要与相对位置（left/right/self），两接口对同一 revision 构建同一棵树；所有输入行都由 revision 上界固定，历史承诺不受后续写入影响。纯读：只开只读事务，不写账本、不消耗 revision。
- **编码路径路由**：携带不透明 ID 的 GET 路由（含与之同前缀的静态路由 `/events/changes`、`/participants/compare`）使用自定义 `EncodedPathRoute`（`app/routing.py`）：匹配基于 ASGI `raw_path`（仍百分号编码的路径），捕获的 ID 段在匹配成功后再做百分号解码——`%2F` 不会被拆成路径分段，`%63hanges` 之类的编码形式也不会撞上字面静态路由；未编码的 `/` 依旧是路径分隔符。当 `raw_path` 缺失或与 `path` 不一致（如尾部斜杠重定向的二次匹配）时回退到默认的解码路径匹配，重定向等既有行为不变。
