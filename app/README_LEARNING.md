# A论后端代码学习注释说明

`alun/app` 下的 29 个 Python 文件已经全部加入中文学习注释，适合按模块阅读。

- 原始代码备份：`alun/app_original`（未注释，可回退/对照阅读）
- 注释副本：`alun/app_annotated`（与当前 `alun/app` 注释版一致）
- 总规模：原始 9,035 行 → 注释版 20,976 行
- 逐文件行数：`alun/app_annotated/manifest.json`
- 校验：29 个文件 `py_compile` 全部通过；剔除新增 docstring 后 AST 与备份完全一致；25 项测试通过。

## 建议阅读顺序

1. `config.py`：配置、路径、数据库/Redis/S3/模型网关；
2. `main.py`：FastAPI 装配、中间件、认证、路由；
3. `db.py`：SQLite/PostgreSQL 双后端；
4. `parser.py` → `pipeline.py` → `knowledge.py`：文档解析与知识抽取；
5. `sessions.py` → `generator.py` → `judge.py` → `reports.py`：出题、判分、报告；
6. `security.py`、`users.py`、`audit.py`：权限与审计；
7. `reader_api.py`、`chart_api.py`、`vector_api.py`：阅读器、图表、向量接口。

## 注释约定

- `# 文件定位`：职责、上下游、调用关系；
- `# 为什么`：设计原因和取舍；
- `# 注意`：边界、异常、SQL 注入、并发/线程、权限；
- 复杂多行结构在上方整块说明，避免把注释插入多行字符串破坏语法。

## 回退

```powershell
# 查看备份
Get-ChildItem D:\AI论文\alun\app_original

# 如需回退（谨慎，先确认没有手工修改）
Copy-Item D:\AI论文\alun\app_original\*.py D:\AI论文\alun\app\ -Force
```
