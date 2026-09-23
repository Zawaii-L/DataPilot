DataPilot 项目交接记录

更新时间：2026-09-22

1. 项目基本信息

项目名：DataPilot

本地路径：F:\DataPilot

系统：Windows

GUI：PySide6

Python：venv

当前开发版本：v6.6

已封版版本：v6.5

当前主模型：本地 Qwen3.5-9B（Ollama）

本地模型名：datapilot-qwen:9b

Ollama Base URL：http://127.0.0.1:11434/v1

模型路由：local-first，手动切换 local/cloud，不自动 local→cloud fallback

2. 用户开发规则

版本号只使用一位小数，例如 v6.5 → v6.6 → v6.7。

未明确封版前，不要擅自升到下一版本。

修改已有 .py 文件时，提供完整替换文件，不要只给代码片段。

新文件必须先说明完整 Windows 路径。

尽量少新增文件。

每一步必须附带可直接运行的测试命令。

优先 pytest / py_compile，再进行一次 GUI/API 实测。

避免终端刷屏。

不要反复确认；下一步明确时直接推进。

注意项目重构后的 canonical 实现位置：优先 core\、tools\、verification\，根目录可能只是兼容 wrapper。

3. 当前主要架构

用户输入目标
↓
GUI
↓
Clarification Gate
↓
Model Router
↓
Workspace + Runtime Context
↓
Task Planner → TaskPlan + Evidence Contract
↓
Stage Router
↓
Skill Selector
↓
AgentLoop
↓
Tool Registry / Executor
↓
Observation
↓
Stage / DataState
↓
Completion Gate / Verification Engine
↓
PASS 或 Completion Recovery

概念区分：

Stage：流程位置

Role：专业推理角色（尚未独立实现）

Skill：方法

Tool：执行函数

4. v6.5 状态

v6.5 已 tag/push：

b1d8a93 release: DataPilot v6.5 local-first dual-model agent

84759fb chore: clean DataPilot v6.5 release snapshot

tag：v6.5

当时封版测试：

126 passed, 2 warnings

5. v6.6 已完成的主要能力

Response-only / Web research

web_business_research Skill

response-only 不生成 Word/Excel

Research Coverage：

competition

policy_low_altitude

employment

education

enterprise_demand

Search Recovery：失败 query → 同主题替代 query

Topic-level Exhaustion / Degrade

Search Infrastructure Guard

Failed Source Recovery

Acquisition Gate

read_webpage 正文证据优先于搜索摘要

source appendix

bounded degradation

Evidence Contract

当前首批 Evidence Contract：

approximate numeric fidelity

conditional CAC calculation

external numeric grounding

forecast matrix completeness

required Top 3 action count

重点业务约束：

80+ 不能擅自改成 85

CAC 统计周期不一致时必须写“暂不能准确计算”

外部数字必须来自实际 read_webpage 正文证据

经营预测必须包含：

保守 / 基准 / 乐观

3 / 6 / 12 月

咨询人数 / 报名人数 / 收入 / 毛利

明确假设

用户要求 Top 3 时必须有清晰 3 项

6. 当前测试基线

最近一次全量基线在上一轮修改前已达到：

220 passed, 1 failed, 2 warnings

随后最后一个失败被判定为测试构造没有真正耗尽生产代码的全部 query variants，已提供新 test_v66_search_recovery.py，目标应为全绿。

此前稳定基线包括：

215 passed, 2 warnings
218 passed, 2 warnings（后续新增测试）

继续开发时，先以用户本地当前代码运行 python -m pytest -q 的实际结果为准。

两个长期 warning：

tests/integration/test_document_tools.py::test_folder_scan

tests/integration/test_document_tools.py::test_batch_inspection

均是测试函数 return list，不是当前功能阻塞项。

7. 当前无人机经营实战 Prompt

用户真实业务：

广东中山三乡镇附近无人机培训基地

80+ 咨询

14 报名

课程费约 8800 元/人

单学员毛利约 6900 元

抖音 + 微信视频号

营销费用约 1000 元，但统计周期可能不一致

最多约 40 名学员

2 名教员

当前瓶颈是招生，不是产能

第一轮只要分析，不要文件

8. 最近一次真实 GUI 运行的关键进展

最近一次运行已经明显推进：

competition 搜索成功

policy_low_altitude 搜索成功

employment 搜索成功

education 搜索失败后被正确 bounded recovery/degrade

enterprise_demand 搜索失败后被 bounded recovery/degrade

成功进入 read_webpage

已实际读取：

中山市人民政府低空经济行动方案

珠海市人民政府低空经济支持政策

广东省政府镜像的中山政策

中山市 2024-2027 低空经济方案

珠海市自然资源局低空经济政策解读

Acquisition 最终 PASS

成功进入 Processing

当前真正的新问题

在 Processing 中 Agent 请求 finish 后：

Completion Gate FAIL
↓
Stage Recovery → acquisition
↓
Acquisition 已经足够
↓
立即 Stage Advance → processing
↓
再次 finish
↓
Completion Gate FAIL
↓
再次 Stage Recovery → acquisition

形成循环，直到：

Completion Recovery Budget 用尽
↓
Final Acquisition Guard PASS
↓
最终 Completion Gate 仍 FAIL
↓
verification_failed

这说明：

Research 层已经基本跑通

当前主要问题不再是搜索

下一步应重点检查 Completion Gate / Evidence Contract 的失败项为什么被映射回 Acquisition

对 response-only 文本答案中的 Evidence Contract FAIL，通常应该在 Processing 修正最终答案，而不是重新回 Acquisition，除非失败项明确是“外部证据不足/来源缺失”

9. 下一步建议排查点

优先检查：

core\agent_loop.py

_stage_recovery_target_from_report(...)

_normalize_response_only_recovery_stage(...)

response-only finalizer / synthesis recovery

stage_orchestrator.py

StageOrchestrator.recovery_target(...)

verification\verification_engine.py

Evidence Contract 的失败 message/category 是否能区分：

evidence/source missing → Acquisition

final answer format/content contract fail → Processing

目标修复：

Evidence Contract 内容失败
→ Processing Recovery
→ 根据 verification_observation 重写最终答案
→ Completion Gate
→ PASS

而不是：

Evidence Contract 内容失败
→ Acquisition
→ Processing
→ 原答案再 finish
→ 无限循环

10. 新聊天开场词

新聊天请直接发：

继续开发我的 DataPilot。请先读取我上传的 DATAPILOT_HANDOFF.md，以它作为当前项目真实状态，不要从头规划。继续遵守里面的开发规则。当前版本是 v6.6。上一轮已经把无人机经营研究任务跑到 Acquisition PASS → Processing，但 Completion Gate FAIL 后错误回退到 Acquisition 并形成循环。下一步请优先定位 Evidence Contract / Completion Recovery 的 Stage Recovery 映射问题。需要改代码时给我完整可替换文件，并给可运行测试。

