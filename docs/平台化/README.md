# A论 V2.0 平台化材料

| 文档/证据 | 内容 |
|---|---|
| [01-V2.0-平台化验收](01-V2.0-平台化验收.md) | PG/pgvector、Redis、MinIO、队列、可观测性、安全与 10 并发/回滚验收 |
| [02-上传去重修复](02-上传去重修复.md) | 删除后重传被跨用户同哈希文档拦截的修复 |
| [03-解析依赖缺失修复](03-解析依赖缺失修复.md) | `No module named 'httpx'` 导致 DOCX 解析失败的诊断与修复 |
| `../../data/platform/migration-report.json` | SQLite → PostgreSQL 逐表迁移报告 |
| `../../data/platform/migration-rollback-report.json` | 平台 schema 迁移回滚演练 |
| `../../data/platform/rollback-app-report.json` | 应用回退 SQLite 演练 |
| `../../data/quality/stress-20261106-124339.md` | 平台模式 10 并发解析压测报告 |
| `../screenshots/19-platform-admin.png` | 平台模式（PostgreSQL 后端）管理端实跑截图 |

运行入口：

- Compose：`docker compose -f alun/platform/docker-compose.yml up -d postgres redis minio`
- 平台服务：见验收报告第 10 节
- 平台 CLI：`python -m alun.platform.cli status`
- 队列 worker：`python -m alun.platform.worker worker-1`
