"""网页链接导入：抓取公开网页并转为可解析的 Markdown。"""
# =============================================================================
# 模块导读（给初次阅读本文件的同学）
# -----------------------------------------------------------------------------
# 一、这个文件是做什么的
#     它只做一件事：把一个用户提供的网址，变成"可以入库的一段内容"。
#     具体分三种情况：
#       1) 网址指向 PDF      -> 原样下载 PDF 字节，交给下游 PDF 解析器；
#       2) 网址是纯文本      -> 原样下载文本字节；
#       3) 网址是普通网页    -> 用标准库 HTMLParser 抽取标题、段落、列表、表格，
#                              拼成 Markdown 文本再交给下游。
#     它不碰数据库、不碰鉴权、不碰任务队列，是一个"纯工具"模块，方便单独测试。
#
# 二、它在整个应用中的位置
#     A论（alun）是一个 FastAPI 文档处理应用：文档导入 -> 解析 -> 分块 -> 抽知识点。
#     本文件位于 app/ 包内，属于"导入阶段"的适配器：
#       浏览器/客户端
#         -> POST /api/v1/documents/from-url        （app/main.py 的路由）
#         -> url_import.fetch_url(url)              （本文件，同步网络抓取）
#         -> pipeline.create_document(...)          （落盘 + 入库）
#         -> pipeline.start_parse(...)              （启动后台解析任务）
#
# 三、主要调用关系
#     app/main.py 的 import_document_url() 在通过 documents:write 权限校验后，
#     用 asyncio.to_thread(url_import.fetch_url, url) 调用本模块。
#     为什么要 to_thread？因为 fetch_url 内部用的是同步 httpx，会阻塞线程；
#     放进线程池后，FastAPI 的事件循环仍能处理其它请求。
#     抓取返回的 dict 会被 pipeline.create_document 使用；
#     如果本模块抛 ValueError，main.py 会把它转成 HTTP 422、错误码 URL_IMPORT_FAILED。
#     另外 tests/test_smoke.py 会单独导入 _ArticleParser 做解析器单元测试。
#
# 四、fetch_url 的输入输出契约（重点）
#     输入：url: str，用户填的网址，可能带首尾空格。
#     输出：dict，键的含义如下：
#       kind       : "html" / "pdf" / "text"，标记内容类型，下游据此选择解析器；
#       content    : bytes。html 分支是 UTF-8 编码后的 Markdown；pdf 分支是 PDF 原始字节；
#                    text 分支是文本原始字节。统一用 bytes 是为了直接写文件/入库。
#       file_name  : 建议的文件名（html -> .md，pdf -> .pdf，text -> .txt）；
#       mime_type  : MIME 类型，供 storage/pipeline 判断；
#       title      : 文档标题，用于列表展示和后续解析；
#       source_uri : 跟随重定向后的最终网址，用于溯源（用户可能只记得短链接）。
#     失败：一律抛 ValueError，消息是可直接展示给用户的中文。
#     这样做的好处是本模块不依赖任何 Web 框架，测试时也不用启动服务。
#
# 五、安全背景（为什么校验代码看起来这么"啰嗦"）
#     网址是用户输入，属于不可信数据。服务端代用户发请求时，如果不管目标地址，
#     攻击者就能让服务器去访问 127.0.0.1、192.168.x.x、169.254.169.254（云元数据）
#     等内部资源，这类漏洞叫 SSRF（Server-Side Request Forgery，服务端请求伪造）。
#     _safe_public_url() 就是这道闸门：限制协议、拒绝本机域名、解析 DNS 后逐个检查 IP；
#     并且在跟随重定向之后，还会对最终地址再验一次，防止"公网 302 跳内网"的绕过。
#
# 六、建议的阅读顺序
#     常量 -> _ArticleParser（纯内存的 HTML 状态机）-> _safe_public_url（安全校验）
#     -> fetch_url（网络 IO 与三个内容分支）。
# =============================================================================
# 【本行作用】启用 PEP 563"延迟求值注解"：函数注解先以字符串形式保存，不在定义时求值。
# 【为什么】这样 list[str]、dict 这类写法在旧版本解释器里也不会报错，同时避免导入期
#           因类型互相引用而产生循环导入问题；这是项目统一的写法。
from __future__ import annotations

# 【导入说明】下面五个是标准库模块，不需要额外安装。
# ipaddress：把字符串转成 IP 对象，并提供 is_private / is_loopback 等判断方法。
import ipaddress
# re：正则表达式。本文件用它统计正文字符数、清洗文件名中的非法字符。
import re
# socket：做 DNS 解析（getaddrinfo），拿到域名背后所有 IPv4/IPv6 地址。
import socket
# HTMLParser：标准库自带的"事件驱动"HTML 解析器。它不建 DOM 树，而是边读边回调，
# 内存占用小、无第三方依赖，非常适合"抽取正文"这种单向处理。
from html.parser import HTMLParser
# urlparse：把 URL 拆成 scheme（协议）、hostname（主机）、port（端口）、path（路径）等部分。
from urllib.parse import urlparse

# httpx：第三方同步/异步 HTTP 客户端。选它是因为支持流式下载（iter_bytes）、
# 重定向控制、细粒度超时，且项目其它模块也在用，保持依赖一致。
import httpx

# 【常量】单次导入允许的最大字节数：12 * 1024 * 1024 = 12,582,912 字节（12 MiB）。
# 【为什么】网页可能非常大（或恶意构造超大响应），必须设上限防止内存和磁盘被打爆；
#           这个值同时用于 Content-Length 预检和流式下载过程中的累计检查。
MAX_BYTES = 12 * 1024 * 1024
# 【常量】"整块跳过"的标签集合：这些标签内部的文本不属于正文。
# script/style 是代码和样式；nav/header/footer/aside 是站点导航与页脚；
# form/button/select/option 是交互控件；noscript/svg/canvas/iframe 是替代内容或嵌入内容。
# 【为什么用 set】成员判断是 O(1)，而 list 是 O(n)；解析大页面时会被调用很多次。
# 注意：这里写成两行是格式化结果，第二行开头的空格只是对齐，不改变集合内容。
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "button",
             "select", "option", "noscript", "svg", "canvas", "iframe"}
# 【常量】"块级边界"标签集合：遇到它们说明上一段文字结束、下一段开始。
# 处理方式是先把缓冲区里的行内文字"冲刷"成一个独立段落，从而保留原文的段落结构；
# 其中 br 表示换行，也按分段处理（Markdown 里换行通常也意味着新段落）。
BLOCK_TAGS = {"p", "div", "section", "article", "main", "br", "ul", "ol", "blockquote", "pre"}


# 【类说明】_ArticleParser：把 HTML 转成 Markdown 的核心，继承标准库 HTMLParser。
# 【工作原理】调用 feed(html) 后，HTMLParser 会按文档顺序回调三个方法：
#   handle_starttag(开始标签) / handle_endtag(结束标签) / handle_data(标签之间的文本)。
#   我们在回调里维护一组状态字段，把文字按"块"收集到 self.blocks，最后 markdown() 拼接。
# 【为什么不用 BeautifulSoup/lxml】避免引入重依赖；而且"抽正文"只需要顺序扫描，不需要 DOM。
# 【命名】前缀下划线表示"模块内部使用"，但它并不是真正的私有，测试代码仍可直接导入。
class _ArticleParser(HTMLParser):
    # 【方法作用】构造函数：初始化解析器的全部状态。
    # 【为什么需要这么多字段】HTML 是流式的，解析到一半时必须记住"我现在在标题里？
    # 在表格第几层？当前段落收集了哪些文字？"，这些字段就是这台状态机的"内存"。
    def __init__(self):
        # 【本行作用】调用父类构造函数，并开启 convert_charrefs=True。
        # 【为什么】开启后 HTMLParser 会自动把 &amp;、&#39;、&nbsp; 等字符实体还原成
        #           真实字符，我们就不用手写 unescape 逻辑了。
        super().__init__(convert_charrefs=True)
        # 【字段】页面标题，来自 <title> 标签，最后用于生成一级标题和文件名。
        self.title = ""
        # 【字段】已完成的 Markdown 块列表，每一项是一个字符串（标题/段落/列表项/表格）。
        self.blocks: list[str] = []
        # 【字段】当前段落还没冲刷的行内文本片段缓冲区。
        self._buf: list[str] = []
        # 【字段】当前正在收集的块级标签名（如 h1、p、li），_flush 时据此决定前缀。
        self._tag = ""
        # 【字段】跳过深度计数器：大于 0 表示正处在 script/style 等要忽略的子树中。
        # 【为什么用计数而不是布尔值】因为忽略标签可能嵌套（如 nav 里还有 footer），
        #           只有对应的结束标签全部出现、计数归零，才真正回到正文。
        self._skip = 0
        # 【字段】是否处于 <title> 内部；是的话文本要累加到 self.title 而不是正文。
        self._title_mode = False
        # 【字段】表格嵌套深度；大于 0 表示"当前在表格里"，文本要进单元格而不是段落。
        self._table_depth = 0
        # 【字段】当前行已收集到的单元格文本列表。
        self._row: list[str] = []
        # 【字段】当前单元格的文本片段缓冲区（单元格内可能有多个文本节点）。
        self._cell: list[str] = []
        # 【字段】已经收完的表格行，结构是"行的列表，每行是单元格字符串的列表"。
        self._rows: list[list[str]] = []

    # 【方法作用】HTMLParser 回调：每遇到一个开始标签就调用一次。
    # 参数 tag 是标签名（原样大小写），attrs 是 [(属性名, 属性值), ...]，本文件用不到属性。
    def handle_starttag(self, tag, attrs):
        # 【本行作用】标签名统一转小写。
        # 【为什么】HTML 标签大小写不敏感，<DIV> 和 <div> 是同一个；统一后再比较才不会漏。
        tag = tag.lower()
        # 【分支】命中"忽略标签"：进入忽略区，深度 +1 后立刻返回，后续文本都不收集。
        if tag in SKIP_TAGS:
            # 【本行作用】跳过深度加一，表示"又进了一层要忽略的子树"。
            self._skip += 1
            # 【本行作用】直接结束本次回调，不再走下面的标题/段落/表格判断。
            return
        # 【分支】已经处在忽略区内（例如 script 里出现了 div）：什么都不做。
        # 【为什么】忽略是"整棵子树"级别的，内部任何标签都不应该影响正文。
        if self._skip:
            # 【本行作用】直接返回，相当于丢弃这个标签。
            return
        # 【分支】遇到 <title>：进入标题模式，后续 handle_data 会把文字写进 self.title。
        if tag == "title":
            # 【本行作用】打开标题模式开关；结束标签出现时再关掉。
            self._title_mode = True
        # 【分支】遇到 h1/h2/h3：标题必须独立成块，不能和上一段文字粘在一起。
        elif tag in ("h1", "h2", "h3"):
            # 【本行作用】先把缓冲区里上一段的文字冲刷出去，形成独立块。
            self._flush()
            # 【本行作用】记录当前标签名，_flush 时据此决定输出 "#"、"##" 还是 "###"。
            self._tag = tag
        # 【分支】遇到普通块级标签（p/div/section/br...）：同样先结束上一段。
        elif tag in BLOCK_TAGS:
            # 【本行作用】冲刷上一段文字。
            self._flush()
            # 【本行作用】记录块类型；普通块在 _flush 里不加任何前缀。
            self._tag = tag
        # 【分支】遇到列表项 <li>：列表项要单独成块并加 "- " 前缀。
        elif tag == "li":
            # 【本行作用】结束上一个列表项（或上一段）。
            self._flush()
            # 【本行作用】标记当前块是列表项，供 _flush 判断。
            self._tag = "li"
        # 【分支】遇到 <table>：表格开始。
        elif tag == "table":
            # 【本行作用】先把表格前面残留的段落文字冲刷掉。
            self._flush()
            # 【本行作用】表格深度 +1；大于 0 时 handle_data 会把文字放进单元格。
            self._table_depth += 1
            # 【本行作用】清空行缓存，开始收集这张新表格的行。
            self._rows = []
        # 【分支】在表格内部遇到 <tr>：新的一行开始。
        elif self._table_depth and tag == "tr":
            # 【本行作用】清空"当前行"的单元格列表，准备接收这一行的单元格。
            self._row = []
        # 【分支】在表格内部遇到 <td>/<th>：新的单元格开始。
        elif self._table_depth and tag in ("td", "th"):
            # 【本行作用】清空单元格缓冲区，准备接收这个单元格里的文字。
            self._cell = []
        # 【分支】在表格单元格内部遇到 <br>：希望用空格代替换行，避免单词粘连。
        # 【提示】由于 br 同时属于 BLOCK_TAGS，而上面的 BLOCK_TAGS 分支排在前面，
        #         这个分支实际上不会被执行（属于冗余但无害的代码）；阅读时了解意图即可。
        elif self._table_depth and tag == "br":
            # 【本行作用】往当前单元格追加一个空格，相当于"单元格内换行变空格"。
            self._cell.append(" ")

    # 【方法作用】HTMLParser 回调：每遇到一个结束标签就调用一次。
    # 这里的逻辑基本是 handle_starttag 的镜像：关掉对应的状态，并在合适的时机产出块。
    def handle_endtag(self, tag):
        # 【本行作用】同样先统一转小写，保证和开始标签的比较一致。
        tag = tag.lower()
        # 【分支】结束的是一个"忽略标签"：跳过深度减一。
        if tag in SKIP_TAGS:
            # 【本行作用】深度减一；用 max(0, ...) 兜底，防止不规范 HTML 多出结束标签
            # 把计数减成负数（负数会导致后续正文被误判为"仍在忽略区"而全部丢弃）。
            self._skip = max(0, self._skip - 1)
            # 【本行作用】结束本次回调，不再执行后面的正文处理逻辑。
            return
        # 【分支】仍处在更外层的忽略子树里（例如 script 结束后又回到 nav 内部）。
        if self._skip:
            # 【本行作用】直接返回，当前结束标签不产生任何输出。
            return
        # 【分支】</title>：关闭标题模式。
        # 【说明】标题文字在 handle_data 里已经累加完毕，这里只需要把开关关掉。
        if tag == "title":
            # 【本行作用】关闭"标题模式"，之后的文本重新走正文/表格逻辑。
            self._title_mode = False
        # 【分支】结束的是标题、普通块或列表项：说明一个段落收集完毕。
        elif tag in ("h1", "h2", "h3") or tag in BLOCK_TAGS or tag == "li":
            # 【本行作用】把缓冲区文字冲刷成一个 Markdown 块（标题加 #、列表加 -）。
            self._flush()
            # 【本行作用】清空当前块标签，避免标签结束后的零散文字被误判成标题/列表。
            self._tag = ""
        # 【分支】</td> 或 </th>：一个单元格结束。
        elif self._table_depth and tag in ("td", "th"):
            # 【本行作用】把单元格内的多个文本片段拼起来，再用 split/join 把所有连续空白
            # （换行、制表、多空格）折叠成单个空格，得到干净的单元格文本，追加到当前行。
            self._row.append(" ".join("".join(self._cell).split()))
            # 【本行作用】清空单元格缓冲区，为同一行的下一个单元格做准备。
            self._cell = []
        # 【分支】</tr>：一行结束。
        elif self._table_depth and tag == "tr":
            # 【条件】any(self._row) 表示"这一行至少有一个非空单元格"。
            # 【为什么】网页里常有纯布局用的空行（全是空白），放进 Markdown 表格会很难看，
            #           所以只有真正有内容才保留。
            if any(self._row):
                # 【本行作用】把这一行追加到表格的行集合中。
                self._rows.append(self._row)
            # 【本行作用】清空当前行缓存，准备下一行。
            self._row = []
        # 【分支】</table>：整张表格结束，把它渲染成 Markdown 表格。
        elif self._table_depth and tag == "table":
            # 【条件】只有收集到行才需要输出，空表格直接忽略。
            if self._rows:
                # 【本行作用】取所有行里最多的单元格数量，作为表格列数。
                # 【为什么】Markdown 表格要求每一行列数一致，否则渲染会错位。
                width = max(len(r) for r in self._rows)
                # 【本行作用】给不够长的行补空字符串，使每行都恰好 width 列。
                rows = [r + [""] * (width - len(r)) for r in self._rows]
                # 【本行作用】构造 Markdown 表格的前两行：
                # 第一行是表头（这里简单地把原始第一行当表头），
                # 第二行是分隔行 "| --- | --- |"，Markdown 靠它识别"这是表格"。
                md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
                # 【本行作用】把剩余的数据行逐行拼成 "| 单元格 | 单元格 |" 的形式，追加到 md。
                md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                # 【本行作用】用换行把整张表拼成一个字符串，作为"一个块"放进 blocks。
                self.blocks.append("\n".join(md))
            # 【本行作用】表格深度减一；max(0, ...) 同样是容错，防止不闭合标签导致负数。
            self._table_depth = max(0, self._table_depth - 1)
            # 【本行作用】清空行缓存，避免影响下一张表格。
            self._rows = []

    # 【方法作用】HTMLParser 回调：处理标签之间的文本内容。
    # 同一个文本节点可能被分成多次回调（例如遇到实体、注释），所以这里都是"追加"而不是"赋值"。
    def handle_data(self, data):
        # 【分支】处在忽略区内（script/style 等）：文本直接丢弃。
        if self._skip:
            # 【本行作用】直接返回，不收集任何字符。
            return
        # 【分支】处在 <title> 内：标题文字单独累加，不进入正文。
        if self._title_mode:
            # 【本行作用】把这段文字追加到标题；多次追加可覆盖 <title> 内嵌套标签的情况。
            self.title += data
            # 【本行作用】标题已处理完，结束本次回调，避免同一段文字又被当成正文。
            return
        # 【分支】处在表格内：文字属于某个单元格，原样暂存（空白等结束标签时统一折叠）。
        if self._table_depth:
            # 【本行作用】把这段文字追加进当前单元格缓冲区。
            self._cell.append(data)
            # 【本行作用】结束回调，避免表格文字同时进入正文段落。
            return
        # 【本行作用】把这段文本里的所有连续空白（空格、换行、制表符）折叠成单个空格，
        # 并去掉首尾空白。HTML 源码里的缩进和换行只是排版，不应影响正文。
        text = " ".join(data.split())
        # 【条件】折叠后还有内容才收集；纯空白文本节点没有任何意义。
        if text:
            # 【本行作用】把清理后的文字追加到当前段落缓冲区。
            self._buf.append(text)

    # 【方法作用】把缓冲区里的行内文字"冲刷"成一个独立的 Markdown 块。
    # 【什么时候调用】遇到新的块级标签、块级标签结束、以及最终生成 Markdown 之前。
    # 【为什么需要它】HTML 的文本是碎片的，必须先攒起来；只有遇到块边界时才知道
    #                 这一段完整文字属于标题、列表项还是普通段落。
    def _flush(self):
        # 【本行作用】先用空格把所有片段拼起来，再整体折叠一次空白并去掉首尾空格。
        # 【为什么再折叠一次】片段之间可能各自带空格，拼接后会出现连续空格。
        text = " ".join(" ".join(self._buf).split())
        # 【本行作用】清空缓冲区。
        # 【为什么放在最前面】无论后面是否真的输出，都不能让同一段文字被重复冲刷。
        self._buf = []
        # 【条件】没有有效文字就直接返回（例如连续的 <div><div> 不应该产生空段落）。
        if not text:
            # 【本行作用】结束方法，不产生块。
            return
        # 【分支】当前块是 h1/h2/h3：输出 Markdown 标题。
        if self._tag in ("h1", "h2", "h3"):
            # 【本行作用】用字典把标签映射成 # 的数量；查表比 if/elif 更简洁。
            level = {"h1": "#", "h2": "##", "h3": "###"}[self._tag]
            # 【本行作用】拼成 "# 标题" 形式，追加为一个块。
            self.blocks.append(f"{level} {text}")
        # 【分支】当前块是列表项：输出无序列表语法。
        elif self._tag == "li":
            # 【本行作用】加 "- " 前缀，Markdown 会把它渲染成圆点列表。
            self.blocks.append(f"- {text}")
        # 【分支】其它情况（p/div/br/pre 等）：按普通段落输出。
        else:
            # 【本行作用】直接保存纯文本；块与块之间稍后会用空行分隔。
            self.blocks.append(text)

    # 【方法作用】解析结束后调用，返回整篇文档的 Markdown 字符串。
    # 【返回值】形如 "# 标题\n\n正文第一段\n\n正文第二段"；没有标题时只返回正文。
    def markdown(self) -> str:
        # 【本行作用】最后再冲刷一次，确保文档末尾没有残留的缓冲区文字被丢掉。
        self._flush()
        # 【条件】表格标签没有闭合（很多网页 HTML 并不严谨）时，_table_depth 仍大于 0。
        # 【为什么】如果不处理，最后一张表的文字会因为"还在单元格模式"而永远不输出。
        if self._table_depth and self._rows:
            # 容错：表格未闭合时也输出
            # 【本行作用】同样取最大列数，保证每行列数一致。
            width = max(len(r) for r in self._rows)
            # 【本行作用】给短行补空单元格，凑齐 width 列。
            rows = [r + [""] * (width - len(r)) for r in self._rows]
            # 【本行作用】直接拼成 Markdown 表格行。
            # 【说明】这种"未闭合"的容错路径没有生成 "---" 分隔行，因此严格来说
            #         不一定是合法表格；原实现如此，这里只做注释、不改变行为。
            self.blocks.append("\n".join("| " + " | ".join(r) + " |" for r in rows))
        # 【本行作用】取标题并去掉首尾空白（<title> 里常带换行和缩进）。
        title = self.title.strip()
        # 【本行作用】把所有块用"两个换行"连接起来，并过滤掉全是空白的块。
        # 【为什么是两个换行】Markdown 里空行才表示段落/块的结束。
        body = "\n\n".join(b for b in self.blocks if b.strip())
        # 【返回值】有标题就在最前面加 "# 标题\n\n"（一级标题），否则只返回正文。
        return (f"# {title}\n\n" if title else "") + body


# 【函数作用】校验并规范化用户提交的网址，确保它指向"公网上的 http/https 页面"。
# 【参数】url: str，原始输入，可能带首尾空格。
# 【返回值】str：重新拼出的规范化 URL（保留路径与查询参数），供 httpx 请求使用。
# 【异常】任何不合规都抛 ValueError，消息可直接展示给用户。
# 【安全意义】这是 SSRF 防护的第一道也是最重要的一道闸门。
def _safe_public_url(url: str) -> str:
    # 【本行作用】去掉首尾空格后解析 URL。
    # 【说明】urlparse 只做字符串拆分，不会发起任何网络请求，所以可以放心地对不可信输入调用。
    parsed = urlparse(url.strip())
    # 【条件】协议必须是 http 或 https，且必须能解析出主机名。
    # 【为什么】阻止 file://（读本地文件）、ftp://、gopher://、javascript: 等危险协议；
    #           hostname 为空则说明是 "http:///path" 这类畸形地址，同样拒绝。
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        # 【本行作用】抛出中文错误；main.py 会把它转成 HTTP 422。
        raise ValueError("只支持 http/https 网页链接")
    # 【本行作用】取出主机名（不含端口），例如 "example.com" 或 "127.0.0.1"。
    host = parsed.hostname
    # 【条件】显式拒绝 localhost 及其常见别名。
    # 【为什么】即使后面的 DNS 检查能覆盖 127.0.0.1，这里先挡一次可以给出更明确的提示，
    #           也能防住某些系统把 localhost 解析到 ::1 等特殊情况。
    if host.lower() in ("localhost", "localhost.localdomain"):
        # 【本行作用】拒绝访问本机，避免用户借服务端访问本机管理端口。
        raise ValueError("不允许导入本机地址")
    # 【try 说明】DNS 解析可能失败（域名不存在、断网），需要单独捕获 socket.gaierror；
    #             注意：try 内部的 ValueError 不会被下面的 except 捕获（类型不匹配），
    #             所以"内网地址"的报错会原样向上抛，行为是正确的。
    try:
        # 【本行作用】向系统解析器查询该域名对应的全部地址。
        # 【参数】第二个参数是端口：优先用 URL 里的端口，没有则按协议取默认 443/80；
        #         type=SOCK_STREAM 表示查的是 TCP 地址（不是 UDP）。
        # 【返回值】列表，每项形如 (family, type, proto, canonname, sockaddr)，
        #           IPv4 的 sockaddr 是 (ip, port)，IPv6 是 (ip, port, flowinfo, scopeid)。
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        # 【循环】逐个检查解析出来的 IP：一个域名可能同时有 A 记录和 AAAA 记录，
        #        必须每一个都安全，否则攻击者可以用"一个公网 IP + 一个内网 IP"绕过。
        for info in infos:
            # 【本行作用】从 sockaddr 的第 0 位取出 IP 字符串，转成 ipaddress 对象。
            ip = ipaddress.ip_address(info[4][0])
            # 【条件】只要命中任意一种"非公网"属性就拒绝：
            #   is_private    私有地址（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、fc00::/7）；
            #   is_loopback   回环地址（127.0.0.0/8、::1）；
            #   is_link_local 链路本地（169.254.0.0/16、fe80::/10），云厂商元数据 169.254.169.254 就在其中；
            #   is_reserved   保留地址（如 240.0.0.0/4）；
            #   is_multicast  组播地址。
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                # 【本行作用】拒绝内网/保留地址，防止 SSRF 探测内网服务。
                raise ValueError("不允许导入内网或保留地址")
    # 【异常处理】只捕获 DNS 解析失败；其它异常（包括上面主动抛的 ValueError）继续向上传播。
    except socket.gaierror as exc:
        # 【本行作用】把底层英文异常包装成用户能理解的中文提示。
        # 【from exc】保留原始异常链（__cause__），排查日志时能看到真正原因。
        raise ValueError("无法解析该网址") from exc
    # 【返回值】用 geturl() 重新拼出规范化后的 URL。
    # 【为什么不用原始 url】这样能去掉首尾空格，并保证 scheme/host 的大小写等形式统一。
    return parsed.geturl()


# 【函数作用】抓取一个网址，返回统一的 dict（内容 + 元信息），供 pipeline 入库。
# 【参数】url: str，用户输入的网址。
# 【返回值】dict，键为 kind/content/file_name/mime_type/title/source_uri，见文件头说明。
# 【异常】所有失败都转成 ValueError（中文消息），由调用方映射为 HTTP 422。
# 【线程模型】内部是同步阻塞 IO，main.py 用 asyncio.to_thread 把它放进线程池执行，
#             因此这里不需要 async/await，也不要在这里做 CPU 密集的长时间计算。
def fetch_url(url: str) -> dict:
    """抓取网页或 PDF，返回 markdown/二进制与来源信息。"""
    # 【本行作用】先做安全校验并拿到规范化 URL；不合法会在这里直接抛 ValueError。
    safe = _safe_public_url(url)
    # 【本行作用】构造请求头字典。
    # 【为什么自定义 UA】很多站点会拒绝空 UA 或默认的 python-httpx UA；这里声明身份和用途，
    #                  "A-Lun/1.0" 是应用名与版本，"document import" 说明用途，便于站长识别。
    headers = {
        "User-Agent": "A-Lun/1.0 (document import; +https://localhost)",
        # 【Accept】告诉服务器我们接受哪些类型，q 是偏好权重（1.0 最高，0.9 次之，0.5 最低）。
        # 优先 HTML/XHTML 与 PDF，其次纯文本，最后是任意类型；服务器可据此做内容协商。
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    }
    # 【try 说明】从这里开始是真正的网络操作，httpx 的异常统一在下面转成 ValueError。
    try:
        # 【本行作用】发起流式 GET 请求，并用 with 管理连接（退出时自动关闭、释放连接池）。
        # 【参数】follow_redirects=True 自动跟随 3xx 跳转（最多默认 20 次）；
        #         timeout=20 表示连接/读取/写入/连接池各阶段各自的超时是 20 秒。
        with httpx.stream("GET", safe, headers=headers, follow_redirects=True, timeout=20) as resp:
            # 【条件】HTTP 状态码 >= 400 视为失败（4xx 客户端错误、5xx 服务端错误）。
            # 【为什么】这些响应通常是错误页，继续解析只会得到无意义正文。
            if resp.status_code >= 400:
                # 【本行作用】把状态码带进错误消息，方便用户判断是 404 还是 403。
                raise ValueError(f"网页返回 HTTP {resp.status_code}")
            # 【本行作用】读取 Content-Length 响应头，转成整数；缺失时用 0 兜底。
            # 【说明】这个值可能不存在（分块传输）或被服务器写错，所以它只用于"提前拒绝"。
            length = int(resp.headers.get("content-length") or 0)
            # 【条件】声明的大小已经超限，不必下载，直接失败，节省带宽和时间。
            if length > MAX_BYTES:
                # 【本行作用】提示上限（消息里写 12 MB，实际是 12 MiB）。
                raise ValueError("网页内容超过 12 MB")
            # 【本行作用】准备一个列表收集下载到的字节块。
            chunks = []
            # 【本行作用】累计已下载字节数，用于真正的上限判断。
            total = 0
            # 【循环】iter_bytes() 按块（chunk）产出响应体，避免一次性把大文件读进内存。
            # 【重要】它产出的是"已解压"的字节（httpx 会自动处理 gzip/deflate/br），
            #         所以 total 统计的是解压后大小，能顺带防住"压缩炸弹"。
            for chunk in resp.iter_bytes():
                # 【本行作用】累加当前块的长度。
                total += len(chunk)
                # 【条件】实际下载量超过上限：立刻抛错。
                # 【为什么还要判断】Content-Length 可能缺失或造假，必须以实际字节为准。
                if total > MAX_BYTES:
                    # 【本行作用】抛出超限错误；with 块退出时会关闭连接，停止继续下载。
                    raise ValueError("网页内容超过 12 MB")
                # 【本行作用】把这一块字节追加到列表（列表追加比 bytes 反复拼接更高效）。
                chunks.append(chunk)
            # 【本行作用】把所有块合并成完整的 bytes 对象。
            # 【为什么用 b"".join】一次性分配最终内存并拷贝，复杂度 O(n)，避免 bytes += 的 O(n²)。
            content = b"".join(chunks)
            # 【本行作用】拿到跟随重定向后的最终 URL。
            # 【为什么需要】用户可能提交短链接；同时它是"二次安全检查"和"溯源"的依据。
            final_url = str(resp.url)
            # 【本行作用】读取 Content-Type 并转小写，方便后面用 in 做包含判断（大小写不敏感）。
            # 【说明】服务器可能不返回该头，此时为空字符串，后面会走"按扩展名/按 HTML"的分支。
            content_type = (resp.headers.get("content-type") or "").lower()
            # 【本行作用】确定解码网页用的字符集：优先用响应头 charset，缺失时默认 utf-8。
            # 【说明】resp.encoding 在响应头没有 charset 时可能为 None，所以要有 or "utf-8"。
            encoding = resp.encoding or "utf-8"
    # 【异常处理】捕获 httpx 的所有网络层异常：连接失败、DNS 错误、超时、协议错误等。
    # 【为什么统一处理】对用户来说这些都是"抓取失败"，没必要暴露底层异常类型；
    #                 但用 from exc 保留原因，方便开发者查日志。
    except httpx.HTTPError as exc:
        # 【本行作用】包装成中文 ValueError 并向上抛出。
        raise ValueError(f"网页抓取失败：{exc}") from exc
    # 【本行作用】对"重定向后的最终地址"再做一次安全校验。
    # 【为什么必须做】攻击者可让公网 URL 302 跳转到 http://127.0.0.1/...；
    #                 只在入口校验会被绕过，所以最终地址必须重新过一遍闸门。
    _safe_public_url(final_url)

    # 【本行作用】从最终 URL 的路径里推断文件扩展名：
    #   path.rsplit(".", 1)[-1] 取最后一个点之后的文本；路径中没有点则为空串；最后转小写。
    # 【说明】这里连续调用了两次 urlparse，属于可读性上的小冗余，但行为没有问题。
    suffix = (urlparse(final_url).path.rsplit(".", 1)[-1] if "." in urlparse(final_url).path else "").lower()
    # 【分支】PDF 判定：Content-Type 含 application/pdf，或路径以 .pdf 结尾。
    # 【为什么两个条件都要】很多下载接口返回 application/octet-stream，只能靠扩展名判断；
    #                       也有些 URL 没有 .pdf 后缀但响应头正确。
    if "application/pdf" in content_type or suffix == "pdf":
        # 【本行作用】取路径最后一段作为文件名（如 /files/paper.pdf -> paper.pdf）；
        #             为空则兜底 "web-document.pdf"。
        name = (urlparse(final_url).path.rsplit("/", 1)[-1] or "web-document.pdf")
        # 【条件】文件名不是以 .pdf 结尾（例如 /download?id=1 或 /file.pdf/）。
        if not name.lower().endswith(".pdf"):
            # 【本行作用】补上 .pdf 后缀，保证下游解析器能按扩展名识别。
            name += ".pdf"
        # 【返回值】PDF 分支的结果字典。
        # 【字段说明】content 是 PDF 原始字节，pipeline 会把它写入存储后再解析；
        #             title 去掉 ".pdf" 后缀作为展示标题。
        return {"kind": "pdf", "content": content, "file_name": name,
                "mime_type": "application/pdf", "title": name.rsplit(".", 1)[0], "source_uri": final_url}
    # 【分支】纯文本：Content-Type 含 text/plain。
    # 【说明】这里固定用 utf-8 解码并 errors="replace"，即使站点声明的编码不准确也不会崩；
    #         返回的 content 仍是原始字节，下游按 mime_type 处理。
    if "text/plain" in content_type:
        # 【本行作用】把字节解码成字符串（本函数内并未使用 text，保留是为了与原实现一致，
        # 也便于调试时查看内容）；errors="replace" 会把非法字节替换成 �。
        text = content.decode("utf-8", errors="replace")
        # 【返回值】文本分支：固定文件名 web-document.txt，标题为"网页文本"。
        return {"kind": "text", "content": content, "file_name": "web-document.txt",
                "mime_type": "text/plain", "title": "网页文本", "source_uri": final_url}
    # 【本行作用】其余情况一律按 HTML 处理：用前面确定的编码解码，非法字节用 � 替换。
    # 【为什么用 errors="replace"】网页编码经常标错（尤其老中文站点），宁可少量乱码也不能抛异常。
    html = content.decode(encoding, errors="replace")
    # 【本行作用】创建解析器实例；每次请求都新建，避免不同页面之间状态串味。
    parser = _ArticleParser()
    # 【try 说明】解析属于"尽力而为"的步骤，任何异常都转成统一的用户提示。
    try:
        # 【本行作用】把整段 HTML 喂给解析器，触发一系列 start/end/data 回调。
        parser.feed(html)
        # 【本行作用】生成 Markdown 并去掉首尾空白。
        markdown = parser.markdown().strip()
    # 【异常处理】捕获解析过程中的任何异常（HTMLParser 本身很宽容，但自定义回调可能出错）。
    except Exception as exc:
        # 【本行作用】转成中文 ValueError，并用 from exc 保留原始堆栈。
        raise ValueError("网页正文解析失败") from exc
    # 【条件】去掉所有空白字符后，正文不足 80 个字符，就认为"没抓到正文"。
    # 【为什么】常见于登录墙、JavaScript 动态渲染、反爬提示页；与其入库一篇空文档，
    #           不如直接告诉用户原因。
    if len(re.sub(r"\s", "", markdown)) < 80:
        # 【本行作用】给出可操作的中文提示。
        raise ValueError("没有提取到足够的网页正文，可能需要登录或页面由 JavaScript 渲染")
    # 【本行作用】确定标题：优先 <title> 文本；为空则用域名；再兜底"网页文档"。
    title = parser.title.strip() or (urlparse(final_url).hostname or "网页文档")
    # 【本行作用】清洗文件名：把 Windows/macOS 不允许的字符 \ / : * ? " < > | 替换成 "-"，
    # 并截断到 80 个字符，避免文件名过长或包含路径分隔符；结果为空则兜底 web-document。
    safe_name = re.sub(r'[\\/:*?"<>|]+', "-", title)[:80] or "web-document"
    # 【返回值】HTML 分支：content 是 Markdown 的 UTF-8 字节（统一 bytes 契约），
    # file_name 以 .md 结尾，mime_type 为 text/markdown，source_uri 为最终网址。
    return {"kind": "html", "content": markdown.encode("utf-8"), "file_name": safe_name + ".md",
            "mime_type": "text/markdown", "title": title, "source_uri": final_url}
# =============================================================================
# 读完后可以这样回顾整条链路：
#   用户提交 URL
#     -> _safe_public_url() 校验协议/主机/DNS，挡掉本机与内网地址（SSRF 防护）
#     -> httpx 流式下载，双重大小限制（Content-Length + 实际累计）
#     -> 重定向后的最终地址再校验一次
#     -> 按 Content-Type/扩展名分流：PDF 原始字节 / 纯文本 / HTML
#     -> HTML 交给 _ArticleParser 变成 Markdown，校验正文长度、生成安全文件名
#     -> 返回统一 dict，由 main.py 交给 pipeline.create_document 入库
# 本文件没有任何全局可变状态，所有解析状态都封装在 _ArticleParser 实例里，
# 因此可以在多线程中并发调用 fetch_url（每次调用都有独立的本地变量和解析器）。
# =============================================================================
