
# =============================================================================
# 文件：app/pipeline.py —— 文档入库与解析流水线
# =============================================================================
# 【这个文件做什么】
#   把用户上传的文件变成"可检索、可出题"的结构化知识库：
#   保存原始文件 → 解析出章节树/语义块/知识点/证据 → 质量分级 → 修复旧引用 → 写审计。
#
# 【在整个应用中的位置】
#   API 层（app/api）收到上传请求后调用本文件：
#     create_document()  校验类型、按内容哈希去重、保存文件、写 document 行
#     start_parse()      创建 task 记录，并把解析任务交给队列或后台线程
#     run_parse_task()   真正的执行体（在 worker 或子线程中运行）
#   解析完成后，进行中的学习会话和题库通过 repair_sessions_for_document() /
#   repair_questions_for_document() 重新挂接到新节点；audit 模块记录审计事件。
#
# 【一次解析的流水线】
#   validating → text_extract → layout → structure → chunk → knowledge → indexing → quality
#   实际由 run_parse_task 推进其中大部分阶段，_stage_labels 提供中文展示名。
#
# 【阅读要点】
#   * task 表描述"这一次执行"的进度，document 表描述"这份文档"的可用状态；
#   * 所有 SQL 都用 ? 占位符绑定参数，只有列名是代码内固定的字符串拼接；
#   * 重新解析会重建 node_id/kp_id，因此必须调用 repair_* 修复历史引用；
#   * 解析线程是守护线程，异常必须自己捕获并写库，不能指望主线程感知。
# =============================================================================

"""文档入库流水线与解析任务执行。"""

# 未来特性导入：把类型注解延迟成字符串（PEP 563）。
# 好处：注解不在运行时求值，可以放心使用 str | None、前向引用等写法，
# 也不会因为类型名尚未定义而在导入阶段报错。
# 必须放在模块文档字符串之后、其它 import 之前。

from __future__ import annotations


# ---- 标准库 ----
# hashlib：SHA-256 摘要，用于上传去重与文件完整性校验。
# os：操作系统接口（本文件当前未直接使用，保留导入不影响运行）。
# re：正则表达式，后面用 re.sub(r"\s+", "", ...) 归一化标题/引文里的空白。
# threading：队列不可用时，用后台守护线程执行解析，避免阻塞 HTTP 请求。
# time：perf_counter() 统计阶段耗时；sleep() 让中间状态可被前端轮询观察到。
# traceback：解析线程出错时把完整堆栈打印到服务端日志。
# pathlib.Path：面向对象的路径，提供 .suffix/.stem/.glob() 等能力。

import hashlib
import os
import re
import threading
import time
import traceback
from pathlib import Path


# ---- 项目内模块（相对导入，说明本文件位于 app 业务包内）----
# .parser            同包解析器：把 PDF/DOCX/图片等解析成 NodeDraft 节点树。
# ..platform.storage 上层基础设施：文件存储适配（本地磁盘或对象存储）。
# .audit             审计日志：记录"谁对哪份文档做了什么"。
# .config            UPLOAD_DIR：本地上传目录，storage 与 get_document_path 共用。
# .db                db()/dump()/fetch_all()/fetch_one()/iso()/load_json()/new_id()/now()：
#                    连接上下文、JSON 序列化、查询封装、时间与 ID 生成。
# .knowledge         build_chunks() 语义分块；extract_knowledge() 抽取知识点与证据。

from . import parser as parser_mod
from ..platform import storage
from . import audit
from .config import UPLOAD_DIR
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id, now
from .knowledge import build_chunks, extract_knowledge


# 阶段英文名 → 中文展示名。
# 数据库只存稳定的英文枚举（便于日志检索、断点续跑、国际化），
# 展示层再用这张表翻译；顺序与解析流水线一致。
# 说明：layout（版面分析）与 indexing（建索引）目前不在 run_parse_task 中显式推进，
# 由 parser 内部或后续索引流程使用，这里保留映射以便前端统一展示。

_stage_labels = {
    "validating": "校验文件",
    "text_extract": "抽取文本层",
    "layout": "版面与阅读顺序",
    "structure": "识别标题与结构树",
    "chunk": "语义分块",
    "knowledge": "抽取知识点与证据",
    "indexing": "建立索引",
    "quality": "解析质量门",
}



# 计算文件的 SHA-256 十六进制摘要（64 个字符）。
# 为什么不直接 read_bytes()？大 PDF 一次读入会占用大量内存，
# 因此按 1 MiB 分块流式读取，任何大小的文件都只占约 1 MiB 内存。

def sha256_file(path: Path) -> str:

    # 创建增量哈希对象；后续多次 update() 的结果等价于一次性哈希整个文件。

    h = hashlib.sha256()

    # 必须用二进制模式 "rb"：文本模式会做编码和换行转换，
    # 算出的摘要就不再是原始字节的摘要，去重会失效。

    with path.open("rb") as f:

        # iter(callable, sentinel)：反复调用 f.read(1 << 20)（即 1 MiB = 1048576 字节），
        # 直到返回 b""（空字节串，代表文件结束）才停止。

        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)

    # hexdigest() 返回小写十六进制字符串；同一内容永远得到同一摘要。

    return h.hexdigest()



# 在 task 表创建一条"排队中"的任务，返回 task_id。
# 先建任务再执行，是为了让前端立刻拿到 task_id 轮询进度；
# document_id / session_id 允许为空，因为 task 表也服务于其它业务类型。

def create_task(task_type: str, document_id: str | None = None, session_id: str | None = None) -> str:

    # 生成带前缀的唯一 ID（如 task_ab12...），日志里一眼能看出对象类型。

    task_id = new_id("task")

    # db() 是上下文管理器：正常退出自动 commit，异常自动 rollback 并关闭连接。

    with db() as conn:

        # 参数化 INSERT：SQL 里用 ? 占位，值放在下一个元组，由 sqlite3 负责转义，杜绝 SQL 注入。
        # 字段：task_id 主键；type 任务类型；status 状态(queued/running/succeeded/failed)；
        #       stage 当前阶段（配合 _stage_labels）；progress 0-100；
        #       document_id/session_id 关联对象；created_at/updated_at 为 UTC ISO-8601 字符串。

        conn.execute(
            "INSERT INTO task (task_id, type, status, stage, progress, document_id, session_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, task_type, "queued", "queued", 0, document_id, session_id, iso(), iso()),
        )

    # 返回任务 ID，调用方用它查询进度或关联日志。

    return task_id



# 局部更新 task 表：只更新调用方显式传入的字段（None 表示"本次不改"）。
# 这种动态拼 SET 的写法避免为每种参数组合写一条 SQL；
# 列名全部来自本函数的固定分支，不接受外部输入，因此 f-string 拼列名是安全的；
# 真正的"值"仍然全部走 ? 参数绑定。

def update_task(task_id: str, *, status: str | None = None, stage: str | None = None,
                progress: int | None = None, result: dict | None = None, error: dict | None = None) -> None:

    # fields 收集 "列名=?" 片段；第一项固定是 updated_at，保证任何更新都刷新时间。

    fields = ["updated_at=?"]

    # values 与 fields 严格一一对应，顺序错位会把值写进错误的列。

    values: list = [iso()]

    # 以下 5 个 if 是同一模式：传了才更新，实现"部分更新"语义。

    if status is not None:
        fields.append("status=?")
        values.append(status)
    if stage is not None:
        fields.append("stage=?")
        values.append(stage)
    if progress is not None:
        fields.append("progress=?")
        values.append(progress)

    # result 存成功结果（文档状态、质量、耗时），先 dump 成 JSON 文本再入库。

    if result is not None:
        fields.append("result=?")
        values.append(dump(result))

    # error 存失败信息（错误码/消息/是否可重试），同样是 JSON 文本。

    if error is not None:
        fields.append("error=?")
        values.append(dump(error))

    # 最后追加 WHERE 用的 task_id，对应 SQL 末尾的 ?。

    values.append(task_id)
    with db() as conn:

        # 只拼接列名片段；tuple(values) 按顺序绑定所有值。

        conn.execute(f"UPDATE task SET {', '.join(fields)} WHERE task_id=?", tuple(values))



# 发起一次文档解析，立即返回 task_id（不等待解析完成）。
# 执行策略：
#   1) 若平台配置了外部任务队列，投递 "parse" 消息，由独立 worker 消费（可跨进程/跨机器）；
#   2) 否则退化为本进程的后台守护线程，保证单机部署也能用。
# 参数：document_id 文档主键；path 原始文件路径；doc_type 解析类型提示（auto 表示自动判断）；
#       title 用户指定标题，None 表示用文件名或文档内标题。

def start_parse(document_id: str, path: Path, doc_type: str = "auto", title: str | None = None) -> str:

    # 先落一条 queued 任务，队列投递失败时也能继续用线程执行。

    task_id = create_task("document_parse", document_id)

    # 队列是"增强能力"：任何异常都静默降级，绝不能让上传接口失败。

    try:

        # 函数内延迟导入：避免模块循环导入，也让缺少队列依赖时不影响本模块加载。

        from ..platform import queue

        # enabled() 判断是否配置并启用了队列；enqueue() 返回 True 表示投递成功。
        # 消息体必须是可 JSON 序列化的纯数据，所以 path 用 str(path) 转成字符串。

        if queue.enabled() and queue.enqueue("parse", {
            "task_id": task_id, "document_id": document_id, "path": str(path),
            "doc_type": doc_type, "title": title,
        }):

            # 投递成功：任务交给外部 worker，本进程直接返回。

            return task_id

    # 队列不可用（未安装客户端/连接失败/未配置）时走下面的线程兜底。

    except Exception:

        # 故意 pass：降级是预期行为，不向用户暴露队列错误。

        pass

    # 创建守护线程执行 run_parse_task：
    #   target 入口函数；args 位置参数；name 方便日志定位是哪个文档；
    #   daemon=True 表示主进程退出时线程随之结束，不阻塞服务关闭。
    # 注意：线程不能复用请求线程的数据库连接，db() 每次调用都会单独取连接。

    thread = threading.Thread(
        target=run_parse_task,
        args=(task_id, document_id, path, doc_type, title),
        name=f"parse-{document_id}",
        daemon=True,
    )

    # start() 立即返回，解析在后台继续；调用方拿 task_id 轮询进度。

    thread.start()
    return task_id



# 解析任务的实际执行体（同步函数，运行在后台线程或队列 worker 中）。
# 整体是"一个 try 包住全流程"：
#   * 正常路径按阶段推进 task/document 状态，最后写统计、审计和成功结果；
#   * 任何异常都被捕获，转成结构化错误码写回 task/document，避免线程静默死亡；
#   * 重新解析时先删旧 node/chunk/kp/evidence，再写新数据，避免两份结果并存。
# 阶段顺序：validating → text_extract → structure → chunk → knowledge → quality。

def run_parse_task(task_id: str, document_id: str, path: Path, doc_type: str, title: str | None) -> None:
    try:

        # timings 记录各阶段累计毫秒数，最终写入 task.result 与 document.stats，便于性能分析。

        timings: dict[str, int] = {}

        # perf_counter() 是单调高精度计时器，不受系统校时影响，适合测耗时。

        started = time.perf_counter()


        # 内部小函数：记录"从开始到现在"的毫秒数；闭包直接读写外层 timings/started。

        def mark(name: str) -> None:
            timings[name] = int((time.perf_counter() - started) * 1000)


        # 阶段 1 校验：任务 running/3%，文档 validating。
        # 前端轮询 task.stage 就能显示"校验文件"。

        update_task(task_id, status="running", stage="validating", progress=3)
        _set_document(document_id, status="validating")

        # 短暂休眠让出 CPU，也让前端有机会观察到 validating 这个中间态。

        time.sleep(0.2)


        # 阶段 2 文本抽取：文档进入 parsing，表示"正在解析、暂不可用"。

        update_task(task_id, stage="text_extract", progress=12)
        _set_document(document_id, status="parsing")

        # 读取旧文档版本号；fetch_one 无结果返回 None，用 or 兜底成默认版本 1。
        # doc_version 用于"重新解析后 +1"，让前端能判断结构是否已更新。

        prior_doc = fetch_one("SELECT doc_version FROM document WHERE doc_id=?", (document_id,)) or {"doc_version": 1}

        # 是否已存在节点 = 是否"重新解析"。
        # SELECT 1 ... LIMIT 1 只判断存在性，不取具体数据，比 SELECT * 更轻。

        had_nodes = bool(fetch_one("SELECT 1 FROM node WHERE doc_id=? LIMIT 1", (document_id,)))

        # 调用解析器，返回对象包含：
        #   nodes（节点树）、title、doc_type、page_count、text_chars、
        #   stats（解析统计）、quality（score/level/issues/metrics）。
        # doc_type_hint/title_hint 只是提示，解析器可结合文件内容自行判断。

        parsed = parser_mod.parse_document(path, doc_type_hint=doc_type, title_hint=title)
        mark("parse_ms")


        # 阶段 3 结构入库：先把任务进度推到 45%。

        update_task(task_id, stage="structure", progress=45)
        time.sleep(0.1)

        # 延迟导入 versions：重新解析前要给旧结构拍快照，支持对比/回滚。

        from . import versions as versions_mod

        # 只有已有节点才需要快照；首次解析没有历史可保存。

        if had_nodes:
            versions_mod.snapshot(document_id, reason="重新解析前自动快照")

        # 一个事务内"先删后插"，保证重新解析不产生孤儿数据：
        #   node（章节树）→ chunk（语义块）→ knowledge_point（知识点）→ evidence（证据）。
        # 这些表都通过 doc_id 归属文档；question/session 不在这里删，交由 repair_* 重挂。
        # 如果不删，旧 node_id 会与新解析结果并存，导致检索重复、统计翻倍。

        with db() as conn:
            conn.execute("DELETE FROM node WHERE doc_id=?", (document_id,))
            conn.execute("DELETE FROM chunk WHERE doc_id=?", (document_id,))
            conn.execute("DELETE FROM knowledge_point WHERE doc_id=?", (document_id,))
            conn.execute("DELETE FROM evidence WHERE doc_id=?", (document_id,))

            # order 是文档内全局递增序号，保证节点顺序稳定（不依赖 parser 的 order_no 是否重复）。

            order = 0

            # 遍历解析出的节点；parser 返回的已是按阅读顺序排好的扁平列表，层级由 parent_id 表达。

            for node in _flatten(parsed.nodes):
                order += 1

                # 插入 node 行，字段依次为：
                #   node_id、doc_id、parent_id（顶层为 None）、level（标题层级）、node_type、
                #   order_no（解析器原始序号）、title、text、page_start/page_end（页码范围）、
                #   bbox（版面坐标，这里存 None）、confidence（置信度）、path（章节路径）、
                #   char_count（现算 len(text)）、order_index（本次遍历的全局序号）。

                conn.execute(
                    "INSERT INTO node (node_id, doc_id, parent_id, level, node_type, order_no, title, text,"
                    " page_start, page_end, bbox, confidence, path, char_count, order_index)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node.node_id, document_id, node.parent_id, node.level, node.node_type, node.order_no,
                        node.title, node.text, node.page_start, node.page_end, None, node.confidence,
                        node.path, len(node.text), order,
                    ),
                )

        # 记录"结构入库"阶段耗时（含删旧数据与批量插入）。

        mark("structure_ms")

        # 阶段 4 语义分块：chunk 是检索与提示词拼装的最小单位。

        update_task(task_id, stage="chunk", progress=58)

        # build_chunks 返回 dict 列表：chunk_id、node_id（来源节点）、text、
        # token_count（估算 token 数，用于控制上下文长度）、order_no（文档内顺序）。

        chunks = build_chunks(document_id, parsed.nodes)

        # 逐块写入 chunk 表；token_count 让检索阶段能按预算截断。

        with db() as conn:
            for ch in chunks:
                conn.execute(
                    "INSERT INTO chunk (chunk_id, doc_id, node_id, text, token_count, order_no)"
                    " VALUES (?,?,?,?,?,?)",
                    (ch["chunk_id"], document_id, ch["node_id"], ch["text"], ch["token_count"], ch["order_no"]),
                )
        mark("chunk_ms")


        # 阶段 5 知识抽取：这是最慢的一步，可能调用大模型。

        update_task(task_id, stage="knowledge", progress=72)

        # 返回 (kps, evidences)：
        #   kps       知识点：name/kp_type/definition/importance/difficulty/evidence_ids/prerequisites；
        #   evidences 证据：quote 原文引文、page_no 页码、char_start/char_end 字符区间、strength 强度。
        # 证据让"知识点—原文"可溯源，出题和答疑时都会用到。

        kps, evidences = extract_knowledge(document_id, parsed.nodes)
        mark("knowledge_ms")

        # 同一事务写知识点与证据，保证两者要么都成功、要么都回滚。
        # evidence_ids / prerequisites 是列表，SQLite 没有数组类型，用 dump() 存 JSON 文本。

        with db() as conn:
            for kp in kps:
                conn.execute(
                    "INSERT INTO knowledge_point (kp_id, doc_id, node_id, name, kp_type, definition,"
                    " importance, difficulty, evidence_ids, prerequisites, order_no)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        kp["kp_id"], document_id, kp["node_id"], kp["name"], kp["kp_type"],
                        kp["definition"], kp["importance"], kp["difficulty"],
                        dump(kp["evidence_ids"]), dump(kp["prerequisites"]), kp["order_no"],
                    ),
                )
            for ev in evidences:
                conn.execute(
                    "INSERT INTO evidence (evidence_id, doc_id, node_id, page_no, quote, char_start,"
                    " char_end, strength, context) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ev["evidence_id"], document_id, ev["node_id"], ev["page_no"], ev["quote"],
                        ev["char_start"], ev["char_end"], ev["strength"], ev["context"],
                    ),
                )


        # 阶段 6 质量门：质量分数在解析阶段已算好，这里只汇总并落库。

        update_task(task_id, stage="quality", progress=92)
        mark("quality_ms")
        timings["total_ms"] = int((time.perf_counter() - started) * 1000)

        # 复制一份解析统计再补充流水线统计，避免修改解析器返回的原对象。

        stats = dict(parsed.stats)

        # 以下三个计数用于前端概览与质量报告。

        stats["node_count"] = len(parsed.nodes)
        stats["chunk_count"] = len(chunks)
        stats["kp_count"] = len(kps)
        stats["timings"] = timings
        quality = parsed.quality
        stats["quality_metrics"] = quality.get("metrics", {})

        # 状态判定：pass（合格）/ review（可用但建议抽查）→ ready；
        # 其它（如 fail）→ needs_review，表示"解析出来了但质量存疑，需要人工复核"。

        status = "ready" if quality["level"] in ("pass", "review") else "needs_review"

        # 兜底：没有节点或有效字符不足 50，直接判 failed。
        # 有些扫描件会"成功"返回空文本，对下游毫无价值，标 failed 比 needs_review 更诚实。

        if not parsed.nodes or parsed.text_chars < 50:
            status = "failed"

        # 回写 document：状态、元数据、质量、统计，并清空 error。
        # doc_version 只在"重新解析"（had_nodes=True）时 +1；首次解析保持 1。

        _set_document(
            document_id,
            status=status,
            title=parsed.title,
            doc_type=parsed.doc_type,
            page_count=parsed.page_count,
            doc_version=int(prior_doc.get("doc_version") or 1) + (1 if had_nodes else 0),
            quality_score=quality["score"],
            quality_level=quality["level"],
            quality_issues=dump(quality["issues"]),
            stats=dump(stats),
            error=None,
        )

        # 重新解析后修复进行中的学习会话：旧 plan 里的 node_id/kp_id 已失效。

        repair_sessions_for_document(document_id)

        # 再修复题库题目：按证据引文把旧题重挂到新节点，找不到的转待复核。

        repair_questions_for_document(document_id)

        # 重新查文档行，拿 user_id/tenant_id/title 写审计（这些信息不在当前上下文中）。

        docrow = fetch_one("SELECT user_id, tenant_id, title FROM document WHERE doc_id=?", (document_id,)) or {}

        # 查用户名；查不到就用空串，审计不能因为用户被删除而失败。

        userrow = fetch_one("SELECT username FROM app_user WHERE user_id=?", (docrow.get("user_id", ""),)) or {}

        # 审计事件：document_ready。
        # 即使最终状态是 needs_review/failed 也记这一条，具体结果在 detail.status，
        # 便于按事件类型统一检索"解析完成"的记录。

        audit.log_event(
            user_id=docrow.get("user_id", ""), username=userrow.get("username", ""),
            tenant_id=docrow.get("tenant_id", ""),
            event_type="document_ready",
            detail={"document_id": document_id, "status": status, "quality": quality.get("score"),
                    "stats": stats, "title": docrow.get("title")},
        )

        # 任务成功收尾：succeeded / done / 100%，result 带文档状态、质量详情和耗时，
        # 前端任务详情页可直接展示，无需再查 document 表。

        update_task(
            task_id, status="succeeded", stage="done", progress=100,
            result={"document_id": document_id, "status": status, "quality": quality, "timings": timings},
        )

    # 捕获所有异常（noqa 表示宽泛捕获是刻意的）。
    # 这里不重新抛出：任务已有数据库记录，把错误写进 task/document 比让线程崩溃更有用。

    except Exception as exc:  # noqa: BLE001

        # 完整堆栈打到服务端日志，保留出错文件与行号，便于运维定位。

        traceback.print_exc()

        # 区分错误类型：
        #   ModuleNotFoundError → 服务器缺解析依赖（如 fitz/pymupdf），错误码 DEPENDENCY_MISSING，
        #     提示安装 requirements.txt；
        #   其它异常 → PARSE_FAILED，消息取异常文本。

        if isinstance(exc, ModuleNotFoundError):

            # 稳定错误码供前端判断展示哪种引导，不要用中文消息做程序判断。

            code = "DEPENDENCY_MISSING"

            # exc.name 是缺失的模块名，拼进消息让运维知道该装什么。

            message = f"服务器缺少解析依赖 {exc.name}，请安装 requirements.txt 后重试"
        else:
            code = "PARSE_FAILED"
            message = str(exc)

        # 文档标记 failed，并记录结构化错误：code 机器码、message 人话、stage 出错阶段。
        # dump() 把 dict 转成 JSON 文本写入 document.error。

        _set_document(document_id, status="failed", error=dump({"code": code, "message": message, "stage": "parse"}))

        # 任务也标记 failed；retryable=True 表示用户可点"重新解析"重试（装好依赖后即可成功）。

        update_task(
            task_id, status="failed", stage="failed", progress=100,
            error={"code": code, "message": message, "retryable": True},
        )



# 重新解析后的"会话修复"：把未完成会话的 plan 重新挂到新节点。
# 背景：解析会重建 node_id/kp_id，而 session.plan 里保存的是旧 ID；
# 不修复的话，进行中的会话会指向已删除节点，出题与报告都会失败。
# 策略：按"章节标题"精确/包含匹配节点，再按"知识点名称"匹配 kp；
# 已生成题目的题干与证据保留，只同步 node_id/kp_id。
# 返回：实际修复的会话数量。

def repair_sessions_for_document(document_id: str) -> int:
    """重新解析后，按章节标题/知识点名称把未完成会话的 plan 重新挂到新节点。

    D7/重新解析会重建 node_id、kp_id；这个修复让正在进行的会话可以继续，
    已出过的题目仍保留原题干与证据引文。
    """

    # 取新结构的全部节点与知识点。

    nodes = fetch_all("SELECT * FROM node WHERE doc_id=?", (document_id,))
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=?", (document_id,))

    # 没有节点说明解析失败/文档为空，无从匹配，直接返回 0。

    if not nodes:
        return 0


    # compact：去掉所有空白字符，抹平"第 1 章"与"第1章"这类差异。

    def compact(s: str) -> str:
        return re.sub(r"\s+", "", s or "")


    # 建立"归一化标题 → 节点列表"索引；用列表是因为同一标题可能出现多次（如每章的"小结"）。

    node_by_title: dict[str, list[dict]] = {}
    for n in nodes:

        # setdefault(key, []).append(n) 一行完成分组；title 可能为 NULL，用 or "" 兜底。

        node_by_title.setdefault(compact(n.get("title") or ""), []).append(n)

    # 建立"节点 ID → 知识点列表"索引，供后面按节点挑 kp。

    kp_by_node: dict[str, list[dict]] = {}
    for kp in kps:
        kp_by_node.setdefault(kp["node_id"], []).append(kp)

    # 每个节点下的知识点按重要度降序，后面取 pool[0] 就是最重要的。

    for items in kp_by_node.values():

        # 排序键取负数实现降序；importance 为 None 时按 0 处理。

        items.sort(key=lambda x: -(x.get("importance") or 0))


    # 只修复未结束的会话；已结束会话的报告已固化，不需要再改。

    sessions = fetch_all("SELECT * FROM session WHERE doc_id=? AND state<>'finished'", (document_id,))
    repaired = 0

    # 逐会话处理；plan 是 JSON 文本，解析失败或为空时回退到 {}。

    for session in sessions:
        plan = load_json(session.get("plan"), {})
        changed = False

        # 遍历 plan.items：每一项对应一个学习章节/知识点。

        for item in plan.get("items", []):
            title = item.get("section_title") or ""

            # 先精确匹配（归一化后完全相同）；or [] 保证结果一定是列表。

            candidates = node_by_title.get(compact(title)) or []
            if not candidates:

                # 精确匹配失败时退化为"包含"匹配：旧标题与新标题任一方包含另一方即算候选。
                # 条件里要求 compact(title) 非空，否则空串是任何串的子串，会匹配到所有节点。

                candidates = [
                    n for key, vals in node_by_title.items()
                    if compact(title) and (compact(title) in key or key in compact(title))
                    for n in vals
                ]

            # 取第一个候选；没有候选就跳过本项的节点更新。

            node = candidates[0] if candidates else None
            if node:
                if item.get("node_id") != node["node_id"]:
                    item["node_id"] = node["node_id"]
                    changed = True
                item["section_title"] = node.get("title") or title

            # 取该节点下的知识点池（已按重要度降序）。

            pool = kp_by_node.get(item.get("node_id", ""), [])
            if pool:

                # 先按名称精确匹配；next(..., None) 找不到时返回 None。

                exact = next((k for k in pool if compact(k["name"]) == compact(item.get("kp_name", ""))), None)

                # 精确匹配不到就取最重要的知识点，保证 plan 项仍有可用的 kp_id。

                kp = exact or pool[0]
                if item.get("kp_id") != kp["kp_id"]:
                    item["kp_id"] = kp["kp_id"]
                    item["kp_name"] = kp["name"]
                    changed = True

        # 只有确实改动过才做后续题目同步与落库，避免无谓写库。

        if changed:
            # 已生成题目的 node_id/kp_id 也同步到新结构，保证报告能聚合章节

            # 同步已生成题目：turn 表按 seq_no 关联 plan.items，
            # 把 question 的 node_id/kp_id 更新为新结构，保证报告能按章节聚合。

            turns = fetch_all("SELECT * FROM turn WHERE session_id=?", (session["session_id"],))
            for turn in turns:

                # 找不到对应 plan 项说明历史数据不一致，跳过该轮，不让整个修复失败。

                item = next((i for i in plan.get("items", []) if i.get("seq_no") == turn.get("seq_no")), None)
                if not item:
                    continue
                q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
                if q:
                    with db() as conn:

                        # 只更新两个外键；题干、选项、答案、证据引文都保持原样。

                        conn.execute(
                            "UPDATE question SET node_id=?, kp_id=? WHERE q_id=?",
                            (item["node_id"], item["kp_id"], q["q_id"]),
                        )

            # plan 修好后整体写回 session（JSON 文本覆盖）。

            with db() as conn:
                conn.execute("UPDATE session SET plan=? WHERE session_id=?", (dump(plan), session["session_id"]))
            repaired += 1

    # 返回修复数量，调用方可用于日志/统计。

    return repaired



# 重新解析后的"题库修复"：把旧题重新挂到新结构节点。
# 为什么需要：旧实现只修 session.plan，question 仍指向已删除的 node_id，
# 导致"证据支持率"统计失败、题目详情无法回原文。
# 策略（按可靠性从高到低）：
#   1) 所有证据引文都能在某个节点文本中完整找到 → 重挂到该节点；
#      多候选时优先 section 节点、页码覆盖、order_index 靠前；
#   2) 找不到 → 用 difflib 最长公共子串找最相似节点，平均覆盖率 ≥55% 才接受，
#      并强制标 pending_review（自动修复必须人工确认）；
#   3) 无引文或仍找不到 → 标 pending_review，绝不静默保留假引用。
# 返回 {"rebound": 重挂数, "pending": 转待复核数}。

def repair_questions_for_document(document_id: str) -> dict:
    """重新解析后把题库里的旧题重新挂到新结构节点。

    旧实现只修复会话 plan，题库中的题目仍指向已删除的 node_id，导致
    “证据支持率”统计失败、题目详情无法回原文。这里按证据引文反查节点：
    引文能在某个节点中完整找到即重挂；找不到则转为待复核，不静默保留假引用。
    """

    # 新节点按 order_index 排序，打分时"越靠前越优先"。

    nodes = fetch_all("SELECT * FROM node WHERE doc_id=? ORDER BY order_index", (document_id,))
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=? ORDER BY order_no", (document_id,))
    if not nodes:
        return {"rebound": 0, "pending": 0}
    kp_by_node: dict[str, list[dict]] = {}
    for kp in kps:
        kp_by_node.setdefault(kp["node_id"], []).append(kp)

    # norm：去掉所有空白，避免引文与节点文本因换行/空格差异匹配失败。

    norm = lambda s: re.sub(r"\s+", "", s or "")

    # 预处理每个节点的归一化全文，避免在双层循环里反复做正则替换（空间换时间）。

    node_text = {n["node_id"]: norm(n.get("text") or "") for n in nodes}
    rebound = pending = 0

    # 只处理 active / pending_review：archived/deleted 无需维护；已待复核的也再试一次。

    for q in fetch_all("SELECT * FROM question WHERE doc_id=? AND status IN ('active','pending_review')", (document_id,)):
        evs = load_json(q.get("evidence"), [])

        # 提取非空引文并归一化；str(... or "") 兼容 quote 为 None 或非字符串。

        quotes = [norm(str(e.get("quote") or "")) for e in evs if str(e.get("quote") or "").strip()]

        # 没有任何引文就无法定位，直接转待复核。

        if not quotes:
            with db() as conn:
                conn.execute("UPDATE question SET status='pending_review' WHERE q_id=?", (q["q_id"],))
            pending += 1
            continue

        # 候选节点：每一段引文都能在该节点原文中完整找到。
        # all(...) 要求全部引文都命中，避免只匹配一半就误挂。

        candidates = [
            n for n in nodes
            if all(quote in node_text[n["node_id"]] for quote in quotes)
        ]

        # 没有节点能完整包含所有引文，进入模糊匹配分支。

        if not candidates:
            # 旧题的引文可能横跨重新切分后的两个节点。用最长公共子串找
            # 最接近的节点，覆盖度 ≥55% 才接受，避免把题目随便挂到无关章节。

            # difflib 是标准库；只在需要模糊匹配时导入，减少模块加载开销。

            from difflib import SequenceMatcher
            best_node, best_score = None, 0.0
            for n in nodes:
                text = node_text[n["node_id"]]
                if not text:
                    continue
                score = 0.0

                # 对每段引文计算"最长公共子串长度 / 引文长度"，再取平均作为覆盖率。

                for quote in quotes:

                    # find_longest_match 返回最长公共子串的位置与长度，match.size 即长度。

                    match = SequenceMatcher(None, quote, text).find_longest_match(0, len(quote), 0, len(text))
                    score += match.size / max(1, len(quote))
                score /= max(1, len(quotes))
                if score > best_score:
                    best_node, best_score = n, score

            # 覆盖率 ≥55% 才接受；低于阈值宁可转待复核，也不乱挂到无关章节。

            if best_node is not None and best_score >= 0.55:
                pool = kp_by_node.get(best_node["node_id"], [])
                kp = pool[0] if pool else None

                # 模糊匹配属于尽力而为：即使挂上也强制 pending_review，让人工确认引文是否对得上。

                with db() as conn:
                    conn.execute(
                        "UPDATE question SET node_id=?, kp_id=?, status='pending_review' WHERE q_id=?",
                        (best_node["node_id"], kp["kp_id"] if kp else q.get("kp_id"), q["q_id"]),
                    )
                pending += 1
                continue

        # 模糊匹配也没有足够相似的节点：只改状态，保留原 node_id/kp_id 供人工对照。

        if not candidates:
            with db() as conn:
                conn.execute("UPDATE question SET status='pending_review' WHERE q_id=?", (q["q_id"],))
            pending += 1
            continue
        # 优先章节节点，其次页码范围能覆盖证据页码的节点

        # 候选节点排序键（元组字典序，越小越优先）：
        #   1) section 节点优先（0）而非段落节点（1）；
        #   2) 页码范围覆盖全部证据页码的优先（0）；
        #   3) order_index 越小越靠前。

        def score(n):
            ev_pages = [int(e.get("page") or 0) for e in evs if e.get("page")]

            # 没有页码信息时视为匹配（True），避免因缺元数据把好节点排到后面。

            page_ok = all(n["page_start"] <= p <= n["page_end"] for p in ev_pages) if ev_pages else True
            return (0 if n["node_type"] == "section" else 1, 0 if page_ok else 1, n["order_index"])
        node = sorted(candidates, key=score)[0]
        pool = kp_by_node.get(node["node_id"], [])

        # 优先复用题目原来的 kp_id（若该节点下仍存在），否则取该节点最重要的 kp。

        kp = next((k for k in pool if k["kp_id"] == q.get("kp_id")), None) or (pool[0] if pool else None)

        # 只有 node_id 或 kp_id 真的变化才写库。

        if q.get("node_id") != node["node_id"] or (kp and q.get("kp_id") != kp["kp_id"]):
            with db() as conn:
                conn.execute(
                    "UPDATE question SET node_id=?, kp_id=? WHERE q_id=?",
                    (node["node_id"], kp["kp_id"] if kp else q.get("kp_id"), q["q_id"]),
                )
            rebound += 1

    # 有实际修复才写审计，避免产生大量空事件。

    if rebound or pending:

        # 审计事件 question_rebound：记录本次重挂/转待复核数量
        # （tenant_id 留空，可再按 document_id 追溯到所属租户）。

        audit.log_event(
            event_type="question_rebound", tenant_id="",
            detail={"document_id": document_id, "rebound": rebound, "pending_review": pending},
        )
    return {"rebound": rebound, "pending": pending}



# 生成器：按顺序逐个产出节点。
# 名字叫 flatten，但这里没有递归——parser 返回的 parsed.nodes 已是按阅读顺序
# 排好的扁平列表，层级关系由 parent_id 表达。
# 用 yield 而不是返回 list，可以边遍历边写库，减少内存占用。

def _flatten(nodes: list[parser_mod.NodeDraft]):

    # 遍历传入的节点序列。

    for node in nodes:

        # yield 交出一个节点后暂停，下次迭代从这里继续。

        yield node



# 通用更新 document 表的辅助函数，例如：
#   _set_document(doc_id, status="ready", title="...")
# 关键字参数名直接作为列名；调用方全是本文件内部代码，不存在注入风险。
# 每次调用自动刷新 updated_at，避免调用方遗漏。

def _set_document(document_id: str, **fields) -> None:

    # 强制写入更新时间（若调用方也传 updated_at 会被这里覆盖，本文件没有这种调用）。

    fields["updated_at"] = iso()

    # 生成 "status=?, title=?, ..."；dict 保持插入顺序，fields 与 values 天然对齐。

    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:

        # f-string 只拼列名；所有值仍走 ? 绑定，最后追加 WHERE 的 document_id。

        conn.execute(f"UPDATE document SET {sets} WHERE doc_id=?", tuple(fields.values()) + (document_id,))



# 创建文档记录：校验扩展名 → 按内容哈希去重 → 保存文件 → 写 document 行。
# 返回 (信息 dict, 是否已存在)：
#   新文档   → ({"doc_id": ..., "file_path": ...}, False)
#   命中重复 → (数据库整行 dict, True)，调用方应跳过重复解析。
# 参数：tenant_id/user_id 决定归属与去重范围（多租户隔离）；
#       file_name 只用于取扩展名和默认标题，不直接作为磁盘文件名（防路径穿越）；
#       content 是完整字节；doc_type 是解析提示；language 影响分词/OCR；
#       source_uri 记录来源（如原始 URL）。

def create_document(
    *,
    tenant_id: str,
    user_id: str,
    file_name: str,
    mime_type: str,
    content: bytes,
    doc_type: str = "auto",
    language: str = "zh-CN",
    title: str | None = None,
    source_uri: str = "",
) -> tuple[dict, bool]:

    # 取小写扩展名（.PDF 也能识别）；Path("a.tar.gz").suffix 为 ".gz"，对本场景够用。

    suffix = Path(file_name).suffix.lower()

    # 白名单校验，避免 .exe/.sh 等可执行文件被当作文档上传。

    if suffix not in (".pdf", ".docx", ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):

        # 抛 ValueError，错误码 DOC_UNSUPPORTED 由上层 API 转成 4xx 响应。

        raise ValueError("DOC_UNSUPPORTED")

    # 对内容算 SHA-256，用于去重与完整性校验。

    digest = hashlib.sha256(content).hexdigest()
    with db() as conn:
        # 去重必须限定到当前用户：同一租户下其他用户的同名/同哈希文档
        # 不应该阻止本次上传，否则会把无权访问的 doc_id 返回给前端。

        # 去重条件（缺一不可）：
        #   tenant_id + user_id —— 只在本用户范围内去重，避免把他人 doc_id 返回给当前用户（越权）；
        #   sha256              —— 内容完全相同；
        #   status IN (...)     —— 只复用可用文档，失败/解析中的允许重新上传；
        #   deleted_at IS NULL  —— 软删除的不算重复。
        # ORDER BY created_at DESC LIMIT 1：多条时取最新一条。

        existing = conn.execute(
            "SELECT * FROM document WHERE tenant_id=? AND user_id=? AND sha256=?"
            " AND status IN ('ready','needs_review') AND deleted_at IS NULL"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_id, user_id, digest),
        ).fetchone()

    # 命中重复：dict(existing) 把 sqlite3.Row 转成普通 dict 返回。

    if existing:

        # 第二返回值 True = 已存在，调用方不应再发起解析。

        return dict(existing), True


    # 新文档 ID，形如 doc_xxx。

    doc_id = new_id("doc")

    # 磁盘文件名 = doc_id + 扩展名。
    # 不用原始文件名：可能含 ../、空格、特殊字符，直接拼路径会有目录穿越与编码问题。

    key = f"{doc_id}{suffix}"

    # 交给 storage 保存（本地磁盘或对象存储）；传 mime_type 便于设置 Content-Type。

    storage.put(key, content, mime_type)

    # 本地模式下的预期路径；对象存储模式可能不存在，get_document_path() 会兜底。

    path = UPLOAD_DIR / key
    with db() as conn:

        # document 表 21 个字段：doc_id/tenant_id/user_id/title/doc_type/source_uri/
        # file_name/mime_type/size_bytes/page_count(先 0)/sha256/status(uploaded)/
        # doc_version(1)/language/quality_score(0)/quality_level(review)/
        # quality_issues("[]")/stats("{}")/error(NULL)/created_at/updated_at。
        # 注意 "[]"/"{}" 是 JSON 文本，不是 Python 的列表/字典。

        conn.execute(
            "INSERT INTO document (doc_id, tenant_id, user_id, title, doc_type, source_uri, file_name, mime_type,"
            " size_bytes, page_count, sha256, status, doc_version, language, quality_score, quality_level,"
            " quality_issues, stats, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id, tenant_id, user_id, title or Path(file_name).stem, doc_type, source_uri, file_name, mime_type,
                len(content), 0, digest, "uploaded", 1, language, 0, "review", "[]", "{}", None, iso(), iso(),
            ),
        )

    # 返回新文档信息；False 表示新建，调用方随后应调用 start_parse()。

    return {"doc_id": doc_id, "file_path": str(path)}, False



# 根据文档 ID 找回原始文件路径，返回 Path 或 None（文档不存在/文件丢失）。
# 调用场景：重新解析、下载原文、在线预览。

def get_document_path(document_id: str) -> Path | None:

    # 先确认文档存在，不存在直接返回 None，避免无谓扫描目录。

    with db() as conn:
        row = conn.execute("SELECT file_name FROM document WHERE doc_id=?", (document_id,)).fetchone()
    if not row:
        return None

    # 本地磁盘优先：按 "doc_id.*" 通配查找，兼容历史文件的不同扩展名（.jpg/.jpeg）。
    # 同一文档只会有一份文件，找到第一个就返回。

    for p in UPLOAD_DIR.glob(f"{document_id}.*"):
        return p

    # 本地没有则尝试 storage.get(key)：对象存储模式下文件不在本地目录，
    # 需要通过存储适配层取回本地可读路径；按支持的扩展名逐个尝试。

    for suffix in (".pdf", ".docx", ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        key = f"{document_id}{suffix}"
        try:
            return storage.get(key)

        # 只吞 FileNotFoundError；权限/网络等其它异常应向上抛，避免掩盖真实故障。

        except FileNotFoundError:
            continue

    # 所有后缀都没找到：返回 None，调用方按"文件缺失"处理。

    return None
