# A论 V1.2 教学闭环材料

| 文档/截图 | 内容 |
|---|---|
| [01-V1.2-教学闭环验收](01-V1.2-教学闭环验收.md) | 教师布置→学生作答→教师复盘完整记录、追问/阅读器/结构版本验收 |
| [02-阅读器图片粘贴](02-阅读器图片粘贴.md) | 阅读器输入框粘贴/上传截图修复 |
| [03-划词自动解读](03-划词自动解读.md) | 选中原文自动 AI 解读，PDF 划词模式 |
| [04-回答定位提问点](04-回答定位提问点.md) | 回答完成后滚动到本次提问位置 |
| [05-停止当前回答](05-停止当前回答.md) | 回答生成中可停止当前请求 |
| `../screenshots/26-stop-answer.png` | 停止当前回答后的状态与消息 |
| `../screenshots/25-answer-scroll.png` | 回答完成后提问位于消息区顶部，回答从开头展示 |
| `../screenshots/24-pdf-faithful-layer.png` | PDF 划词模式：原文页图片 + 透明文本层，公式版式保持一致 |
| `../screenshots/12-teacher.png` | 教师教学闭环控制台 |
| `../screenshots/13-teacher-review.png` | 教师复盘：作答明细、报告、评语 |
| `../screenshots/14-student-assignments.png` | 学生首页「老师布置的题卷」 |
| `../screenshots/15-reader.png` | 阅读器页码同步 |
| `../screenshots/15-reader-notes.png` | 阅读器页级笔记 |
| `../screenshots/20-reader-sticky.png` | 阅读器固定 AI 解读栏（左文滚动、右侧不动） |
| `../screenshots/16-versions.png` | 文档结构版本与回滚 |
| `../screenshots/17-student-report.png` | 学生报告中的教师复盘 |

复现入口：

- 教师：`http://127.0.0.1:8010/teacher`，`teacher / teacher123`
- 学生：`http://127.0.0.1:8010/`，`demo / demo123`
- 自动演示：`python -m alun.scripts.teaching_demo --doc-id doc_37b9cbf889`
