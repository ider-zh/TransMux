# RAG 召回长时间等待修复

2026-09-20，workflow 15。

任务 d879d1a8c9ba470db136b720b29ea55a 停在 recalling，没有生成 batch 输入，也没有进入 Agent。服务器日志记录 Hugging Face 配置和 tokenizer 文件 HEAD 请求不断出现 Network is unreachable 并重试，错误未抛出到任务层，因此页面没有失败结果。

改为解析并加载本地缓存快照，默认离线运行，不在任务内隐式下载。缓存缺失明确报错，向量模型锁等待限制 120 秒，页面阶段超过 60 秒显示等待提示（不误称任务已失败）。

验证：10 项 embedding/语言流水线测试通过，浏览器等待提示显示与清除检查通过，真实离线向量计算成功。Ruff、JavaScript 语法通过。

取消旧任务并保持原 file_id、RAG 开关和审校上限重提：b8d15784163a4c3383574ce162fbe77e。实际召回用时 13.22 秒，已进入翻译阶段。LAN 与 Tailnet 健康接口均为 v15。
