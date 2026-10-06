# 园区访客通行规划服务（Park Route Planner）

面向园区访客的**离线**通行规划 HTTP 服务。导入一张带方向、通行秒数、台阶标记的
步道图，以及按**半开时间区间** `[start, end)` 生效的封闭记录；给定起终点、出发
时刻与是否避台阶，返回**最早到达**路线及逐段的进入、离开时间。支持在查询中
指定**按顺序停靠的站点**（每站带开始停留时间窗与停留秒数），见第 4.1 节；
也支持为**多名访客**在候选节点中选择公共汇合点，见第 4.2 节。

步道图导入时，每条路段还可携带**反向通行时间窗**数组（活动时段反向通行）：
窗内仅允许原终点走向原起点，窗外恢复原方向；方向以进入路段时刻判定，途中
跨越窗边界仍可走完全程，见第 3.1 节。

- 语言/存储：Python 3.11 + SQLite（标准库，无需外部数据库）
- Web 框架：FastAPI + Uvicorn
- 无外部网络依赖，适合园区内网 / 边缘设备离线运行

---

## 1. 快速启动

### 方式 A：Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
# 查看状态
curl http://localhost:8080/health
```

容器启动时会自动建库并导入内置示例数据（`app/sample_data.py`），随后在
`8080` 端口提供服务。数据持久化在命名卷 `park-route-data` 中。

停止：

```bash
docker compose down            # 保留数据
docker compose down -v         # 连同数据卷一起删除
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 建库 + 初始化数据（默认内置示例；--graph/--closures 可指定自定义 JSON）
python -m app.cli init-db

# 启动服务
python -m app.cli serve
# 或：uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 直接用 `uvicorn` 启动时若库为空，服务可正常运行，只是查询会返回 `503 no_data`，
> 请改用导入接口写入数据，或使用 `python -m app.cli serve` / Docker 入口（会自动种子化）。

**服务地址与端口**：默认 `http://0.0.0.0:8080`，可用 `APP_HOST` / `APP_PORT` 修改。
交互式 API 文档：`http://localhost:8080/docs`。

---

## 2. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `APP_PORT` | `8080` | HTTP 监听端口 |
| `APP_DB_PATH` | `./data/routing.db`（容器内 `/data/routing.db`） | SQLite 数据库文件路径 |
| `APP_SEED_GRAPH` | （空） | 首次初始化使用的步道图 JSON 文件路径 |
| `APP_SEED_CLOSURES` | （空） | 首次初始化使用的封闭记录 JSON 文件路径 |
| `APP_SEED_SAMPLE` | `1` | 未指定种子文件时，是否导入内置示例数据；`0`/`false` 表示空库启动 |

---

## 3. 数据模型

### 步道图

- **节点**：字符串标识，如 `"GATE"`、`"PLAZA"`。
- **路段（边）**：有方向；字段：
  - `id`：路段编号，**同一版本内唯一**，用于平局字典序比较；
  - `from` / `to`：起终点，必须引用已声明的节点；
  - `travel_seconds`：通行秒数，**正整数**；
  - `has_stairs`：是否含台阶（布尔）。

示例见 [`data/graph.example.json`](data/graph.example.json)。

#### 3.1 反向通行时间窗（`reverse_windows`，可选）

部分步道在活动时段**反向通行**。导入图时可在顶层给出
`reverse_windows` 对象（键为路段编号，值为时间窗数组）：

```json
{
  "nodes": ["LAKE", "TOWER"],
  "edges": [
    {"id": "E5", "from": "LAKE", "to": "TOWER",
     "travel_seconds": 240, "has_stairs": false}
  ],
  "reverse_windows": {
    "E5": [
      {"start": "2026-10-05T13:00:00Z", "end": "2026-10-05T16:00:00Z"}
    ]
  }
}
```

完整示例见 [`data/graph.reverse-windows.example.json`](data/graph.reverse-windows.example.json)。

语义规则（与封闭记录同样使用现有时间格式，半开区间 `[start, end)`）：

- **窗内**该路段仅允许**原终点 → 原起点**（反向进入）；**窗外**仅允许
  **原方向**（`from → to`）；未配置反向窗的路段行为完全不变；
- 方向以**进入路段的时刻**判定；进入后即使途中跨越窗边界，也按进入时
  方向走完全程（例如窗 `[13:00,14:00)`，13:50 反向进入、通行 20 分钟，
  14:10 到达原起点，仍为反向走完全程）；
- 可在节点等候方向开放：窗外想反向进入，会在原终点节点等到下一个窗的
  `start`；窗内想正向进入，会在原起点节点等到当前窗的 `end`。半开边界
  与封闭一致——恰在 `start` 可反向进入，恰在 `end` 可正向进入；
- 窗外想反向、但此后再无任何反向窗时该方向不可达，沿用现有
  `404 no_route` / `404 no_meeting_candidate` 错误语义；
- **封闭记录仍约束整个通行区间** `[enter, enter+travel_seconds)`，与
  通行方向无关；方向门等待与封闭等待可叠加（例如窗内反向进入会撞上
  封闭，则在节点等到封闭结束；若此时已落到窗外，则须再等下一个反向窗）；
- 同一路段的多个时间窗**不得重叠**（端点相接的 `[a,b)`、`[b,c)` 合法），
  每个窗须满足 `start < end`；时间格式错误、引用不存在路段等同样非法。
  **任一反向窗无效或重叠都会拒绝整次图导入并完整保留旧版本**；
- 反向窗是步道图的一部分：新图发布时随图整体替换（新图不带
  `reverse_windows` 即清空）；引用已删除路段的反向窗随新图自动清理。
- 路线路线、按序停靠、多人汇合都在单次请求固定的同一数据版本上计算，
  反向窗对每一程、每名访客同样生效。

逐段结果新增 `direction` 字段显示**实际方向**（`"forward"` 原方向 /
`"reverse"` 窗内反向）；`from_node` / `to_node` 即实际走向，反向时为
原终点 → 原起点。其余响应结构与之前一致。

### 封闭记录

- 作用于某条边，时间区间为**半开** `[start, end)`：
  - `end` 时刻该路段**立即恢复**，允许恰好在 `end` 进入；
  - `start` 时刻该路段已经封闭。
- 必须满足 `start < end`，时间使用 ISO 8601（推荐 `...Z`）；不带时区按 UTC 处理。

示例见 [`data/closures.example.json`](data/closures.example.json)。封闭记录为**整体替换**。

### 通行与等候规则

- 允许在节点等候封闭结束；
- 一条边的整个通行区间 `[enter, enter + travel_seconds)` 不得与该边任何封闭区间重叠，
  不允许“先走进路段、在路中间等”；
- 寻路时自动计算每段实际进入时刻与等候秒数。

---

## 4. HTTP API

所有请求/响应均为 JSON。

### `POST /api/route` — 查询路线

请求：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T09:55:00Z",
  "avoid_stairs": false
}
```

响应 `200`（节选）：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "query_departure_time": "2026-10-05T09:55:00Z",
  "arrival_time": "2026-10-05T10:07:00Z",
  "total_waiting_seconds": 0,
  "avoid_stairs": false,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": [
    {
      "edge_id": "E1",
      "from_node": "GATE",
      "to_node": "PLAZA",
      "direction": "forward",
      "has_stairs": false,
      "enter_time": "2026-10-05T09:55:00Z",
      "leave_time": "2026-10-05T09:58:00Z",
      "travel_seconds": 180,
      "waiting_seconds": 0
    }
  ],
  "data_version": 2
}
```

- 每段都给出**进入**（`enter_time`，已含节点等候）与**离开**（`leave_time`）时刻，
  以及 `waiting_seconds`；
- `direction` 为该段**实际通行方向**：`forward` 原方向（`from_node → to_node`），
  `reverse` 反向窗内的反向（此时 `from_node`/`to_node` 为原终点 → 原起点）；
- `data_version` 标明本次查询实际使用的数据版本。

错误：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 起终点未知、时间格式错误、停靠点参数非法等 | `400` | `invalid_payload` |
| 无可行路线（含避台阶后不可达、任一停靠站无法按时停留） | `404` | `no_route` |
| 尚未导入步道图 | `503` | `no_data` |

### 4.1 按顺序停靠（`stops`，可选）

查询可携带 `stops` 数组，访客将**按给定顺序**完成各站停靠：

```json
{
  "origin": "GATE",
  "destination": "TOWER",
  "departure_time": "2026-10-05T08:50:00Z",
  "avoid_stairs": false,
  "stops": [
    {
      "node": "LAKE",
      "earliest_start": "2026-10-05T09:00:00Z",
      "latest_start": "2026-10-05T09:10:00Z",
      "stay_seconds": 300
    }
  ]
}
```

每站字段：

| 字段 | 说明 |
| --- | --- |
| `node` | 停靠地点（必须是已知节点） |
| `earliest_start` | 最早开始停留时刻（ISO 8601 或纪元秒） |
| `latest_start` | 最晚开始停留时刻，须 `>= earliest_start`（闭区间） |
| `stay_seconds` | 停留秒数，**非负整数**（`0` 表示即停即走） |

语义规则：

- 抵达某站后可在节点**等到窗口开放**，开始停留时刻为
  `max(抵达时刻, earliest_start)`，且**不得晚于 `latest_start`**，
  否则本次查询无路线（`404 no_route`）；
- **结束停留后才能前往下一站**；途经尚未轮到的站点（包括未来各站所在
  节点）**不算完成停靠**，只当作普通节点通过；
- 起点本身就是第 1 站、或相邻两站位于同一节点时，立即（链式）完成停靠；
- 选路目标不变：**最终到达**（完成全部停靠并抵达终点）最早优先，平局按
  **全程**路段编号序列字典序选取，不逐段独立打破平局；
- 封闭区间（半开）、避台阶、单次查询固定数据版本的规则对每一程同样生效。

带停靠时响应在原有结构基础上增加 `stops` 明细与 `total_stay_seconds`
（无停靠点时不出现这两个字段，返回结构与之前完全一致）：

```json
{
  "arrival_time": "2026-10-05T09:09:00Z",
  "total_waiting_seconds": 120,
  "total_stay_seconds": 300,
  "edge_sequence": ["E1", "E3", "E5"],
  "segments": ["... 全程逐段（跨各程拼接） ..."],
  "stops": [
    {
      "node": "LAKE",
      "arrival_time": "2026-10-05T08:58:00Z",
      "start_time": "2026-10-05T09:00:00Z",
      "end_time": "2026-10-05T09:05:00Z",
      "stay_seconds": 300,
      "wait_for_window_seconds": 120
    }
  ],
  "data_version": 2
}
```

- `stops[i]` 给出第 `i+1` 站的**抵达**、**开始停留**、**结束停留**时刻
  （均附 `_epoch` 秒级副本）与等窗秒数；
- `total_waiting_seconds` 为非通行、非停留的等候总秒数（封闭等候 + 等窗）；
- `segments` 覆盖**全程**（各程拼接），`waiting_seconds` 仍为进入该路段前
  在节点的封闭等候。

错误映射：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 时间窗无效（`earliest_start > latest_start`、时间格式错误） | `400` | `invalid_payload` |
| `stay_seconds` 非非负整数（负数、小数、布尔、字符串） | `400` | `invalid_payload` |
| 停靠地点未知 | `400` | `invalid_payload` |
| 任一站无法按时停留（抵达已晚于 `latest_start`） | `404` | `no_route` |
| 某站或终点不可达 | `404` | `no_route` |

### 4.2 多人汇合点（`POST /api/meeting`）

一次请求给出**至少两名**访客（各自的起点、最早出发时刻、避台阶标记）、
**非空**候选节点列表和共同的**最晚汇合时刻**，服务在候选点中选出公共汇合点：

```json
{
  "visitors": [
    {"id": "alice", "origin": "GATE",
     "departure_time": "2026-10-05T09:55:00Z", "avoid_stairs": false},
    {"id": "bob", "origin": "PLAZA",
     "departure_time": "2026-10-05T09:50:00Z", "avoid_stairs": false}
  ],
  "candidates": ["TOWER", "LAKE"],
  "latest_meeting_time": "2026-10-05T12:00:00Z"
}
```

访客字段：

| 字段 | 说明 |
| --- | --- |
| `origin` | 起点（必须是已知节点） |
| `departure_time` | **最早出发时刻**（ISO 8601 或纪元秒）；按既有封闭、等候与最早到达规则独立通行 |
| `avoid_stairs` | 是否避台阶（布尔，默认 `false`） |
| `id` | 可选访客标识（字符串/整数），缺省按 1 起始序号；同一请求内不得重复 |

语义规则：

- 每名访客从自己的最早出发时刻**独立**通行，规则与 `POST /api/route` 完全一致
  （半开封闭区间、节点等候、避台阶、最早到达 + 全程路段编号序列字典序破平局）；
- 抵达汇合点后可在该节点等待其余访客；
- 只接受**所有人**的抵达时刻都**不晚于** `latest_meeting_time` 的候选点
  （恰好在最晚时刻抵达也可以）；
- 合格候选依次按以下标准择优：
  1. **全员实际汇合时刻**（最晚个人抵达时刻）最早；
  2. **个人抵达时刻总和**最小；
  3. **候选节点编号字典序**最小（字符串比较，如 `"N10" < "N2"`）；
- 单人路线的平局仍按其**全程路段编号序列字典序**处理，与单人路线查询语义不变；
- 整次计算在请求开始时固定**同一数据版本**，候选评估与全部访客共用该版本。

响应 `200`：

```json
{
  "meeting_node": "LAKE",
  "meeting_time": "2026-10-05T10:03:00Z",
  "meeting_time_epoch": 1791194580,
  "latest_meeting_time": "2026-10-05T12:00:00Z",
  "latest_meeting_time_epoch": 1791201600,
  "total_arrival_seconds": 3582389100,
  "visitors": [
    {
      "id": "alice",
      "origin": "GATE",
      "destination": "LAKE",
      "query_departure_time": "2026-10-05T09:55:00Z",
      "arrival_time": "2026-10-05T10:03:00Z",
      "avoid_stairs": false,
      "edge_sequence": ["E1", "E3"],
      "segments": ["... 与单人路线查询结构一致的逐段明细 ..."],
      "wait_after_arrival_seconds": 0,
      "total_waiting_seconds": 0
    },
    {
      "id": "bob",
      "origin": "PLAZA",
      "destination": "LAKE",
      "arrival_time": "2026-10-05T09:55:00Z",
      "edge_sequence": ["E3"],
      "segments": ["..."],
      "wait_after_arrival_seconds": 480,
      "total_waiting_seconds": 0
    }
  ],
  "data_version": 2
}
```

- `meeting_time` 为全员实际汇合时刻（即最晚个人抵达时刻）；
- `visitors[]` 与请求中的访客顺序一致，每人给出完整单人路线（字段结构同
  `POST /api/route`，起点即候选点时 `edge_sequence`/`segments` 为空数组）；
- `wait_after_arrival_seconds` 为该访客**到达汇合点之后**等待全员到齐的秒数
  （`meeting_time - arrival_time`，最晚抵达者为 `0`）；
- `total_arrival_seconds` 为个人抵达时刻（纪元秒）总和，即二级择优指标；
- 所有时刻均附 `_epoch` 秒级副本。

错误映射：

| 场景 | 状态码 | `error` |
| --- | --- | --- |
| 字段非法（访客少于两人、时间格式错误、`avoid_stairs` 非布尔等） | `400` | `invalid_payload` |
| 起点或候选节点未知、候选列表为空或重复、访客 `id` 重复 | `400` | `invalid_payload` |
| 没有任何候选点能让全员在最晚时刻前抵达（含某人不可达/避台阶后不可达） | `404` | `no_meeting_candidate` |
| 尚未导入步道图（空图） | `503` | `no_data` |

### `POST /admin/graph` — 原子发布新步道图

```bash
curl -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.example.json
```

校验失败（未知节点引用、非正通行秒数、编号重复，或 `reverse_windows`
时间窗无效、同路段时间窗重叠、引用不存在路段等）返回 `400`，响应体含
`"rejected": true` 与当前仍生效的 `active_version`，**旧版本完整保留**。
发布成功后旧封闭记录中引用了已删除路段的条目会自动清理。

导入带反向窗的图：

```bash
curl -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.reverse-windows.example.json
```

### `POST /admin/closures` — 原子整体替换封闭记录

```bash
curl -X POST http://localhost:8080/admin/closures \
  -H 'Content-Type: application/json' \
  -d @data/closures.example.json
```

引用不存在的路段、`start >= end` 等返回 `400` 并保留旧版本。

### 其他

- `GET /health`：健康检查与当前版本号；
- `GET /version`：当前版本及节点/路段/封闭/反向窗数量。

---

## 5. 调用示例

```bash
# 09:55 出发（E4 在 10:00–11:00 封闭），不避台阶 → 绕行平路
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T09:55:00Z","avoid_stairs":false}'

# 避台阶：只经过 has_stairs=false 的路段
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:00:00Z","avoid_stairs":true}'

# 节点等候：HILL 09:59 出发，E4 [10:00,11:00) 封 → 在 HILL 等到 11:00 进入
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"HILL","destination":"TOWER",
       "departure_time":"2026-10-05T09:59:00Z","avoid_stairs":false}'

# 按顺序停靠：08:50 出发，LAKE 站 09:00–09:10 之间开始停留、停留 300 秒
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:50:00Z",
       "stops":[{"node":"LAKE",
                 "earliest_start":"2026-10-05T09:00:00Z",
                 "latest_start":"2026-10-05T09:10:00Z",
                 "stay_seconds":300}]}'
# 结果要点：E1(08:50→08:53) → E3(08:53→08:58) 抵 LAKE；
#   等窗 120 秒，09:00 开始停留，09:05 结束；E5(09:05→09:09)；
#   arrival_time=2026-10-05T09:09:00Z，stops[0] 记录 08:58/09:00/09:05。

# 停靠窗口不可行：最晚 08:55 开始，但最早 08:58 才到 LAKE → 404 no_route
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"TOWER",
       "departure_time":"2026-10-05T08:50:00Z",
       "stops":[{"node":"LAKE",
                 "earliest_start":"2026-10-05T08:00:00Z",
                 "latest_start":"2026-10-05T08:55:00Z",
                 "stay_seconds":60}]}'

# 活动时段反向通行（先导入带 reverse_windows 的图）
curl -s -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d @data/graph.reverse-windows.example.json

# 窗内反向：E5 原方向 LAKE→TOWER，13:00–16:00 仅允许 TOWER→LAKE
#   14:00 从 TOWER 出发立即反向进入 → 14:04 到 LAKE，direction="reverse"
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TOWER","destination":"LAKE",
       "departure_time":"2026-10-05T14:00:00Z"}'

# 窗外想反向：12:00 从 TOWER 出发，在节点等到窗开始 13:00 再反向进入
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TOWER","destination":"LAKE",
       "departure_time":"2026-10-05T12:00:00Z"}'

# 窗内想正向：14:00 从 LAKE 出发须等到窗结束 16:00 才能正向进入 → 16:04 到
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"LAKE","destination":"TOWER",
       "departure_time":"2026-10-05T14:00:00Z"}'

# 窗外反向且此后无窗：17:00 TOWER→LAKE 不可达 → 404 no_route
curl -s -X POST http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"TOWER","destination":"LAKE",
       "departure_time":"2026-10-05T17:00:00Z"}'

# 反向窗重叠 → 整次导入拒绝（400，rejected=true，旧版本保留）
curl -s -X POST http://localhost:8080/admin/graph \
  -H 'Content-Type: application/json' \
  -d '{"nodes":["LAKE","TOWER"],
       "edges":[{"id":"E5","from":"LAKE","to":"TOWER",
                 "travel_seconds":240,"has_stairs":false}],
       "reverse_windows":{"E5":[
         {"start":"2026-10-05T13:00:00Z","end":"2026-10-05T15:00:00Z"},
         {"start":"2026-10-05T14:00:00Z","end":"2026-10-05T16:00:00Z"}]}}'

# 冒烟自检（需服务已启动）
python3 scripts/smoke_test.py http://localhost:8080
```

多人汇合点示例：

```bash
# alice 09:55 从 GATE、bob 09:50 从 PLAZA 出发；
# LAKE 汇合 10:03（alice 10:03 到、bob 09:55 到，bob 等 480 秒），
# TOWER 汇合 10:07 → 汇合时刻最早选 LAKE
curl -s -X POST http://localhost:8080/api/meeting \
  -H 'Content-Type: application/json' \
  -d '{"visitors":[
         {"id":"alice","origin":"GATE",
          "departure_time":"2026-10-05T09:55:00Z","avoid_stairs":false},
         {"id":"bob","origin":"PLAZA",
          "departure_time":"2026-10-05T09:50:00Z","avoid_stairs":false}],
       "candidates":["TOWER","LAKE"],
       "latest_meeting_time":"2026-10-05T12:00:00Z"}'

# 无人能在最晚时刻前抵达所有候选 → 404 no_meeting_candidate
curl -s -X POST http://localhost:8080/api/meeting \
  -H 'Content-Type: application/json' \
  -d '{"visitors":[
         {"origin":"GATE","departure_time":"2026-10-05T09:55:00Z"},
         {"origin":"PLAZA","departure_time":"2026-10-05T09:50:00Z"}],
       "candidates":["LAKE"],
       "latest_meeting_time":"2026-10-05T10:00:00Z"}'
```

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/api/meeting",
    data=json.dumps({
        "visitors": [
            {"id": "alice", "origin": "GATE",
             "departure_time": "2026-10-05T09:55:00Z", "avoid_stairs": False},
            {"id": "bob", "origin": "PLAZA",
             "departure_time": "2026-10-05T09:50:00Z", "avoid_stairs": False},
        ],
        "candidates": ["TOWER", "LAKE"],
        "latest_meeting_time": "2026-10-05T12:00:00Z",
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    plan = json.load(resp)
print(plan["meeting_node"], plan["meeting_time"])
for v in plan["visitors"]:
    print(v["id"], v["arrival_time"], v["edge_sequence"],
          "wait_after_arrival", v["wait_after_arrival_seconds"])
```

Python 示例：

```python
import json, urllib.request

req = urllib.request.Request(
    "http://localhost:8080/api/route",
    data=json.dumps({
        "origin": "GATE",
        "destination": "TOWER",
        "departure_time": "2026-10-05T09:55:00Z",
        "avoid_stairs": True,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    route = json.load(resp)
print(route["arrival_time"], route["edge_sequence"])
```

反向通行窗示例（先导入带 `reverse_windows` 的图，再在窗内反向查询；
逐段结果用 `direction` 区分实际方向）：

```python
import json, urllib.request

# 1) 导入：E5 原方向 LAKE→TOWER，13:00–16:00 仅允许 TOWER→LAKE
graph = {
    "nodes": ["LAKE", "TOWER"],
    "edges": [{"id": "E5", "from": "LAKE", "to": "TOWER",
               "travel_seconds": 240, "has_stairs": False}],
    "reverse_windows": {
        "E5": [{"start": "2026-10-05T13:00:00Z",
                "end":   "2026-10-05T16:00:00Z"}]
    },
}
pub = urllib.request.Request(
    "http://localhost:8080/admin/graph",
    data=json.dumps(graph).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(pub) as resp:
    print("published v", json.load(resp)["data_version"])

# 2) 窗内反向：TOWER 14:00 → LAKE，segments[0].direction == "reverse"
req = urllib.request.Request(
    "http://localhost:8080/api/route",
    data=json.dumps({
        "origin": "TOWER",
        "destination": "LAKE",
        "departure_time": "2026-10-05T14:00:00Z",
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    route = json.load(resp)
for seg in route["segments"]:
    print(seg["edge_id"], seg["from_node"], "->", seg["to_node"],
          seg["direction"], seg["enter_time"], "->", seg["leave_time"])
# E5 TOWER -> LAKE reverse 2026-10-05T14:00:00Z -> 2026-10-05T14:04:00Z
```

---

## 6. 选路与版本语义

- **选路目标**：① 最早到达（带停靠时为完成全部停靠并抵达终点的时刻）；
  ② 到达时刻相同时，按**全程路段编号序列字典序**选路（字符串逐元素比较，
  例如 `("E1","E3") < ("E2","E4")`），不逐段独立打破平局。
- **算法**：
  1. 时间依赖的 Dijkstra（FIFO 网络）计算最早到达时刻 `T`（带停靠时逐程
     复合：各程最早到达映射与“停留结束时刻”均为单调非降，逐程取最早即
     全局最早，同时校验各站窗口可行）；
  2. 在“最终到达不晚于 `T`”的状态空间做多标签搜索，取 `(到达时刻, 编号序列)`
     最优者。无停靠时状态为节点；带停靠时状态为 `(节点, 已完成停靠数)`，
     抵达当前轮到的站点即完成停靠（等窗 + 停留），途经未来站点不产生停靠。
     第二阶段用于处理“节点等候/停靠窗口把不同路径的到达时刻拉平”后，
     单标号算法可能选错字典序路径的问题。
- **反向通行窗**：对每个节点同时展开正向邻接（原起点）与反向邻接
  （反向窗覆盖时原终点也可达）。一条边的"最早可进入时刻（含实际方向）"
  由方向门（窗内只反向、窗外只正向）与封闭区间交替求解：方向不符就等到
  对应窗边界，撞封闭就等到封闭结束并重判方向门。该时刻关于出发时刻单调
  非降（FIFO），故上述两阶段算法无需改动；平局仍只看**全程路段编号序列**
  （不区分方向），逐段结果用 `direction` 显示实际方向。反向不可达（窗外
  反向且后续无窗）即该方向无后继标号，沿用现有不可达语义。
- **多人汇合点**（`POST /api/meeting`）：对每个候选节点，各访客复用上述
  单人 `plan` 独立求“最早到达 + 全程编号序列字典序最小”路线；丢弃任一访客
  不可达或晚于最晚汇合时刻的候选；对合格候选取
  `(最晚个人抵达, 抵达时刻总和, 候选节点编号)` 字典序最小者。每段路线的
  个人平局仍按全程路段编号序列处理，汇合点选择本身不改变单人路线语义。
- **原子发布与版本一致**：
  - 每次导入（图或封闭）在一个 SQLite 写事务中完成全部校验、复制与版本指针切换，
    失败整体回滚；
  - 每次查询在开始时读取一次 `current_version` 并固定，**整次请求只用同一版本**，
    导入进行中或刚完成都不会看到半个版本；
  - 历史版本在库中保留，可按版本号追溯。

---

## 7. 项目结构

```
app/
  config.py        # 环境变量配置
  timeutil.py      # ISO 8601 <-> 纪元秒（UTC）
  schemas.py       # 导入/查询负载校验（含停靠点 stops、多人汇合 meeting、反向窗）
  db.py            # SQLite 版本化与原子发布（含反向通行窗表）
  routing.py       # 时间依赖最短路 + 字典序选路引擎（含反向窗、按序停靠、多人汇合）
  main.py          # FastAPI 路由
  sample_data.py   # 内置示例数据（含 E5 下午活动时段反向窗）
  cli.py           # init-db / import-* / serve
data/              # 示例 JSON 与本地数据库
scripts/smoke_test.py
tests/             # 标准库 unittest 测试（142 个用例）
Dockerfile
docker-compose.yml
docker-entrypoint.sh
```

### 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖半开区间边界、节点等候、封闭穿越判定、方向、避台阶、字典序平局、
**反向通行窗（窗内/窗外方向门、进入时刻定方向与跨窗边界走完全程、
节点等窗、封闭与方向门叠加、多窗/相邻窗、反向链与混合方向、反向平局、
反向避台阶、反向窗导入校验/重叠拒绝/旧版本保留/版本快照）**、
按顺序停靠（窗口等待、顺序约束、全程字典序、起点/终点即站点、反向段衔接）、
多人汇合（候选筛选、汇合时刻/抵达总和/节点编号三级择优、到达后等待、
逐人避台阶与出发时刻、反向窗生效、负载校验）、
无路线、原子发布回滚与版本快照等场景。
