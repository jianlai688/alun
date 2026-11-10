# A论后端代码学习注释版

本目录是 `alun/app` 注释版的镜像；当前 `alun/app` 已同步注释，原始未注释代码备份在 `alun/app_original`。

## 阅读方式

1. 先读 `config.py`：理解全局配置、路径、数据库/Redis/S3/模型网关的接线方式；
2. 再读 `main.py`：看 FastAPI 应用如何组装、中间件如何挂载、路由如何注册；
3. 然后按功能读：
   - 文档解析：`parser.py` → `pipeline.py` → `knowledge.py`
   - 问答与判分：`sessions.py` → `generator.py` → `judge.py`
   - 数据层：`db.py`、`models` 相关 SQL
   - 平台化：`reader_api.py`、`chart_api.py`、`vector_api.py`
   - 权限/安全：`security.py`、`users.py`、`audit.py`

## 注释约定

- `# 【文件定位】`：文件职责和上下游关系；
- `# 【为什么】`：解释设计原因，而不只是复述代码；
- `# 【注意】`：边界条件、异常、SQL 注入防护、线程/异步注意事项；
- 函数 docstring：参数、返回值、异常；
- 复杂多行结构（SQL、三引号、长表达式）在块上方整体注释，避免把注释插进字符串破坏语法。

## 校验

```powershell
python -m py_compile alun/app_annotated/*.py
```

## 覆盖范围

- 29 个 Python 文件全部生成注释版；
- 原始代码 9,035 行 → 注释版 20,976 行；
- 逐文件行数见 `manifest.json`；
- 全部文件 `py_compile` 通过，且去除新增 docstring 后 AST 与 `alun/app` 原文件一致。

注释已同步到 `alun/app`；如需回退，使用 `alun/app_original`。
