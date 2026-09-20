# 风格提取流水线改造验收

日期：2026-09-20。目标版本：workflow 14。

## 已实现

- 取消 8 段上限；按约 10,000 输入 token 预算分批。文档独立，较大批次优先沿章节边界切分，短章节可合批。
- 文档内最多 24 个均匀分布的正文样本用于风格观察；术语、人名仍检查全部目标语言文本。
- 保守清理 PDF 块内英文断行与重复边缘短行，长块切片保留原段落编号；不修改原文件。
- 每批提取短观察、可校验的引用、术语和人名。统一汇总最多 12 条规则，超大观察集采用保留证据链的分层汇总。
- 文档预处理、已成功批次、汇总结果持久化缓存；内容、目标语言、Agent、模型选择、版本参与缓存键。失败重试复用已成功步骤。
- 提取独立调用 CLI，不恢复或替换项目对话会话。
- `requirements.md` 单独管理人工要求及历史。保留旧人工版本，自动指南依当前语料重新生成。直接编辑指南会将完整编辑结果保存为用户要求，界面明示。
- 风格、术语、人名及完成标记统一校验和保存，普通写入失败回滚；同时保护并发编辑。
- 提取成功与 RAG 成功分别记录；有效索引复用，索引失败仅需重试索引。
- 进度展示计划批次、复用量、文档名和执行阶段；任务目录保存详细计划及证据。

## 批次数检查

使用 1,600 个约 54 字符的英文短段落：旧的 8 段限制至少 200 批，新规划器为 13 批。原文覆盖完整，风格样本涵盖首尾，共 24 段。该结果只表示合成输入的批次数变化，不代表真实文档速度提升倍数。

## 验证结果

- `tests/test_style_pipeline.py` 原 7 项 + `tests/test_corpus_style_status.py` 4 项：11 passed。
- 指南语言、术语、语言流水线、历史相关针对性回归：34 passed，6 deselected。
- 新增用户要求 API、旧人工要求迁移、无效证据阻止发布：3 passed。
- 真实子进程模拟的独立会话测试：1 passed；确认两次调用均不恢复会话，原项目会话指针保留。
- 并发人工编辑保护补测：1 passed。
- 最终 `tests/test_style_pipeline.py` 全部 11 项通过，包含新增的超长中文切分预算及完整覆盖检查。综合上述不同测试文件/用例，共 51 个针对性用例通过，并非一次完整测试套件验收。
- Ruff、JavaScript 语法检查、HTML ID 唯一性与新增编辑控件检查通过。

当前沙箱下异步线程返回曾出现等待不结束。上述逻辑回归使用临时 pytest 适配器，将 `Worker.blocking` 中的本地计算直接执行，不改变生产代码；因此这些测试不替代完整线程调度/取消行为验收。独立会话子进程测试没有使用该适配器。环境切换前原 7 项流水线测试曾在正常线程模式下通过；后续改动以当前针对性测试为准。

## 首次验收的环境限制（后续补验见下）

- Codex 真实调用：`build/style-codex-jzt7k1nb`，连接被拒绝，任务失败。
- CodeBuddy 真实调用：`build/style-codebuddy-wf2f9m35`，任务超时。
- 浏览器：Chrome 启动时 `setsockopt: Operation not permitted`，未完成新版端到端验证。
- 当前沙箱无法访问 LAN 服务（`Operation not permitted`），也看不到部署进程。未重启生产服务，不能宣称新版已上线。

恢复允许的运行环境后可执行：

```bash
rtk proxy env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -p pytest_asyncio.plugin -q
rtk proxy env PYTHONPATH=. python3 tests/browser_smoke.py
rtk proxy env PYTHONPATH=. TRANSMUX_AGENT_TIMEOUT=300 python3 scripts/test_live_style.py codex --model gpt-5.6-luna
rtk proxy env PYTHONPATH=. TRANSMUX_AGENT_TIMEOUT=300 python3 scripts/test_live_style.py codebuddy --model hy3
rtk proxy python3 scripts/deploy_when_idle.py
```

真实调用脚本使用隔离的合成语料，验证重复提取零新增 Agent 调用、用户要求保留、项目对话会话不变。脚本使用测试向量，不验证真实 embedding 质量。部署脚本等待运行任务结束后再升级。

## 限制

风格抽样可能漏掉罕见表达模式；证据存在检查不等于语义概括绝对正确。语言识别、token 预算和 PDF 清理均为保守启发式。删除语料会移除其风格观察，但不会自动删除已有术语、人名或历史。缓存暂不自动回收，CLI 默认模型在应用外更改时需要显式选择模型来切换缓存范围。文件回滚不承诺进程崩溃时的跨文件原子性。


## 2026-09-20 恢复正常环境后的补验

- Codex `gpt-5.6-luna` 真实调用通过：`build/style-codex-e2lnrz5g/acceptance.json`。
- CodeBuddy `hy3` 真实调用通过：`build/style-codebuddy-enx495i2/acceptance.json`。
- 两者首次合成语料提取均为 2 次调用，重复执行为 0 次新增调用；原项目会话指针保留。
- `tests/browser_smoke.py` 通过：创建、自动保存、上传、RAG、召回、翻译、下载、对话与移动端，无 JavaScript 错误。
- `tests/test_agents.py` 共 10 项通过，包括新增的 CodeBuddy 流式上下文错误提前终止测试。CLI 即使未输出最终 result，也不会持续等待相同超限错误。
- 此前线上为 v13；实际失败任务 `39eb1a73b83c4833848dc8c77bf61e50` 仍使用 139 批和已超限会话。已通过取消接口停止该任务，保留文件和历史，准备升级后重新提交。

- 完整回归（正常线程模式）：159 passed，297.83 秒；新增提前终止场景另外包含在 10 项 Agent 测试中通过。
- 已部署 workflow 14，服务 PID 2163693；LAN 与 Tailscale `/api/health` 均返回 200 / v14。
- 已重新提交实际项目的风格提取：`755a8788d4da4215872ef355cd2e53c1`。原失败/取消任务及记录保留。
