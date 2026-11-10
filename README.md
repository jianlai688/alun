# A论 · 论文导读智能体

> 先读懂论文的结构，再向你提问。

**A论（A-Lun）** 是《基于文档结构的提问智能体 DGQA 设计文档》与《DGQA 接口文档》的落地实现：上传 PDF / DOCX / Markdown 后，系统先还原题名、摘要、章节、图表与参考文献，构建结构树；再从结构节点抽取知识点与证据单元；最后按结构配额、认知层级与用户表现逐题生成、校验、提问、判分，并输出章节掌握度报告。

界面使用「纸稿 + 朱印」的阅读视觉；每道题都绑定原文页码，答完反馈会引用证据原句。

### 界面速览

| 用户登录 | 文档书架 |
|---|---|
| ![登录](docs/screenshots/00-login.png) | ![首页](docs/screenshots/01-home.png) |

| 文档详情与结构树 | 答题与提示 |
|---|---|
| ![文档](docs/screenshots/02-document.png) | ![答题](docs/screenshots/03-session.png) |

| 学习报告 | 管理控制台 |
|---|---|
| ![报告](docs/screenshots/04-report.png) | ![管理台](docs/screenshots/05-admin-overview.png) |

| 实时监控 | 导读模型设置 |
|---|---|
| ![监控](docs/screenshots/06-admin-monitor.png) | ![设置](docs/screenshots/07-admin-settings.png) |

| A论 AI 问答助手 | Token 用量 |
|---|---|
| ![助手](docs/screenshots/09-assistant.png) | ![用量](docs/screenshots/08-admin-usage.png) |

| 论文管理 | 质量验收 |
|---|---|
| ![论文管理](docs/screenshots/10-admin-documents.png) | ![质量验收](docs/screenshots/11-quality.png) |

| 教学闭环（教师） | 阅读器增强 |
|---|---|
| ![教学闭环](docs/screenshots/12-teacher.png) | ![阅读器](docs/screenshots/15-reader-notes.png) |

| 平台化（PostgreSQL 后端） |
|---|
| ![平台化](docs/screenshots/19-platform-admin.png) |

---

## 1. 已实现能力

| 模块 | 实现 |
|---|---|
| 文档接入 | PDF / DOCX / Markdown / PNG / JPG / WEBP 上传、**网页链接抓取导入**、SHA-256 去重、异步解析任务、SSE 进度流 |
| 结构解析 | 双栏阅读顺序、页眉页脚剔除、标题编号 + 字号字重识别、摘要/关键词/参考文献/作者简介、图表题注、页码范围、**表格单元格 Markdown 抽取与公式文本节点** |
| 图片识别 | 低文本 PDF 与图片型文档自动调用多模态 OCR 模型，重建文本行后继续做结构树、知识点与证据抽取 |
| 结构树 | 邻接表存储、materialized path、层级/顺序/页码/置信度，D6 查询与 D7 人工校正；**可视化拖拽调整层级、重命名、合并同级节点、删除** |
| 质量门 | 文本覆盖、乱码率、标题置信度、结构完整度、图表关联，输出 pass / review 与问题清单 |
| 质量与验收 | 黄金集 v1（8 篇 / 169 个标题）、标题 P/R/F1、证据支持率、节点可追溯率、判分一致率、时延分位、缺陷台账与一键回归；管理端「质量验收」看板 |
| 教学闭环 | 追问持久化、固定题卷布置/作答/提交、教师复盘评语、学生报告回显；阅读器页码同步与页级笔记；结构版本快照/回滚 |
| 平台化 | SQLite/PG 双后端、pgvector 语义检索、Redis 限流/缓存、MinIO 对象存储、Redis Streams 队列 worker、Prometheus 指标/JSON 日志/健康检查、安全基线 |
| 图表数据 | 图片表格 OCR→Markdown 表格节点；柱状图/折线图/饼图/散点图视觉模型提取为 chart 节点 + series/values JSON |
| 划词解读 | 阅读器选中原文后一键 AI 解释，可开启「自动解读」；PDF 可切换「划词模式」，原文页图片 + 透明文本层保持版式一致 |
| 知识建模 | 语义分块、六类知识点（概念/方法/数据/结论/观点/局限）、证据单元与页码偏移、重要度与难度 |
| 提问规划 | 导读/测评/答辩三种模式、结构配额、Bloom 层级、题型矩阵、加权 softmax 选题、同节最多 2 题；用户可勾选选择题/填空题/判断题/问答题 |
| 逐题生成 | 模型按「证据 → 题干 → 选项 → 答案 → 提示」生成；V1—V8 校验；失败重试；规则模板兜底 |
| 追问 | 答完一题后可发起澄清型、证据型、迁移型追问，模型基于原题与证据生成追问并给出 0—100 追问评分 |
| 题库与题卷 | 题目入库、预览、编辑、难度调整、复核/下架；按文档和轻松/适中/挑战/混合难度生成多套题卷 |
| 会话 | S1—S11：创建计划、取下一题、作答、三级提示、跳过、提前结束、轮次回放、报告 |
| 答题复盘 | 上一题导航、查看解析：标准答案、评分要点、原文证据、提示与得分复盘 |
| 判分 | 客观题规则判分（多选漏选部分分、错选不得分）；主观题按「要点覆盖/准确性/证据引用/表达结构」四维评分 |
| 自适应 | Elo 能力估计（0—1000，初始 500），分章节能力向量，答对提高、答错降低 |
| 报告 | 总分、章节掌握度、Bloom 分布、错题与原文页码、三条具体建议、Markdown 导出 |
| 账号与登录 | 用户注册/登录、管理员登录、PBKDF2 密码哈希、JWT 角色与租户隔离、账号禁用 |
| 管理后台 | 用户使用情况、回答准确率、模型幻觉率（证据校验失败率）、实时行为流/在线用户、导读模型设置、token 与积分用量 |
| 论文管理 | 管理端可查看所有用户论文、检索标题/文件名、删除论文并清理原始文件；删除后从用户文档库和会话列表移除 |
| AI 问答助手 | 用户端与管理端共用：全文 RAG、会话/平台数据、图片理解、OpenAlex/arXiv/PubMed 权威资料检索、原文页码与来源标注；**支持 LaTeX / MathJax 数学公式渲染** |
| 原文阅读器 | 文档页「原文」进入网页内阅读：PDF/图片/文本原样展示，右侧固定 AI 解读栏独立占位，不覆盖原文；支持总结全文、解释方法、查找原文证据 |
| 前端 | 登录页、文档书架、结构树、模式/题量配置、答题卡、提示、反馈、报告全流程；独立 `/admin` 管理控制台 |

接口实现覆盖接口文档第 4 章的 33 个端点（A1、D1—D10、T1—T3、S1—S11、Q1—Q4、R1—R2、U1、H1），并新增认证、管理端与 AI 助手 API。原始契约见 `docs/dgqa-openapi.json`，当前运行时完整 OpenAPI（93 条路径）见 `docs/alun-openapi.json`。

---

## 2. 快速启动

### 2.1 安装依赖

```powershell
cd D:\AI论文\alun
python -m pip install -r requirements.txt
```

要求 Python 3.11+（推荐 3.12）。

### 2.2 启动

```powershell
.\run.cmd
```

或：

```powershell
python -m uvicorn alun.app.main:app --host 127.0.0.1 --port 8010
```

打开 <http://127.0.0.1:8010>，使用首次启动自动创建的账号登录：

| 账号 | 默认密码 | 角色 | 入口 |
|---|---|---|---|
| `demo` | `demo123` | 普通用户（学生） | `/` |
| `teacher` | `teacher123` | 教师 | `/teacher` |
| `admin` | `admin123` | 管理员 | `/admin` |

首次启动会自动创建 `data/alun.db`、`data/uploads/` 以及上述三个账号。生产环境请通过环境变量修改默认密码，并设置独立的 `ALUN_AUTH_SECRET`。

用户可在登录页注册新账号：用户名 3—32 位，密码至少 8 位且包含字母和数字；邮箱、手机号选填但会做唯一性校验。注册成功后自动登录。

### 2.3 管理后台

访问 <http://127.0.0.1:8010/admin>，使用管理员账号登录，可查看：

- **总览**：注册用户、今日活跃、文档/会话/作答、回答准确率、模型幻觉率、token 总量、近 14 天趋势。
- **论文**：所有用户论文列表、标题/文件名检索、解析质量与结构统计，可删除论文并清理原文件。
- **用户**：每个用户的文档数、会话数、作答数、准确率、token 用量、最近活跃时间，可禁用/启用账号。
- **实时监控**：最近 5 分钟在线用户、登录/上传/出题/作答/提示/报告等行为事件流，每 2 秒自动刷新。
- **模型设置**：启用/关闭模型、主模型与备用模型、temperature、max tokens、导读默认题量与提示级数、系统提示词；图片识别（OCR）模型、DPI、最多识别页数与触发阈值；修改后立即生效。
- **Token 用量**：按日、调用类型、实际模型统计请求数、prompt/completion token 与积分。

### 2.4 A论 AI 问答助手

用户端和管理端右下角均有「A论 AI」悬浮入口：

- **用户端**：自动带入当前论文的摘要、章节结构、知识点和原文片段；可问全文概括、方法、实验数据、概念解释，也可上传截图/图片。回答会标注原文页码，外部资料标注 `[S1]` 等来源。
- **管理端**：自动带入后台真实统计（用户、准确率、幻觉率、token、模型设置与最近事件），适合问“今天使用情况”“哪些用户需要关注”“模型幻觉率怎么理解”。
- **权威检索**：使用 OpenAlex、arXiv、PubMed 等公开学术接口；检索失败会明确说明，不把外部资料伪装成论文原文。
- **图片**：支持 PNG / JPEG / WEBP / GIF，最多 3 张、单张 8 MB；模型先读图中文字/表格，再结合文档或平台数据回答。
- **无依据不编造**：找不到原文或权威依据时直接说明，不生成假页码、假链接。

### 2.5 接入模型（可选但推荐）

A论默认读取本机 WorkBuddy2API 网关（OpenAI 兼容，`http://127.0.0.1:7863/v1`）。若未启动网关或没有配置密钥，系统自动降级为「规则模板出题 + 规则判分」，仍然可以完整走通流程，但题目表达和主观题反馈会明显弱于模型模式。

也可以显式配置任意 OpenAI 兼容服务：

```powershell
$env:ALUN_LLM_BASE_URL = "https://api.example.com/v1"
$env:ALUN_LLM_API_KEY  = "sk-..."
$env:ALUN_LLM_MODEL    = "your-model"
.\run.cmd
```

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ALUN_LLM_BASE_URL` | `http://127.0.0.1:7863/v1` | OpenAI 兼容 Base URL |
| `ALUN_LLM_API_KEY` | 从 `~/.dsh/wb2api.json` 读取 | 网关密钥 |
| `ALUN_LLM_MODEL` | `cn:deepseek-v4-flash` | 生成/判分模型 |
| `ALUN_LLM_FALLBACK_MODEL` | `cn:glm-5.3-flash` | 主模型失败时的备用模型 |
| `ALUN_ENABLE_LLM` | `1` | 设为 `0` 强制规则模式 |
| `ALUN_LLM_TIMEOUT` | `90` | 单次模型调用超时（秒） |
| `ALUN_AUTH_MODE` | `strict` | `strict` 强制 Bearer JWT；`demo` 为本地免登录调试 |
| `ALUN_AUTH_SECRET` | 本地开发密钥 | JWT HS256 签名密钥，生产必须修改 |
| `ALUN_ADMIN_USERNAME` | `admin` | 首次启动创建的管理员账号 |
| `ALUN_ADMIN_PASSWORD` | `admin123` | 管理员初始密码，生产必须修改 |
| `ALUN_DEMO_USERNAME` | `demo` | 首次启动创建的演示账号 |
| `ALUN_DEMO_PASSWORD` | `demo123` | 演示账号初始密码 |
| `ALUN_TEACHER_USERNAME` | `teacher` | 首次启动创建的教师账号 |
| `ALUN_TEACHER_PASSWORD` | `teacher123` | 教师初始密码，生产必须修改 |
| `ALUN_DATA_DIR` | `./data` | 数据库、上传文件、报告目录 |
| `ALUN_MAX_UPLOAD_MB` | `100` | 单文件大小上限 |

### 2.6 质量验收（V1.1）

管理端新增「质量验收」页与 CLI 回归：

```powershell
# 全量评测（结构 + 证据 + 判分 + 时延），生成 Markdown 报告
python -m alun.quality.cli run --suite all --report

# 只跑结构
python -m alun.quality.cli run --suite parse

# 黄金集清单/校验
python -m alun.quality.cli golden

# 并发压测（服务需先启动）
python -m alun.scripts.quality_stress --mode parse --concurrency 10
```

黄金集位于 `alun/quality/goldens/golden-v1/`，标注规范见 `docs/质量/01-黄金集与标注手册.md`，验收报告见 `docs/质量/02-V1.1-质量验收报告.md`。指标口径：标题 F1 ≥0.95、证据支持率 100%、节点可追溯率 100%、可答性 ≥4.3、主观题一致率 ≥85%、30 页解析 P95 ≤90 秒。

### 2.7 教学闭环（V1.2）

教师登录 `/teacher` 后，在「教学闭环」页可以：

1. 从论文的 active 题目生成题卷，或直接选择题库中已有题卷；
2. 选择学生、截止时间和布置说明，生成作业；
3. 学生登录 `/`，在「老师布置的题卷」中开始作答，系统按固定题序出题，完成后自动提交；
4. 教师查看逐题作答、报告摘要、章节掌握度与追问记录，填写评语完成复盘；
5. 学生在学习报告末尾看到教师复盘评语。

相关能力：

- **追问持久化**：`follow_up` 表 + `/sessions/{id}/follow-ups`，刷新页面/报告后仍保留；
- **阅读器增强**：`/documents/{id}/reading-state` 与 `/documents/{id}/notes`，支持页码恢复与页级笔记；
- **结构版本**：结构编辑、重新解析、回滚前自动快照；`/documents/{id}/versions` 查看/回滚；
- **教师账号**：`teacher / teacher123`（可用 `ALUN_TEACHER_USERNAME/PASSWORD` 覆盖）。

验收记录见 `docs/教学闭环/01-V1.2-教学闭环验收.md`。

### 2.8 平台化（V2.0）

平台模式通过环境变量开启，默认单机仍使用 SQLite + 本地文件 + 线程：

```powershell
# 基础设施（PostgreSQL 18 + pgvector / Redis / MinIO）
docker compose -f alun/platform/docker-compose.yml up -d postgres redis minio

# 平台模式启动
$env:ALUN_DB_BACKEND="postgres"
$env:ALUN_DATABASE_URL="postgresql://alun:alun_dev_password@127.0.0.1:55432/alun"
$env:ALUN_REDIS_URL="redis://:123456@127.0.0.1:6379/1"
$env:ALUN_QUEUE_BACKEND="redis"
$env:ALUN_S3_ENDPOINT="http://127.0.0.1:9000"
$env:ALUN_S3_ACCESS_KEY="alun"
$env:ALUN_S3_SECRET_KEY="alun_dev_password"
$env:ALUN_AUTH_SECRET="replace-with-a-random-secret"
python -m uvicorn alun.app.main:app --host 127.0.0.1 --port 8011
python -m alun.platform.worker worker-1
```

- 迁移：`python -m alun.platform.migrate_sqlite --target ...`
- 迁移台账/回滚：`python -m alun.platform.cli status|migrate|rollback`
- 指标：`/metrics`；就绪：`/api/v1/ready`；结构化日志：`ALUN_LOG_JSON=1`
- 语义检索：`POST /api/v1/documents/{id}/embeddings`、`GET /api/v1/search/semantic`
- 平台验收：`docs/平台化/01-V2.0-平台化验收.md`

### 2.9 图表/表格解析

图片型表格（截图、扫描页、PDF 中的表格图片）会被 OCR 转成 Markdown 表格；没有「表 x」题注的表格会自动补建 `table` 结构节点。柱状图/折线图/饼图/散点图由视觉模型提取标题、坐标轴、图例与 series/values，生成 `chart` 节点和 `chart-data` JSON；可在管理端「模型设置」配置 `chart.model` 与页数上限。已入库文档点击「重新解析」即可生效。详见 `docs/解析增强/01-图表表格解析.md`。

---

## 3. 使用流程

1. 打开首页，使用 `demo / demo123` 登录（也可在登录页注册普通账号）。
2. 首页上传论文（或拖入）。解析任务在后台执行，首页/文档页显示阶段与进度。
3. 进入文档页，查看结构树、摘要、解析质量与知识点。
4. 在右侧选择「导读 / 测评 / 答辩」、题量与难度，点击「让 A论 开始提问」。题量选择是上限；实际题量受「同一小节最多 2 题」和可用知识点数量约束，页面会显示当前文档可出的最大题量并提示调整。
5. 逐题作答；可以请求三级提示（分别扣 10% / 20% / 40%）或跳过。
6. 提交后查看得分、四维评分、原文证据与页码；点「查看解析」查看标准答案、评分要点和完整解析，点「上一题」回看已答题目。
7. 全部完成或提前结束后生成学习报告；支持导出 Markdown。

### 演示数据

工作目录 `D:\AI论文` 下的 PDF（当前包含 6 份 DGQA 样例）可直接通过页面或接口上传。也可以在服务启动后运行：

```powershell
python alun/scripts/seed_samples.py
```

接口示例：

```powershell
curl.exe -X POST "http://127.0.0.1:8010/api/v1/documents" `
  -F "file=@D:\AI论文\基于强化微调的低资源电力文档关键信息提取研究与应用.pdf" `
  -F "doc_type=auto"
```

---

## 4. 目录结构

```
alun/
├─ app/
│  ├─ main.py          # FastAPI 路由：33 个 DGQA 接口 + 认证/管理端
│  ├─ config.py        # 环境变量、模型网关配置
│  ├─ db.py            # SQLite 建表与数据访问
│  ├─ parser.py        # PDF/DOCX/MD 解析、阅读顺序、结构树、表格/公式、质量门
│  ├─ url_import.py    # 网页链接抓取与正文 Markdown 提取
│  ├─ knowledge.py     # 语义分块、知识点、证据单元
│  ├─ planner.py       # 结构配额与加权采样
│  ├─ generator.py     # 题目生成、V1—V8 校验、模板兜底
│  ├─ judge.py         # 客观/主观判分、Elo 与章节能力
│  ├─ sessions.py      # 会话编排 S1—S11
│  ├─ reports.py       # 报告聚合
│  ├─ pipeline.py      # 上传、去重、异步解析任务
│  ├─ users.py         # 用户、角色、密码哈希与初始化账号
│  ├─ settings.py      # 运行时模型/导读设置
│  ├─ audit.py         # 活动事件与 AI 输出审计
│  ├─ assistant.py     # AI 问答助手：文档 RAG、平台数据、权威检索
│  ├─ admin.py         # 管理端 API
│  ├─ quality_api.py   # 质量验收看板 API
│  └─ security.py      # JWT（HS256）、密码哈希与身份解析
├─ quality/            # 黄金集、评测指标、运行器、报告与 CLI
│  ├─ goldens/golden-v1/
│  ├─ metrics.py
│  ├─ runner.py
│  └─ cli.py
├─ platform/           # V2.0 PG/pgvector、Redis、S3、队列、迁移与可观测性
│  ├─ migrations.py
│  ├─ storage.py
│  ├─ queue.py
│  ├─ worker.py
│  └─ docker-compose.yml
├─ web/                # 无构建依赖的前端（用户端 + /admin 管理端 + /teacher 教师端）
├─ data/               # SQLite、上传原文、导出报告（运行时生成）
├─ tests/              # 冒烟测试
├─ run.cmd / run.ps1
└─ requirements.txt
```

---

## 5. 数据与接口说明

### 5.1 表结构

`document`、`node`、`chunk`、`knowledge_point`、`evidence`、`question`、`question_set`、`session`、`turn`、`report`、`task`、`usage_log`、`idempotency`，以及新增的 `app_user`、`app_setting`、`activity_event`、`ai_audit`；V1.1 质量验收新增 `quality_run`、`quality_case`、`quality_review`、`quality_defect`；V1.2 教学闭环新增 `follow_up`、`paper_assignment`、`reader_state`、`reader_note`、`document_version`，并给 `session` 增加 `assignment_id`。 V2.0 平台化新增 `platform_schema`、`document_embedding`（pgvector）、`platform_job`。

### 5.2 关键接口

| 编号 | 方法与路径 | 说明 |
|---|---|---|
| D1 | `POST /api/v1/documents` | 上传，202 返回 document_id / task_id |
| D1-URL | `POST /api/v1/documents/from-url` | 抓取公开网页/PDF 链接并导入解析 |
| D3 | `GET /api/v1/documents/{id}` | 文档详情、stats、quality |
| D6 | `GET /api/v1/documents/{id}/outline` | 结构树（depth / include_text） |
| D8 | `GET /api/v1/documents/{id}/quality` | 解析质量报告 |
| D9/D10 | `GET .../knowledge-points`、`.../evidence` | 知识点与证据 |
| T1/T2 | `GET /api/v1/tasks/{id}`、`/events` | 任务轮询 / SSE |
| S1 | `POST /api/v1/sessions` | 创建会话并生成计划 |
| S4 | `POST /api/v1/sessions/{id}/next-question` | 取下一题（不含答案/证据） |
| S5 | `POST /api/v1/sessions/{id}/answers` | 判分、反馈、下一步动作 |
| S6/S7 | `.../hint`、`.../skip` | 三级提示 / 跳过 |
| 追问 | `POST .../follow-up`、`.../follow-up/answer` | 澄清/证据/迁移追问与追问评分 |
| 复盘 | `GET /api/v1/sessions/{id}/turns/{seq_no}` | 上一题、标准答案、评分要点与原文证据 |
| S9 | `GET /api/v1/sessions/{id}/report` | 学习报告 |
| Q1—Q4 | `/api/v1/questions...` | 题库查询、编辑、人工复核 |
| 题卷 | `GET/POST /api/v1/question-sets` | 多难度题卷生成与列表 |
| 教学 | `GET/POST /api/v1/assignments`、`GET /assignments/{id}` | 教师布置题卷、作业列表与作答详情 |
| 教学 | `POST /api/v1/assignments/{id}/start`、`/submit`、`/review` | 学生开始/提交、教师复盘 |
| 教学 | `GET /api/v1/teaching/students`、`/documents`、`GET/POST /teaching/papers` | 教师选学生/论文、生成题卷 |
| 阅读器 | `GET/PUT /api/v1/documents/{id}/reading-state` | 阅读页码与缩放状态 |
| 阅读器 | `GET/POST/PATCH/DELETE /api/v1/documents/{id}/notes` | 页级笔记 |
| 结构版本 | `GET /api/v1/documents/{id}/versions`、`POST .../versions/{no}/restore` | 版本列表与回滚 |
| 追问 | `GET /api/v1/sessions/{id}/follow-ups` | 持久化追问记录 |
| R1/R2 | `/api/v1/reports/{id}`、`/export` | 报告与 Markdown 导出 |
| U1/H1 | `/api/v1/usage`、`/health` | 用量与健康检查 |
| 认证 | `POST /api/v1/auth/login`、`/register`、`GET /auth/me` | 用户名密码登录、注册、当前用户 |
| 管理 | `GET /api/v1/admin/overview`、`/users`、`/events`、`/online` | 总览、用户使用情况、实时事件与在线用户 |
| 管理 | `GET /api/v1/admin/documents`、`DELETE /api/v1/admin/documents/{id}` | 论文列表与删除（可清理原文件） |
| 管理 | `GET/PUT /api/v1/admin/settings`、`POST /api/v1/admin/model/test` | 导读模型与策略设置 |
| 管理 | `GET /api/v1/admin/usage`、`/model-logs` | token 用量与模型输出审计 |
| 质量 | `GET /api/v1/admin/quality/overview`、`/runs`、`/runs/{id}`、`/runs/{id}/report` | 质量看板、运行历史与报告下载 |
| 质量 | `POST /api/v1/admin/quality/runs`、`GET /golden`、`GET/PATCH /defects` | 触发回归、黄金集清单、缺陷台账 |
| 质量 | `GET/POST /api/v1/admin/quality/reviews` | 题目可答性与主观题参考分录入 |
| 助手 | `POST /api/v1/assistant/chat` | 文本/图片问答，文档 RAG + 会话/平台数据 + 权威检索 |
| 平台 | `GET /api/v1/health`、`/api/v1/ready`、`/metrics` | 依赖健康、就绪检查与 Prometheus 指标 |
| 平台 | `GET /api/v1/admin/platform/status` | 管理员查看迁移、队列、向量索引与依赖状态 |
| 向量 | `POST /api/v1/documents/{id}/embeddings`、`GET /api/v1/search/semantic` | pgvector 索引与语义检索 |
| 图表 | `GET /api/v1/documents/{id}/charts`、`POST /api/v1/documents/{id}/charts/extract` | 查询/重解析图表数据 |

`strict` 认证模式（默认）下，先调用 `POST /api/v1/auth/login` 获取 JWT，后续请求携带 `Authorization: Bearer <token>` 与 `X-Tenant-Id`；管理端 API 还要求 `role=admin`；教师端 API 允许 `role=admin` 或 `role=teacher`。

---

## 6. 设计取舍

- **SQLite 而非 PostgreSQL/pgvector**：首期单机演示优先，部署零依赖；表结构保留了向量/索引扩展位。生产化时按设计文档 7.3 替换为 PostgreSQL 16 + pgvector，接口层不变。
- **规则为主、模型增强**：结构识别与质量门用可解释规则，保证离线可用；题目生成、主观判分走模型。模型不可用时自动降级为模板题与规则判分，不阻断会话。
- **逐题生成**：不在解析阶段批量出题，降低无效 token 消耗，并让题目贴合当前能力估计。
- **证据强约束**：生成后逐字校验引文是否在节点原文中；不通过则重试一次，再失败降级为模板题。
- **前端零构建**：单页 HTML/CSS/JS，避免 Node 构建链，方便私有化部署和二次开发。

---

## 7. 已知边界

- OCR 依赖多模态模型：扫描件/图片型 PDF 会渲染页面并调用图片识别模型；网关不可用或模型不支持图片时保留文本层并进入 `needs_review`，不会阻断上传。
- 质量门中的「乱码率」只统计替换字符、控制字符与不可显示方块；PDF 公式字体常用的私有区符号（如 `\uf03d`）单独记为 `formula_glyph_rate`，属于公式字形而不是 OCR 失败，不会误报「建议 OCR」。
- 复杂合并表格、公式推导题仍不做强制语义结构化：表格单元格以 Markdown 文本进入结构节点，公式以公式文本节点记录；可支持定位与含义题，复杂公式推导仍建议人工复核。
- 自动追问已实现澄清/证据/迁移三种类型，追问评分独立展示、不计入主会话总分；追问题目/回答/评分已持久化到 `follow_up`，刷新后可恢复，报告展示追问数与均分。
- 逐题生成受模型网关时延影响：V1.1 实测 P95 24.85 秒，未达到较基线下降 50% 的目标；V1.2 计划通过题目预生成队列、首题预取、流式返回与模型路由继续优化。
- 解析后重新解析会重建 `node_id`；系统会按章节标题/知识点名称自动修复未完成会话的 plan，并按证据引文反查题库节点：能逐字定位的自动重挂，定位不到的转 `pending_review`，不再静默保留失效引用。
- 管理台「模型幻觉率」定义为：模型最终生成的题目中，未通过证据/结构校验的比例；模板兜底不计入分母，首轮失败但重试成功也不计为幻觉。
- 题卷当前按 active 题目自动抽取，尚未提供逐题手工挑题/排序编辑器；作业按单个学生布置，尚未实现班级/分组批量布置。
- PDF 页码定位依赖浏览器内置 PDF 查看器 `#page=` 参数；文字型文档按结构节点页码滚动。
- 当前账号体系面向单机/小团队：账号密码保存在本地 SQLite，JWT 为 HS256；生产环境应改为 HTTPS、外部身份源（OIDC/LDAP）与密钥管理服务。

---

## 8. 验证

```powershell
# 健康检查
curl.exe http://127.0.0.1:8010/api/v1/health

# 冒烟测试（不依赖外网，使用临时 Markdown 文档；在 D:\AI论文 下执行）
python -m unittest discover -s alun/tests -t . -v

# 质量验收回归（黄金集 + 指标 + Markdown 报告）
python -m alun.quality.cli run --suite all --report

# 教学闭环演示（教师布置 → 学生作答 → 教师复盘）
python -m alun.scripts.teaching_demo --doc-id doc_37b9cbf889

# 平台化迁移/回滚（需 PostgreSQL）
python -m alun.platform.cli status
python -m alun.platform.cli migrate
python -m alun.platform.cli rollback --target 0

# 10 并发压测（平台服务）
python -m alun.scripts.quality_stress --mode parse --concurrency 10 --base-url http://127.0.0.1:8011
```

如果使用本机 WorkBuddy2API 网关，`/api/v1/health` 的 `model_gateway` 会显示 `ok`；页面右上角显示「导读模型已就绪」。

---

## 9. 参考

- 《基于文档结构的提问智能体 DGQA 设计文档》V1.0，DGQA-DESIGN-001
- 《DGQA 接口文档》V1.0，DGQA-API-001
- GB/T 8567—2006《计算机软件文档编制规范》
- ISO/IEC 25010:2011 质量模型
