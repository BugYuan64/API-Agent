"""API-Agent 全功能介绍与使用方法知识语料及确定性分块器。

用于 AI 助手小Y 的向量知识库构建与未构建向量时的词法回退检索。
每个知识片段包含稳定的 chunk_id、分类、标题、关键词与完整操作步骤。
"""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    """知识库最小检索单元。"""

    chunk_id: str
    doc_id: str
    category: str
    title: str
    section: str
    keywords: tuple[str, ...]
    content: str


_RAW_DOCUMENTS: tuple[dict, ...] = (
    {
        "doc_id": "overview",
        "category": "项目总览",
        "title": "API-Agent 项目总览与工作台布局",
        "sections": [
            {
                "section": "核心定位与页面结构",
                "keywords": ("API-Agent", "工作台", "导航", "侧边栏", "功能总览", "页面"),
                "content": (
                    "API-Agent 是本地单进程 API 调试、自动化测试与大模型能力探测工作台，"
                    "前端基于 Vue 3，后端基于 FastAPI + SQLite。左侧导航包含五大功能区：\n"
                    "1. 单点测试：包含「接口测试」（HTTP/HTTPS 与 SSE 实时调试）与"
                    "「长连接测试」（WebSocket 双向调试）。\n"
                    "2. 智能群测：导入接口文档或扫描本机源码生成契约文档，自动生成并"
                    "批量执行测试用例、支持有状态变量链与 AI 总结。\n"
                    "3. 大模型测试：面向 OpenAI 兼容、Anthropic Messages 与 Gemini 端点"
                    "拉取模型并探测可用性、工具调用、流式 TTFT、结构化输出、多模态与专用端点。\n"
                    "4. 执行结果（历史记录）：统一回看、筛选与清理 HTTP、WebSocket 及"
                    "智能群测历史记录。\n"
                    "5. 系统设置：配置工作台测试默认参数、亮暗主题与调色板配色、大模型服务、"
                    "AI 助手小Y 向量知识库以及测试执行引擎（native / postman MCP）。"
                ),
            },
            {
                "section": "侧边栏与界面交互",
                "keywords": ("侧边栏", "Logo", "折叠", "隐藏", "主题", "亮色", "暗色"),
                "content": (
                    "界面交互要点：\n"
                    "- 点击侧边栏顶部的动态信号动画项目 Logo 即可折叠或展开侧边栏；"
                    "鼠标悬停在 Logo 上时会在右侧气泡显示项目名称与操作提示。\n"
                    "- 侧边栏底部按钮可一键切换暗色/亮色主题，切换时保留当前页面输入与状态。\n"
                    "- 三大测试页（接口测试、长连接测试、智能群测）共用四分区可拖动布局："
                    "左侧历史栏、中间编辑区、右侧结果区与底部 AI 助手小Y 对话区，"
                    "拖动手柄可自由调整宽度和高度并自动保存到浏览器。"
                ),
            },
        ],
    },
    {
        "doc_id": "request-console",
        "category": "单点测试",
        "title": "接口测试（HTTP / HTTPS / SSE）使用方法",
        "sections": [
            {
                "section": "发送 HTTP 请求与参数编辑",
                "keywords": ("接口测试", "HTTP", "请求体", "JSON", "请求头", "URL前缀"),
                "content": (
                    "进入「接口测试」（/request）页面可进行单接口调试：\n"
                    "1. 选择 HTTP 方法（GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS），"
                    "填写目标地址（可用协议/前缀快捷补全，也支持点击示例芯片快速填充）。\n"
                    "2. 切换到「请求体」标签选择 none / JSON / Text / x-www-form-urlencoded；"
                    "JSON 模式支持一键美化格式化。GET/HEAD/OPTIONS 不携带请求体。\n"
                    "3. 切换到「请求头」标签按行增删请求头；Host、Content-Length、"
                    "Connection 等传输层保留头会被后端拦截；Authorization、Cookie、"
                    "Token 等敏感头在保存历史时自动脱敏为 [redacted]。\n"
                    "4. 点击「发送」后，右侧实时展示状态码、耗时、字节数、响应正文与响应头；"
                    "支持复制响应、查看完整详情弹窗与清除当前显示。"
                ),
            },
            {
                "section": "SSE 流式响应与重定向处理",
                "keywords": ("SSE", "流式", "重定向", "302", "跨源", "超时", "设置"),
                "content": (
                    "接口测试高级行为与排查：\n"
                    "- SSE 流式接口：后端通过 /api/console/requests/live 逐事件推送 SSE 帧，"
                    "结果区实时展示事件序号、事件名与数据，并标注结束原因"
                    "（target_closed / limits_exceeded / deadline_exceeded）。"
                    "可在「设置」标签调整单次超时、SSE 最大事件数与最长持续时间。\n"
                    "- 重定向行为：默认不跟随 3xx 跳转，直接展示原始 301/302 状态码与 "
                    "Location 提示；在「设置」中开启「跟随重定向」后会逐跳记录链路，"
                    "并在跨源跳转时自动剥离 Authorization、Cookie 与自定义头以防凭据泄漏。\n"
                    "- 历史回填：点击左侧「请求历史」可回填参数与上次响应预览"
                    "（已脱敏的凭据需重新填写后再发送）；点击「新建测试」开启空白草稿。"
                ),
            },
        ],
    },
    {
        "doc_id": "long-connection",
        "category": "单点测试",
        "title": "长连接测试（WebSocket）使用方法",
        "sections": [
            {
                "section": "建立连接与双向实时收发",
                "keywords": ("长连接", "WebSocket", "ws", "wss", "子协议", "双向发送"),
                "content": (
                    "进入「长连接测试」（/long-connection）页面可调试 WebSocket 服务：\n"
                    "1. 填写 ws:// 或 wss:// 地址（http/https 也会自动规范化为 ws/wss）。\n"
                    "2. 未连接时，消息输入框可填写连接成功后自动按间隔发送的预置消息"
                    "（每行一条）；连接建立后，同一输入框切换为实时发送模式，"
                    "按 Ctrl+Enter 或点击「发送」即可实时推帧。\n"
                    "3. 可展开「请求头」「子协议（逗号分隔）」「设置」配置握手请求头、"
                    "子协议、连接超时、心跳间隔、事件数上限与最长持续时间。\n"
                    "4. 右侧时间线实时区分「收/发」方向、文本/二进制帧、握手打开、"
                    "关闭码与错误帧；支持按全部/接收/发送筛选、全部折叠/展开、"
                    "导出时间线与查看握手详情。"
                ),
            },
            {
                "section": "连接终止原因与容量限制",
                "keywords": (
                    "close_code",
                    "handshake_rejected",
                    "limits_exceeded",
                    "STREAM_MAX_SESSIONS",
                    "STREAM_MAX_BYTES",
                ),
                "content": (
                    "WebSocket 会话生命周期与常见问题：\n"
                    "- 结束原因（stop_reason）明确区分：target_closed（对端主动关闭，"
                    "附带 close_code/close_reason）、client_stopped（用户手动停止）、"
                    "limits_exceeded（触发事件数或字节上限）、"
                    "deadline_exceeded（达到最大持续时长）。\n"
                    "- 若服务端拒绝握手（如 401/403/404），结果记录为 handshake_rejected "
                    "并保留 HTTP 状态码。\n"
                    "- 后端通过环境变量 STREAM_MAX_SESSIONS（默认 4）和 "
                    "STREAM_MAX_BYTES（默认 8MiB）保护本机连接与内存预算。"
                ),
            },
        ],
    },
    {
        "doc_id": "intelligent-tests",
        "category": "智能群测",
        "title": "智能群测（文档驱动批量测试）使用方法",
        "sections": [
            {
                "section": "导入文档、生成契约与编辑草稿",
                "keywords": (
                    "智能群测",
                    "OpenAPI",
                    "Swagger",
                    "Postman",
                    "生成接口文档",
                    "草稿",
                ),
                "content": (
                    "进入「智能群测」（/testcases）页面可完成从接口文档到批量验收的全流程：\n"
                    "1. 上传文档：支持 JSON、YAML、Markdown 与纯文本（最大 512 KiB），"
                    "本地自动识别标准 OpenAPI 3 / Swagger 2 / Postman Collection，"
                    "非标准文档可由已配置大模型解析。\n"
                    "2. 生成接口文档：点击「生成接口文档」可选择本机源码目录，"
                    "由生成模型分析脱敏源码并导出 OpenAPI 文档。\n"
                    "3. 审查与编辑草稿：识别完成后可展开接口列表，内联编辑路径"
                    "（支持 {{var}} 占位符）、方法、请求头、请求体、断言及变量提取规则"
                    "（extract），也可删除多余接口或勾选部分接口点击「重新生成 test」。"
                ),
            },
            {
                "section": "写操作护栏、执行进度与 AI 总结",
                "keywords": ("allowWrites", "写操作", "permission_required", "导出总结"),
                "content": (
                    "智能群测执行与安全护栏：\n"
                    "1. 填写「测试目标 URL」（文档中若含 server URL 会自动预填），"
                    "在「设置」中可配置每个接口生成的 test 数量、单流观察时长、"
                    "总时限以及「允许写操作」开关。\n"
                    "2. 默认关闭写操作权限：POST/PUT/PATCH/DELETE 用例默认标记为 "
                    "permission_required（write_permission_required）并跳过执行；"
                    "确认目标环境安全后，勾选允许写操作即可实际执行写用例。\n"
                    "3. 点击「开始测试」后，右侧实时流式更新用例结果树、通过/失败/跳过统计"
                    "与 SSE 实时事件；执行完毕自动流式生成中文 Markdown AI 总结，"
                    "支持「导出总结」下载报告或选中历史记录修改后点击「再次测试」。"
                ),
            },
        ],
    },
    {
        "doc_id": "stateful-chains",
        "category": "进阶测试",
        "title": "有状态多步骤依赖链（变量提取与传递）使用方法",
        "sections": [
            {
                "section": "变量定义、提取规则与占位符注入",
                "keywords": (
                    "有状态",
                    "多步骤",
                    "依赖链",
                    "extract",
                    "variables",
                    "占位符",
                    "JSON Pointer",
                    "depends_on",
                    "always_run",
                ),
                "content": (
                    "API-Agent 在 ExecutionPlan（native 与 postman 双引擎）及智能群测中"
                    "均支持有状态多步骤接口链路：\n"
                    "1. 初始变量：在计划顶层声明 variables（或在 OpenAPI 文档使用 "
                    "x-variables），存放非敏感初始参数（禁止存放 token/password 等敏感键）。\n"
                    "2. 响应提取（extract / x-extract）：每个请求步骤可配置提取规则，"
                    "支持 source 为 json（按 RFC 6901 JSON Pointer，如 /data/id、/data/token）、"
                    "header（按 header_name 提取响应头）或 status（提取状态码）。"
                    "敏感字段（token/secret/password 等或 sensitive: true）会自动脱敏为 "
                    "[REDACTED]，绝不泄漏到结果报告或 AI 上下文。\n"
                    "3. 占位符注入：后续步骤可在 path、query、headers、body 及 json_equals "
                    '断言的 expected 中引用 {{var_name}}；整值占位符 "{{id}}" 保留原标量类型。\n'
                    "4. 依赖与清理：配置 depends_on 指定前置步骤 ID，前置失败时自动跳过"
                    "（dependency_failed）；配置 always_run: true 可作为清理步骤（teardown），"
                    "即使中间业务断言失败也会尽量执行删除清理。"
                ),
            },
            {
                "section": "运行期安全校验与双引擎一致性",
                "keywords": ("unsafe_variable_value", "missing_variable", "native", "postman"),
                "content": (
                    "有状态链路的安全约束：\n"
                    "- 静态检查：计划校验阶段强制要求每个 {{var}} 必须来自顶层 variables "
                    "或前序步骤的 extract，未绑定占位符会被拒绝。\n"
                    "- 运行期防护：若提取值包含路径穿越（.. / 斜杠 / 百分号）或请求头换行符，"
                    "将阻止发送并标记 unsafe_variable_value；若上游提取失败导致变量缺失，"
                    "下游标记为 skipped（missing_variable）。\n"
                    "- 双引擎支持：native 原生引擎与 postman CLI 引擎（通过固定纯数据 "
                    "prerequest/test 模板及 pm.collectionVariables）均完整支持提取、注入、"
                    "依赖跳过与 always_run 清理。"
                ),
            },
        ],
    },
    {
        "doc_id": "model-testing",
        "category": "大模型测试",
        "title": "大模型测试（多协议、专用端点与能力画像）使用方法",
        "sections": [
            {
                "section": "拉取模型与多协议支持",
                "keywords": ("大模型测试", "OpenAI", "Anthropic", "Gemini", "protocol"),
                "content": (
                    "进入「大模型测试」（/model-testing）页面可批量体检第三方或本地大模型网关：\n"
                    "1. 输入接口 Base URL 与 API Key（若与系统设置同源可勾选复用已保存密钥，"
                    "跨源绝不转发旧密钥）。\n"
                    "2. 选择协议模式：支持 auto（自动识别）、OpenAI 兼容（/v1/models、"
                    "Bearer 鉴权）、Anthropic Messages（/v1/messages、x-api-key 与 "
                    "anthropic-version）以及 Gemini generateContent（/v1beta/models、"
                    "x-goog-api-key）。\n"
                    "3. 点击「拉取模型」后展示模型清单，自动结合元数据与命名规则推断模型分类"
                    "（chat / reasoning / embedding / rerank / image / audio / video / "
                    "moderation）与输入/输出模态。"
                ),
            },
            {
                "section": "进阶能力与专用端点探测",
                "keywords": (
                    "TTFT",
                    "流式",
                    "结构化输出",
                    "json_schema",
                    "工具调用",
                    "视觉",
                    "rerank",
                    "moderation",
                    "TTS",
                    "ASR",
                ),
                "content": (
                    "勾选待测模型与探测维度后点击「开始探测」：\n"
                    "- 对话模型支持探测：基础可用性（availability）、工具调用（tool_calling，"
                    "支持 /chat/completions 与 /responses 回退）、SSE 流式响应与首字延迟"
                    "（streaming / ttft_ms）、结构化输出（structured_output，优先测试 "
                    "json_schema 并自动回退 json_object）及视觉输入（input_modalities）。\n"
                    "- 专用模型自动路由对应端点：embedding（/embeddings 或 :embedContent）、"
                    "rerank（/rerank）、moderation（/moderations）、image（/images/generations）、"
                    "audio（自动识别 ASR /audio/transcriptions 或 TTS /audio/speech）。\n"
                    "- 结果表格支持按关键词、模型类型、可用性与工具调用多条件筛选，"
                    "并可点击单行查看完整探测详情与元数据。"
                ),
            },
        ],
    },
    {
        "doc_id": "engines-and-mcp",
        "category": "执行引擎与 MCP",
        "title": "测试执行引擎（Native / Postman CLI）与 MCP 工具服务",
        "sections": [
            {
                "section": "Native 引擎与 Postman CLI 引擎",
                "keywords": ("native", "postman", "执行引擎", "Newman", "断言", "json_equals"),
                "content": (
                    "在「系统设置 → 测试执行引擎」可切换默认执行引擎，测试计划 JSON 与"
                    "执行引擎完全解耦：\n"
                    "- native（本地原生引擎，默认）：纯 Python HTTPX 进程内执行，"
                    "零外部依赖、速度快，适合日常调试与持续测试。\n"
                    "- postman（Postman CLI 沙箱引擎）：调用本机 Postman CLI（默认 "
                    ".cache/tools/postman/postman.exe 或环境变量 POSTMAN_CLI_PATH）"
                    "在临时隔离工作区运行集合并输出 Newman v2.1 报告。\n"
                    "- 两套引擎统一支持 5 类确定性断言：status（HTTP 状态码）、"
                    "json_equals（JSON Pointer 严格深比较）、json_exists（JSON Pointer 存在性）、"
                    "content_type（application/json）与 response_time（毫秒耗时上限）。"
                ),
            },
            {
                "section": "内置 MCP 服务器接入方法",
                "keywords": ("MCP", "postmanmcp", "stdio", "validate_plan", "run_plan", "Cursor"),
                "content": (
                    "API-Agent 提供内置受控 stdio MCP 服务器（命令：uv run --project backend "
                    "--locked api-agent-mcp），仅暴露 3 个纯数据工具（无任意 Shell/文件能力）：\n"
                    "1. postman_status：只读探测执行引擎可用性与版本。\n"
                    "2. validate_plan：离线校验 v1 ExecutionPlan 结构与目标白名单策略。\n"
                    "3. run_plan：在受控边界内执行计划并返回归一化断言报告。\n"
                    "在系统设置中点击 Postman 引擎卡片右上角齿轮图标即可一键复制 "
                    "Claude Desktop / Cursor / Cline 的 MCP 接入配置。"
                ),
            },
        ],
    },
    {
        "doc_id": "target-policy-and-proxy",
        "category": "安全策略与环境配置",
        "title": "目标地址策略、安全护栏与系统代理配置",
        "sections": [
            {
                "section": "目标放开规则与可选白名单",
                "keywords": ("TARGET_ALLOWED_ORIGINS", "白名单", "域名", "局域网", "公网", "护栏"),
                "content": (
                    "目标地址访问策略（通过项目根目录 .env 或环境变量配置）：\n"
                    "- 默认策略：TARGET_ALLOWED_ORIGINS=[]（为空）时，允许访问任意合法的 "
                    "http(s) / ws(s) 域名、局域网 IP、回环地址与公网 IP。\n"
                    "- 白名单收口：若在 .env 中显式配置非空 JSON 数组（如 "
                    'TARGET_ALLOWED_ORIGINS=["http://127.0.0.1:8080"]），则接口测试、'
                    "长连接、计划执行、智能群测与 AI 编排统一仅放行列表内源。\n"
                    "- 始终生效的安全护栏：禁止非 http(s)/ws(s) 协议、禁止 URL 内嵌账号密码"
                    "（user:pass@）、禁止覆盖 Host/Content-Length 等传输保留头、跨源重定向"
                    "自动剥离认证头；plan.auth_ref 仅允许引用 TARGET_AUTH_ENV_VARS 白名单变量名。"
                ),
            },
            {
                "section": "系统代理开关与响应裁剪",
                "keywords": (
                    "TRUST_SYSTEM_PROXY",
                    "代理",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "OMIT_RESPONSE_PAYLOAD",
                ),
                "content": (
                    "网络代理与隐私环境变量：\n"
                    "- TRUST_SYSTEM_PROXY（默认 false）：为避免本机系统代理意外劫持本地或"
                    "内网接口请求，默认忽略 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY；"
                    "当需要通过代理访问外网 API 或海外大模型接口时，在 .env 中设置 "
                    "TRUST_SYSTEM_PROXY=true 并重启服务，即可全链路生效（覆盖控制台 "
                    "HTTP/SSE、WebSocket、native/postman 执行引擎、MCP 子进程与大模型接口）。\n"
                    "- OMIT_RESPONSE_PAYLOAD（默认 false）：设为 true 时执行引擎不返回"
                    "响应头与响应体，仅返回状态码、耗时与断言结论，适合高敏响应场景。"
                ),
            },
        ],
    },
    {
        "doc_id": "settings-appearance-history",
        "category": "外观、历史与配置",
        "title": "主题配色定制、历史记录与本地重置",
        "sections": [
            {
                "section": "主题配色与 Logo / 助手图标配色",
                "keywords": ("主题配色", "外观", "Logo 配色", "AI 助手图标配色", "绿色", "重置"),
                "content": (
                    "在「系统设置 → 外观与配色」中可分别自定义暗色主题与亮色主题的完整调色板"
                    "（修改即时生效并保存在当前浏览器，互不干扰）：\n"
                    "- 支持自定义界面背景、文字与边框、强调色、状态色、HTTP 方法色以及"
                    "「品牌、图标与阴影」分组。\n"
                    "- 在「品牌、图标与阴影」中可单独修改「项目 Logo 配色」（控制侧边栏信号"
                    "发射动画 Logo 颜色）与「AI 助手图标配色」（控制小Y 动态多表情机器人图标"
                    "颜色，默认绿色）。\n"
                    "- 点击右上角「重置暗色主题 / 重置亮色主题」可一键恢复当前主题的全部默认配色。"
                ),
            },
            {
                "section": "执行结果历史与工作台设置",
                "keywords": ("历史记录", "筛选", "删除", "重置本地设置", "工作台设置"),
                "content": (
                    "历史管理与系统配置：\n"
                    "- 「执行结果」（/history）页汇总展示 HTTP 请求、WebSocket 会话与智能"
                    "群测历史，支持按 URL/关键词与状态筛选，支持查看详情、单条删除与清空历史。\n"
                    "- 「系统设置 → 工作台设置」集中管理三个测试页的默认超时、事件数、重定向、"
                    "写权限等参数，点击「重置本地设置」仅清除浏览器偏好与布局缓存，不会删除"
                    "后端大模型配置、测试历史或小Y对话。\n"
                    "- 「系统设置 → 大模型服务」配置对话模型（小Y 对话与 AI 总结）与可选独立的"
                    "文档/test 生成模型，支持一键预设、拉取模型列表与连通性探测，密钥仅存本地 "
                    "SQLite 且永不回显明文。"
                ),
            },
        ],
    },
    {
        "doc_id": "assistant-xiaoy",
        "category": "AI 助手小Y",
        "title": "AI 助手小Y 能力、当前测试结果读取工具与向量知识库",
        "sections": [
            {
                "section": "对话分析与当前测试结果读取工具",
                "keywords": (
                    "小Y",
                    "AI 助手",
                    "读取数据中",
                    "努力思考中",
                    "工具调用",
                    "故障排查",
                ),
                "content": (
                    "AI 助手小Y 位于接口测试、长连接测试与智能群测页面底部，可直接切换对话模型"
                    "并围绕当前测试进行多轮问答：\n"
                    "- 当前测试结果读取工具（read_current_test_result）：当用户询问当前接口"
                    "报错原因、状态码含义、返回异常、超时、WebSocket 断开或群测失败排查时，"
                    "小Y 会调用读取工具获取当前测试页的实时请求参数、响应状态/响应头/响应体、"
                    "错误信息、SSE/WebSocket 事件或群测断言明细（敏感凭据已自动脱敏），"
                    "结合观测证据给出根因分析与具体可执行的解决办法。\n"
                    "- 运行状态提示：发送问题后未开始流式输出前显示「努力思考中...」，"
                    "调用读取工具时显示「读取数据中...」，并附带动态省略号循环提示。\n"
                    "- 对话图标：小Y 标题栏配备绿色动态多表情机器人图标，会定时随机切换多种表情，"
                    "点击也可立即切换，配色可在系统设置中修改。"
                ),
            },
            {
                "section": "小Y 向量知识库准备与构建方法",
                "keywords": (
                    "向量知识库",
                    "Chroma",
                    "ChromaDB",
                    "Embedding",
                    "向量模型",
                    "构建向量",
                    "语义检索",
                    "词法回退",
                ),
                "content": (
                    "AI 助手小Y 内置基于 Chroma（ChromaDB 本地持久化集合）的项目全功能知识库：\n"
                    "1. 配置向量模型：打开「系统设置 → AI 助手小Y 向量知识库」，选择复用主大"
                    "模型服务地址或填写独立的 Embedding Base URL 与 API Key，并填写向量模型"
                    "名称（如阿里千问 text-embedding-v4、text-embedding-3-small、BAAI/bge-m3 等）"
                    "与召回条数 Top-K，点击「保存向量配置」。\n"
                    "2. 构建向量索引：配置完成后点击「构建向量知识库」，后端会分批调用 "
                    "/embeddings 接口生成归一化向量并持久化写入本地 Chroma 集合。\n"
                    "3. 智能检索与优雅回退：Chroma 索引就绪后自动启用余弦相似度语义检索；"
                    "在尚未接入向量模型或未构建索引前，系统会自动采用本地词法检索回退，"
                    "确保小Y 仍可准确回答项目功能与使用方法问题。"
                ),
            },
        ],
    },
)


def build_corpus_chunks() -> list[KnowledgeChunk]:
    """生成稳定有序的项目知识库分块列表。"""
    chunks: list[KnowledgeChunk] = []
    for doc in _RAW_DOCUMENTS:
        doc_id = doc["doc_id"]
        category = doc["category"]
        title = doc["title"]
        for idx, section_data in enumerate(doc["sections"], start=1):
            chunks.append(
                KnowledgeChunk(
                    chunk_id=f"{doc_id}#{idx}",
                    doc_id=doc_id,
                    category=category,
                    title=title,
                    section=section_data["section"],
                    keywords=tuple(section_data["keywords"]),
                    content=section_data["content"].strip(),
                )
            )
    return chunks


def corpus_hash(chunks: list[KnowledgeChunk] | None = None) -> str:
    """计算当前知识语料的 SHA-256 指纹，用于判断已建向量是否过期。"""
    items = build_corpus_chunks() if chunks is None else chunks
    payload = [
        {
            "chunk_id": item.chunk_id,
            "category": item.category,
            "title": item.title,
            "section": item.section,
            "keywords": list(item.keywords),
            "content": item.content,
        }
        for item in items
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def list_document_summaries(chunks: list[KnowledgeChunk] | None = None) -> list[dict]:
    """按文档汇总知识库目录，供前端展示准备就绪的语料结构。"""
    items = build_corpus_chunks() if chunks is None else chunks
    grouped: dict[str, dict] = {}
    for item in items:
        entry = grouped.setdefault(
            item.doc_id,
            {
                "doc_id": item.doc_id,
                "category": item.category,
                "title": item.title,
                "sections": [],
                "chunk_count": 0,
            },
        )
        entry["sections"].append(item.section)
        entry["chunk_count"] += 1
    return list(grouped.values())
