# 风机告警静默间隔试算系统

调整风机告警的静默间隔前，先用**一周的告警和检修工单试算**。本服务用
**FastAPI + SQLite** 实现，提供 REST 接口与单页操作界面，支持 `docker compose` 一键启动。

## 业务规则

### 1. 评分切片登记（含告警明细，版本化不可变）

- 同一（风机，切片名）版本号严格递增，不传版本号自动 +1。
- 登记时**可附触发告警的 UTC 整秒列表**（Unix epoch 秒），随该版一并保存；
  入库即按整秒去重、升序存储——**同秒告警合为一次**。
- 两种"无列表"语义严格区分：
  - **省略列表**（请求中不带 `alarm_timestamps` 字段）= **缺明细**；
  - **空列表 `[]`** = **确无告警**。

### 2. 静默间隔核算

在核算页选择**同一风机的切片版本**，填写：

- 起始 UTC 日期（回放从该日 **UTC 零点**开始，**满七日即止**，窗口 `[t0, t0+604800)`）；
- 通知上限（**1～100 条**）；
- 本周检修工单号及各自的**预警起止整秒**（闭区间，须落在七日窗口内）。

回放与计时规则：

- **起初无静默**：窗口内第一条告警一定发出通知；
- 只有回放中**真正发出的通知**才开始计时，被静默压制的告警不重置计时器；
- 距上一条通知时刻达到静默间隔 `s` 才允许再次通知；
- 在 **0～21600 秒的整数间隔**中求**最短可行间隔**，要求：
  1. 通知总数 ≤ 通知上限；
  2. 每张工单预警**闭区间内至少有一条通知**。

页面当次列出：最短间隔、全部通知时刻、每张工单区间内的**首次通知时刻**；
无解时明确提示原因（上限不可达 / 工单无法覆盖）。

### 3. 缺明细保护与重启可读旧版

- 对缺明细的切片版本发起核算：**列出缺项并停止核算**（HTTP 422，不留核算记录）。
- 告警明细与核算结论均落盘，**重启后选择旧版切片仍读取该版保存的明细**，
  历史核算记录可随时复看。

### 4. 大数据量与算法约束

- 回放须处理 **20 万条原始告警、500 张工单**。
- 每次试算只从库中**读取一次**窗口内告警序列；间隔寻优利用单调性做**二分查找**
  （约 15 次探活，每次 O(m) 线性回放），**绝不逐个枚举 0..21600 间隔重扫明细**。
- 单调性依据：间隔越大，贪心通知序列是小间隔序列的子序列——通知总数单调不增、
  工单覆盖性质只能由满足变不满足，故可行间隔构成前缀，二分成立。

## 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/slices` | 登记评分切片版本（可附 UTC 整秒告警列表，省略=缺明细，`[]`=确无告警） |
| GET | `/api/slices` | 切片版本列表（不含明细大字段） |
| GET | `/api/slices/{id}?include_alarms=true` | 查看某版保存的告警整秒明细 |
| POST | `/api/trials` | 七日回放 + 最短静默间隔试算，结论落库 |
| GET | `/api/trials` | 历史核算记录列表 |
| GET | `/api/trials/{id}` | 查看某次核算的间隔、通知时刻与工单首次通知 |
| GET | `/` | 单页操作界面；`/docs` 为 OpenAPI 文档；`/healthz` 健康检查 |

### 请求示例

```bash
# 登记带明细的切片（省略 alarm_timestamps 即表示缺明细）
curl -X POST localhost:8000/api/slices -H 'Content-Type: application/json' -d '{
  "turbine_id": "WT-01",
  "slice_name": "weekly_score",
  "alarm_timestamps": [1794182400, 1794182460, 1794182460, 1794186000]
}'

# 试算：起始日 + 上限 + 工单预警闭区间
curl -X POST localhost:8000/api/trials -H 'Content-Type: application/json' -d '{
  "slice_id": 1,
  "start_date": "2026-11-09",
  "notify_cap": 20,
  "work_orders": [
    {"order_no": "WO-1001", "start_ts": 1794182400, "end_ts": 1794268800}
  ]
}'
```

## 启动方式

### docker compose

```bash
docker compose up --build
# 访问 http://localhost:8000 ，SQLite 持久化在命名卷 alarm-data (/data/alarm.db)
```

### 本地运行

```bash
pip install -r requirements.txt
export APP_DB_PATH=/data/alarm.db   # 可选，默认 /data/alarm.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试与冒烟

```bash
pip install pytest httpx
python -m pytest tests/ -q          # 16 个 HTTP 链路用例（含 20 万告警性能用例）
python scripts/smoke.py             # 对运行中的服务做端到端剧情验证
```

测试覆盖：版本递增冲突、省略/空列表两种明细语义、入库同秒去重、
起初无静默与"仅通知启动计时"、七日窗口裁剪、闭区间端点覆盖、
二分最短间隔及其最短性、两类无解提示、缺明细 422 并列缺项、
重启后旧版明细与核算结论复算、500 工单覆盖、20 万告警下探活次数与扫描量约束。

## 目录结构

```
app/
  engine.py       # 纯函数回放引擎：贪心回放 + cap 提前退出 + 二分寻优
  database.py     # SQLite 连接与建表（切片版本 / 告警明细 / 核算记录）
  schemas.py      # Pydantic 入参（含 1~100 上限、UTC 整秒校验）
  services.py     # 切片登记、窗口取数、工单校验、试算落库与历史复算
  errors.py       # 统一业务错误（含 422 缺明细）
  main.py         # FastAPI 路由
  static/index.html  # 单页操作界面（登记 / 核算 / 历史三页签）
tests/               # pytest 全链路用例
scripts/smoke.py     # 端到端冒烟脚本（仅标准库）
Dockerfile / docker-compose.yml / requirements.txt
```
