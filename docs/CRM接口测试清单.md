# CRM 系统接口测试清单

> 目标环境：本机 Docker 部署的 CRM 系统（源码 `<CRM_PROJECT_DIRECTORY>`）
> 文档生成日期：2026-11-16（2026-11-26 补充 WebSocket `/api/ws/chat` 端点）
> 所有接口均已在运行环境实测，响应状态以实测为准。

---

## 一、环境与入口

### 1.1 容器清单

| 容器 | 镜像 | 宿主机端口 → 容器端口 | 作用 |
| --- | --- | --- | --- |
| crm-frontend | crm-frontend:1.0.0 | 8088 → 80 | Nginx，前端页面 + `/api/` 与 `/api/ws/` 反向代理 |
| crm-backend | crm-backend:1.0.0 | 8081 → 8080 | Spring Boot 3（Java 17），业务接口 + WebSocket 实时对话 |
| crm-rag | crm-rag:1.0.0 | 8002 → 8000 | FastAPI，RAG 智能问答（SSE） |
| crm-mysql | mysql:8.4 | 3307 → 3306 | 业务库 `elcrms` |
| crm-redis-stack | redis/redis-stack-server | 6380 → 6379；8001 → 8001 | 向量库 `crm_knowledge` + RedisInsight |

### 1.2 三条访问链路

```
① 浏览器统一入口（推荐，含 Nginx 代理、SSE 不缓冲与 WebSocket Upgrade 配置）
   HTTP / SSE：http://127.0.0.1:8088/api/...
   WebSocket ：ws://127.0.0.1:8088/api/ws/chat

② 后端直连（绕过 Nginx，调试用）
   HTTP / SSE：http://127.0.0.1:8081/api/...
   WebSocket ：ws://127.0.0.1:8081/api/ws/chat

③ RAG 服务直连（不经 Java 层）
   http://127.0.0.1:8002/...
```

### 1.3 接口总览（共 10 类 / 12 个端点）

| # | 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | POST | `/api/user/register` | 否 | 用户注册 |
| 2 | POST | `/api/user/login` | 否 | 登录换取 JWT |
| 3 | GET | `/api/customer/list` | 是 | 客户分页 + 关键词查询 |
| 4 | GET | `/api/customer/{id}` | 是 | 客户详情 |
| 5 | POST | `/api/customer` | 是 | 新增客户 |
| 6 | PUT | `/api/customer/{id}` | 是 | 编辑客户（乐观锁） |
| 7 | DELETE | `/api/customer/{id}` | 是 | 软删除客户 |
| 8 | GET | `/api/chat/ask/stream` | 是 | 流式问答（SSE，后端转发 RAG） |
| 9a | GET | `/health` | 否 | RAG 健康检查（直连 8002） |
| 9b | POST | `/ask/stream` | 否 | RAG 流式问答（直连 8002） |
| 9c | POST | `/ask` | 否 | RAG 非流式问答（直连 8002） |
| 10 | GET (WS) | `/api/ws/chat` | 可选 | 实时双向对话（WebSocket，鉴权或匿名外连） |

> 后端未接入 OpenAPI/Swagger：访问 `/openapi.json`、`/docs` 均被登录拦截器拦下并返回 401。

---

## 二、鉴权机制（测试前置）

- 登录成功后返回 JWT，HTTP 接口**必须放在名为 `token` 的请求头**（不是 `Authorization: Bearer`）。
- HTTP 登录拦截器白名单共四个：`/api/user/register`、`/api/user/login`、`/api/ws/**`、`/error`；**其余全部 HTTP 路径**（含 RAG 转发接口）都需要 token。
- WebSocket 端点 `/api/ws/chat` 跳过 HTTP `LoginInterceptor`，改由专用握手拦截器 `ChatHandshakeInterceptor` 校验：按 `token` 请求头 → `Authorization: Bearer <JWT>` → `?token=<JWT>` 顺序读取令牌；未传 token 时允许匿名外部客户端接入，但若显式传入无效/过期 token 则直接返回 HTTP 401 拒绝握手。
- JWT 载荷：`userId`、`email`、`iat`、`exp`；有效期 24 小时（`jwt.expire=86400000`）。
- 未携带或校验失败时，**HTTP 状态码为 401**，body 为 `{"code":"401","msg":"...","data":null}`。

### 2.1 获取 token

```bash
curl -s -X POST http://127.0.0.1:8088/api/user/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@elcrms.com","password":"123456"}'
```

实测响应：

```json
{"code":"200","msg":"登录成功","data":{"token":"eyJ0eXAiOiJKV1Qi...","userId":1,
 "name":"系统管理员","email":"admin@elcrms.com","company":"ELCRMS 科技",
 "phone":"13800000001","perms":[]}}
```

### 2.2 可用测试账号

| 邮箱 | 密码 | 姓名 | 角色 | `perms` 实测 |
| --- | --- | --- | --- | --- |
| admin@elcrms.com | 123456 | 系统管理员 | 无绑定角色 | `[]` |
| director@elcrms.com | 123456 | 陈总监 | 销售总监 | 未绑定，预期 `[]` |
| 11@11.com | 123456 | 11 | user_role 中 id=7 → 角色4 | 见下 |

> 密码统一为 MD5 存储，`123456` 的 MD5 是 `e10adc3949ba59abbe56e057f20f883e`。
> 注意：部分种子用户（如 `director@elcrms.com`）密码字段是 BCrypt 串，与当前 MD5 登录逻辑不匹配，**无法登录**。测试请用 `admin@elcrms.com` / `11@11.com` 这类 MD5 账号。
> `perms` 为权限 URL 列表（`/customer`、`/chat`），仅角色绑定后非空；实测 admin 为空数组，**接口本身不做权限校验**，仅登录拦截。

### 2.3 鉴权相关测试点（已实测）

| 场景 | 请求 | 结果 |
| --- | --- | --- |
| 无 token 访问客户列表 | GET `/api/customer/list` | HTTP 401，`{"code":"401","msg":"未登录，请先登录"}` |
| 伪造 token | 同上 + `token: garbage` | HTTP 401 |
| 无 token 访问 SSE | GET `/api/chat/ask/stream?question=hi` | HTTP 401 |
| 无 token 访问不存在路径 | GET `/api/nope` | HTTP 401（拦截器先于路由生效，不会返回 404） |
| OPTIONS 预检 | OPTIONS `/api/customer/list` | HTTP 200，放行 |
| 非法 token 连接 WebSocket | GET `/api/ws/chat?token=garbage`（WS 握手） | HTTP 401，握手拒绝 |
| 匿名连接 WebSocket | GET `/api/ws/chat?room=lobby&name=匿名端`（WS 握手） | HTTP 101 建连成功，`clientType="external"`, `authenticated=false` |

---

## 三、统一响应结构

```json
{ "code": "200", "msg": "操作成功", "data": {} }
```

`code` 取值（**均为字符串**，非数字）：

| code | 含义 | 典型触发 |
| --- | --- | --- |
| 200 | 成功 | 正常返回 |
| 400 | 参数校验失败 | `@Validated` 未通过、邮箱重复 |
| 401 | 未登录 / token 失效 | 拦截器直接写出 HTTP 401 |
| 500 | 业务异常 | 记录不存在、密码错误、DB 约束冲突 |

> ⚠️ **重要陷阱**：除 401 外，`code=400/500` 的业务错误 **HTTP 状态码仍是 200**。
> 断言必须检查 body 中的 `code` 字段，不能只看 HTTP status。
> ⚠️ 业务异常（`RuntimeException`）会把 `e.getMessage()` 原样返回，SQL 异常会**泄露完整堆栈**（见 4.6）。

---

## 四、接口详情

### 4.1 POST /api/user/register — 用户注册

白名单接口，无需 token。

**请求体**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| name | string | 是 | 非空，≤100 |
| email | string | 是 | 非空，邮箱格式，全局唯一 |
| phone | string | 是 | 非空，`^1[3-9]\d{9}$` |
| company | string | 是 | 非空，≤200 |
| password | string | 是 | 非空，6–32 位 |
| confirmPassword | string | 是 | 非空，须与 password 一致 |

**成功响应**：`{"code":"200","msg":"操作成功","data":"注册成功"}`

**错误用例（全部实测）**

| 场景 | 请求体要点 | 响应 |
| --- | --- | --- |
| 两次密码不一致 | password=123456, confirm=654321 | `500` 两次密码输入不一致 |
| 密码过短 | password=123 | `400` 密码长度需在6-32位之间 |
| 邮箱已注册 | email=admin@elcrms.com | `500` 该邮箱已被注册 |
| 邮箱格式错误 | email=not-an-email | `400` 邮箱格式不正确 |
| 手机号格式错误 | phone=123 | `400` 手机号格式不正确 |
| 姓名为空 | 缺 name | `400` 姓名不能为空 |

```bash
curl -s -X POST http://127.0.0.1:8088/api/user/register \
  -H "Content-Type: application/json" \
  -d '{"name":"接口测试用户","email":"apireg@example.com","phone":"13900003333",
       "company":"测试公司","password":"test123456","confirmPassword":"test123456"}'
```

> 注册成功后**不会自动分配角色**，新用户 `perms` 为空。

---

### 4.2 POST /api/user/login — 登录

白名单接口。

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| email | string | 是 | 非空，邮箱格式 |
| password | string | 是 | 非空 |

**成功响应**：见 2.1，`data` 为 `LoginVO`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| token | string | JWT，后续所有请求头使用 |
| userId | long | 用户 ID |
| name / email / company / phone | string | 用户信息 |
| perms | string[] | 权限 URL 列表，无角色时为 `[]` |

**错误用例（实测）**

| 场景 | 响应 |
| --- | --- |
| 密码错误 | `500` 邮箱或密码错误 |
| 邮箱不存在 | `500` 邮箱或密码错误（与密码错误同文案，防枚举） |
| 邮箱格式非法 | `400` 邮箱格式不正确 |
| 缺 password | `400` 密码不能为空 |

---

### 4.3 GET /api/customer/list — 客户分页查询

需要 token。

**Query 参数**（`CustomerQueryDTO`）

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| keyword | string | 无 | 模糊匹配 name / company / email |
| pageNum | int | 1 | 页码，`pagehelper.reasonable=true` 会纠正越界值 |
| pageSize | int | 10 | 每页条数（**无上限**，见风险项） |

**成功响应**（`data` 为 PageHelper `PageInfo<CustomerVO>`，实测截取）

```json
{"code":"200","msg":"操作成功","data":{
  "pageNum":1,"pageSize":3,"size":3,"total":10,"pages":4,
  "isFirstPage":true,"isLastPage":false,"hasNextPage":true,
  "navigateFirstPage":1,"navigateLastPage":4,"navigatepageNums":[1,2,3,4],
  "list":[
    {"id":1,"name":"李明","company":"华为技术有限公司","email":"liming@huawei.com",
     "phone":"13911110001","status":"active","totalSales":580000.00,"points":5300,
     "lastContact":"2026-05-20","createdAt":"2025-10-10T10:00:00","updatedAt":"2026-11-02T21:23:52"}
  ]}}
```

`CustomerVO` 字段：`id, name, company, email, phone, status, totalSales, points, lastContact, createdAt, updatedAt`
（不含 `version`，实体已隔离）

**测试点**

| 用例 | 预期 |
| --- | --- |
| 不传参 | pageNum=1, pageSize=10 |
| keyword=华为 | 命中 1 条（company 模糊） |
| keyword 命中 name/email | 均能匹配 |
| keyword 无匹配 | list 为空，total=0 |
| pageNum=0 | 被纠正为 1（实测） |
| pageSize=1000 | 原样生效，返回全部 10 条（无上限限制） |
| 排序 | `ORDER BY updated_at DESC` |

> `status` 取值：`active` / `potential` / `inactive`。**列表不过滤软删除**，`inactive` 记录仍会返回。

---

### 4.4 GET /api/customer/{id} — 客户详情

需要 token。`id` 为 Long。

- 成功：`{"code":"200","data":{...CustomerVO...}}`
- 不存在：`{"code":"500","msg":"客户不存在","data":null}`，**HTTP 200**
- `id` 非数字：参数类型转换异常 → `500` 服务器内部错误
- 软删除后的记录**仍可查**（`status` 为 `inactive`）

---

### 4.5 POST /api/customer — 新增客户

需要 token。

**请求体**（`CustomerDTO`）

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| name | string | 是 | 非空 |
| company | string | 是 | 非空 |
| email | string | 是 | 非空，邮箱格式，**DB 唯一键** |
| phone | string | 是 | 非空，`^1[3-9]\d{9}$` |
| status | string | 否 | active / potential / inactive，缺省自动填 `potential` |
| lastContact | date | 否 | `yyyy-MM-dd` |

**成功响应**：`{"code":"200","msg":"操作成功","data":"新增客户成功"}`

> ⚠️ 新增接口**不返回新记录 id**（`useGeneratedKeys` 已回填到实体但未返回）。需要 id 时请按 email 反查列表。

**错误用例（实测）**

| 场景 | 响应 |
| --- | --- |
| 缺 name | `400` 姓名不能为空 |
| email 格式错误 | `400` 邮箱格式不正确 |
| phone 格式错误 | `400` 手机号格式不正确 |
| email 与已有客户重复 | `400` 该邮箱已被注册 |
| 不传 status | 成功，落库 `potential`，`lastContact=null` |

---

### 4.6 PUT /api/customer/{id} — 编辑客户

需要 token。**全量覆盖语义**。

**请求体**：同 4.5 `CustomerDTO`（字段校验一致）。

**实现细节**：先按 id 查原记录取 `version`，`BeanUtils.copyProperties(dto, customer)` 覆盖后执行
`UPDATE ... SET version = version + 1 WHERE id = ? AND version = ?`（乐观锁）。

| 场景 | 响应 |
| --- | --- |
| 正常更新 | `200` 编辑客户成功，`version` +1 |
| id 不存在 | `500` 客户不存在 |
| 并发版本冲突（rows=0） | `500` 数据已被修改，请刷新后重试 |
| 软删除后可编辑 | `200`（无状态前置校验，实测可改） |

**⚠️ 缺陷：PUT 不传 `status` 会报错并泄露堆栈（实测）**

因为 `BeanUtils` 把 `status=null` 覆盖进实体，而 DB 列 `status` 为 `NOT NULL DEFAULT 'potential'`，
最终返回 `code=500`，`msg` 中包含完整 MyBatis/SQL 异常与 SQL 语句：

```
### Error updating database. Cause: java.sql.SQLIntegrityConstraintViolationException:
Column 'status' cannot be null ... ### SQL: UPDATE customers SET name = ?, ... status = ?, ...
```

> 这是**真实可复现的接口缺陷**，建议测试用例覆盖。修复方向：编辑时 `status` 加 `@NotBlank`，或在 service 中对 null 做保留原值处理。
> 对比：新增接口对 null status 有兜底，编辑接口没有。

---

### 4.7 DELETE /api/customer/{id} — 软删除

需要 token。**不做物理删除**，执行 `UPDATE customers SET status='inactive'`。

| 场景 | 响应 |
| --- | --- |
| 正常删除 | `200` 删除客户成功 |
| id 不存在 | `500` 客户不存在 |
| 客户存在关联商机 | `500` 该客户存在 N 个关联商机，无法删除，请先处理商机记录 |
| 重复删除**同一**客户 | `200` 删除客户成功（**幂等**，实测第二次仍成功） |
| 删除后可查询 | 记录仍在，`status=inactive` |

**关联商机实测数据**

| 客户 id | 客户 | 关联商机数 | 删除结果 |
| --- | --- | --- | --- |
| 1 | 李明（华为） | 3 | 拒绝删除 |
| 2 | 赵芳（腾讯） | 2 | 拒绝删除 |
| 4 | 周敏（字节） | 2 | 拒绝删除 |
| 5 | 王客户（星河） | 1 | 拒绝删除 |
| 7/8/9/10 等 | 其余 | 0 | 可删除 |

> 关联商机数通过 `SELECT COUNT(1) FROM opportunities WHERE customer_id = ?` 实时统计，非缓存。

---

### 4.8 GET /api/chat/ask/stream — 流式问答（SSE）

需要 token。

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| question | query | string | 是 | 提问内容 |

- 响应 `Content-Type: text/event-stream`
- 后端用 WebClient 调用 RAG `/ask/stream`，**过滤** RAG 的 `[START]`/`[END]` 标记与 `data:` 前缀后转发
- 每个分片格式为 `data:<文本>\n\n`（注意 `data:` 后无空格）

实测（节选）：

```
data:如果您

data:指的是

data:“

data:我们

data:”
```

**测试点**

| 用例 | 预期 |
| --- | --- |
| 正常提问 | 逐字流式返回，首字节延迟低 |
| 经 8088 Nginx | 与直连一致（已关闭 `proxy_buffering`） |
| 无 token | HTTP 401 |
| 缺少 question 参数 | 抛 `MissingServletRequestParameterException` → `{"code":"500","msg":"服务器内部错误"}`，**HTTP 200 且非流式** |
| RAG 服务不可用 | 转发异常，返回 500 包装 |

> 完整 curl（带 `-N` 关闭本地缓冲）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8088/api/user/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@elcrms.com","password":"123456"}' \
  | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')

curl -N -H "token: $TOKEN" \
  "http://127.0.0.1:8088/api/chat/ask/stream?question=%E4%BD%A0%E5%A5%BD"
```

---

### 4.9 RAG 服务（直连 8002）

#### a) GET /health

无鉴权。`{"status":"ok"}` — 可用于健康探针。

#### b) POST /ask/stream — 流式问答

**请求体**：`{"question": "..."}`（`QuestionRequest`，string 必填）

响应 `text/event-stream`，格式：

```
data: [START]
data: <分片1>
data: <分片2>
...
data: [END]
```

> 分片粒度很细（常见单字/双字），实测一次回答约 300+ 个 `data:` 事件。

#### c) POST /ask — 非流式问答

**请求体**：同上。**成功响应**：`{"answer": "<完整回答>"}`

**错误用例（实测）**

| 场景 | HTTP | 响应 |
| --- | --- | --- |
| 缺 question 字段 | 422 | `{"detail":[{"type":"missing","loc":["body","question"],"msg":"Field required"...}]}` |
| question 为空串 `""` | 400 | `{"detail":"问题不能为空"}` |
| question 为数字 123 | 422 | `{"detail":[{"type":"string_type",...}]}` |
| 非法 JSON | 422 | FastAPI 标准校验错误 |

> 注意 RAG 的 HTTP 状态码语义是规范的（400/422），与 Java 后端「业务错误也返 200」的风格不一致，写断言时需分别处理。

**RAG 检索策略（源码事实）**

- 向量库：Redis Stack，索引 `crm_knowledge`，嵌入模型 `text-embedding-v4`（1024 维）
- 知识库：`crm-rag-fastAPI/data/CRM_QA.csv`，30 组问答
- 阈值：`SIMILARITY_THRESHOLD=0.45`，`TOP_K=3`，`TEMPERATURE=0.3`，`MAX_TOKENS=1000`
- 命中知识（相似度 ≥ 0.45）→ 把 top3 拼进 prompt，要求优先使用知识库信息
- **未命中即降级为纯 LLM 直答**（不报错，回答质量下降）
- 向量库不可用时也降级为纯 LLM 直答

**测试建议**

| 类型 | 问题示例 | 预期 |
| --- | --- | --- |
| 命中知识库 | `CRM系统有哪些核心功能模块？` | 回答贴合 CSV 中「六大核心模块」表述 |
| 未命中 | `今天天气怎么样？` | 走通用 LLM 回答，不报错 |
| 边界 | 空串 / 超长文本 / 特殊字符 | 分别 400 / 观察截断 / 正常容错 |

---

### 4.10 GET /api/ws/chat — 实时双向对话（WebSocket）

Spring WebSocket 原生文本长连接（`WebSocketConfig` + `ChatHandshakeInterceptor` + `ChatWebSocketHandler`），支持按房间广播消息、心跳保活与在线成员查询。

- **统一入口（推荐，经 Nginx `/api/ws/` 代理）**：`ws://127.0.0.1:8088/api/ws/chat`
- **后端直连（调试用）**：`ws://127.0.0.1:8081/api/ws/chat`
- **Nginx 代理配置**：`crm-frontend/nginx.conf` 已对 `/api/ws/` 配置 `proxy_http_version 1.1`、`Upgrade $http_upgrade`、`Connection "upgrade"`、`proxy_buffering off` 与 `proxy_read_timeout 3600s`。

#### 握手鉴权规则（`ChatHandshakeInterceptor`）

按优先级依次读取令牌：
1. 请求头 `token: <JWT>`
2. 请求头 `Authorization: Bearer <JWT>`
3. Query 参数 `?token=<JWT>`

- **有效 JWT**：解析 `userId`/`email` 并回查用户表确定显示名，`authenticated=true`；未指定 `client` 时默认 `clientType="crm"`。
- **未传 token**：放行建连，标记为匿名外部客户端（`authenticated=false`，默认 `clientType="external"`，默认 `sender="API测试端"`）。
- **传入无效或过期 token**：直接返回 **HTTP 401** 并拒绝升级握手。

#### Query 与 Header 参数

| 参数 | 位置 | 类型 | 默认 | 约束与说明 |
| --- | --- | --- | --- | --- |
| `token` | Header / Query | string | 无 | 可选 JWT；传入时必须合法，否则握手返回 401 |
| `Authorization` | Header | string | 无 | 可选 `Bearer <JWT>`（优先级低于 `token` 头、高于 `?token=`） |
| `room` | Query | string | `lobby` | 房间名；仅保留汉字、字母、数字、`_`、`-`，最长 40 字符 |
| `name` | Query | string | 鉴权用户名 / `API测试端` | 会话显示名称，最长 32 字符 |
| `client` | Query | string | `crm`（鉴权）/ `external`（匿名） | 客户端标识（如 `crm`、`external`、`api-agent`） |
| `echo` | Query | string | `true` | 是否向发送者本人回显 `chat` 广播；传 `false` 关闭自回显 |

#### 文本帧协议（JSON / 纯文本）

**服务端主动推送帧**

| `type` | 触发时机 | 关键字段与说明 |
| --- | --- | --- |
| `welcome` | 握手建连成功后仅发给本人 | `sessionId`, `room`, `sender`, `clientType`, `authenticated`, `onlineCount`, `peers[]`, `content`, `timestamp` |
| `system` | 房间内其他成员加入 / 离开时广播给其余成员 | `event`（`"join"` / `"leave"`）, `room`, `sessionId`, `sender`, `clientType`, `authenticated`, `onlineCount`, `peers[]`, `content`, `timestamp` |
| `pong` | 收到 `{"type":"ping"}` 后仅发给本人 | `room`, `sessionId`, `onlineCount`, `timestamp` |
| `peers` | 收到 `{"type":"peers"}` 或 `{"type":"who"}` 后仅发给本人 | `room`, `onlineCount`, `peers[]`, `timestamp` |
| `chat` | 收到有效文本或 JSON 消息后向同房间广播 | `messageId`, `room`, `senderSessionId`, `sender`, `clientType`, `authenticated`, `format`（`"text"` / `"json"`）, `content`, `timestamp`（当 `echo=false` 时不回显给发送者） |
| `error` | 收到空串或纯空白文本帧时仅发给本人 | `room`, `content: "消息内容不能为空"`, `timestamp` |

**客户端上行帧规则**

- 心跳：`{"type":"ping"}` → 返回 `pong`
- 成员查询：`{"type":"peers"}` 或 `{"type":"who"}` → 返回 `peers`
- 标准 JSON 消息：`{"content":"你好"}`（同样兼容 `message` / `text` / `msg` 字符串字段，可选 `format`）→ 广播 `chat`（默认 `format="text"`）
- 自定义 JSON 对象：如 `{"event":"probe","seq":1}`（不含上述文本字段）→ 原样序列化进 `content` 并以 `format="json"` 广播
- 纯文本消息：直接广播 `chat`（`format="text"`），单条内容最长截断至 8000 字符
- 空白内容（如 `"   "`）：返回 `{"type":"error","content":"消息内容不能为空",...}`，不广播

#### 实测用例表

| 场景 | 连接与上行要点 | 实测结果 |
| --- | --- | --- |
| 鉴权建连 + 心跳 + 成员查询 + 文本聊天 | `ws://127.0.0.1:8088/api/ws/chat?room=qa-room&client=api-agent` + `token` 头，依次发送 `{"type":"ping"}`、`{"type":"peers"}`、文本消息 | HTTP 101；依次收到 `welcome`（`authenticated=true`）、`pong`、`peers`、`chat`（`format="text"`） |
| 匿名外连 + 自定义 JSON + 空白消息校验 | `ws://127.0.0.1:8088/api/ws/chat?room=qa-ext&name=外部测试端&client=external`，依次发送 `{"event":"probe","seq":1}` 与 `"   "` | HTTP 101；收到 `welcome`（`authenticated=false`）、`chat`（`format="json"`）与 `error`（`消息内容不能为空`） |
| 非法 token 拒绝握手 | `ws://127.0.0.1:8088/api/ws/chat?token=invalid.jwt.token` | HTTP 401，握手拒绝 |
| 同房间多端互通 + `echo=false` 抑制自回显 | 终端 A（`echo=false`）与终端 B 加入同房间，A 发消息 | B 收到 `system(join)`、`chat` 与 `system(leave)`；A 不收到自己的 `chat` 回显 |
| 后端直连对比 | `ws://127.0.0.1:8081/api/ws/chat` | 与经 8088 Nginx 代理行为完全一致 |

---

## 五、数据库测试数据

库名 `elcrms`，共 11 张表：`users`、`customers`、`opportunities`、`companies`、`stores`、
`point_rules`、`point_transactions`、`roles`、`perms`、`role_perm`、`user_role`。

| 表 | 行数 | 说明 |
| --- | --- | --- |
| customers | 10 | 含 `active` / `potential` / `inactive` 三种状态 |
| users | 11 | 密码为 MD5 或 BCrypt（混存） |
| opportunities | 10 | 关联客户 1/2/3/4/5/6 |
| companies / stores | 3 / 5 | 有外键 `stores.company_id → companies.id` |
| roles / perms / role_perm / user_role | 5 / 2 / 1 / 2 | 权限体系数据较少 |

直接查库（端口 3307）：

```bash
docker exec crm-mysql mysql -uroot -p123456 -e "USE elcrms; SELECT id,name,status FROM customers LIMIT 5;"
```

> `init.sql` 仅在数据卷首次初始化时执行（`docker-entrypoint-initdb.d`）。
> 重置基线数据需 `docker compose down -v && docker compose up -d --build`，会清空全部数据。

---

## 六、已知风险与缺陷清单

| 级别 | 问题 | 位置 | 影响 |
| --- | --- | --- | --- |
| 高 | PUT 客户不传 `status` 触发 `Column 'status' cannot be null`，并把完整 SQL 异常堆栈返回客户端 | 4.6 / `CustomerServiceImpl.update` | 信息泄露 + 接口不可用 |
| 高 | 所有未捕获异常的 `msg` 直接回显 `e.getMessage()`，含 SQL、表名、字段名 | `GlobalExceptionHandler` | 信息泄露 |
| 中 | 业务错误（400/500）统一返回 HTTP 200 | 全局 | 客户端/网关无法按状态码区分成败 |
| 中 | `pageSize` 无上限，`pageSize=1000000` 可拖垮 DB | 4.3 | 资源耗尽风险 |
| 中 | 软删除后记录仍可被 GET 查看、被 PUT 修改，且列表接口不过滤 | 4.4/4.6/4.3 | 业务语义不一致 |
| 中 | 新增客户不返回新 id | 4.5 | 自动化测试链路需二次查询 |
| 低 | 401 由拦截器手写 JSON，未走统一 `Result` 序列化 | `LoginInterceptor` | 响应体格式靠字符串拼接 |
| 低 | 不存在的路径返回 401 而非 404 | `WebConfig` 拦截 `/**` | 路由错误被掩盖 |
| 低 | RAG SSE 分片过碎（单字级） | `rag_system.py` | 前端渲染压力、网络开销大 |
| 低 | 向量库不可用/未命中时静默降级为纯 LLM | 4.9 | 回答质量下降但无告警 |
| 低 | 种子用户密码 MD5 / BCrypt 混存，部分账号无法登录 | `init.sql` | 测试账号选择需注意 |
| 低 | `Content-Type` 未声明 `charset=utf-8`（内容实为 UTF-8） | 后端 | 部分客户端可能猜错编码 |

---

## 七、快速冒烟脚本

```bash
BASE=http://127.0.0.1:8088

# 1. 登录
TOKEN=$(curl -s -X POST $BASE/api/user/login -H "Content-Type: application/json" \
  -d '{"email":"admin@elcrms.com","password":"123456"}' \
  | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] && echo "✅ 登录成功" || echo "❌ 登录失败"

# 2. 鉴权拦截
[ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/customer/list)" = "401" ] \
  && echo "✅ 无 token 被拦截" || echo "❌ 鉴权未生效"

# 3. 列表
curl -s -H "token: $TOKEN" "$BASE/api/customer/list?pageNum=1&pageSize=3" \
  | python -c "import sys,json;d=json.load(sys.stdin);print('✅ 列表 total=',d['data']['total']) if d['code']=='200' else print('❌',d)"

# 4. 详情
curl -s -H "token: $TOKEN" "$BASE/api/customer/1" \
  | python -c "import sys,json;d=json.load(sys.stdin);print('✅ 详情',d['data']['name']) if d['code']=='200' else print('❌',d)"

# 5. 参数校验
curl -s -X POST $BASE/api/customer -H "token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"n","company":"c","email":"bad","phone":"1"}' \
  | python -c "import sys,json;d=json.load(sys.stdin);print('✅ 校验生效' if d['code']=='400' else '❌',d['msg'])"

# 6. RAG 健康
curl -s http://127.0.0.1:8002/health | grep -q ok && echo "✅ RAG 健康" || echo "❌ RAG 异常"

# 7. SSE 流式
curl -s -N -m 60 -H "token: $TOKEN" \
  "$BASE/api/chat/ask/stream?question=hello" | head -3

# 8. WebSocket 实时对话（需 Python websockets 库）
python -c "
import asyncio, json, os, websockets
async def main():
    uri = 'ws://127.0.0.1:8088/api/ws/chat?room=smoke&client=api-agent'
    async with websockets.connect(uri, additional_headers={'token': os.environ['TOKEN']}) as ws:
        print('welcome:', await ws.recv())
        await ws.send(json.dumps({'type': 'ping'}))
        print('pong:', await ws.recv())
asyncio.run(main())
"
```

> 冒烟脚本只做读取与校验类请求，不写入数据。
> 需要测试新增/编辑/删除时，请使用独立测试数据（如 `xxx@example.com`）并在用例结束后清理。

---

## 八、写入类用例的清理规范

实测过程中创建的测试数据已全部清理，库回到基线（customers=10，users=11）。

建议测试数据约定：

| 类型 | 建议值 | 清理方式 |
| --- | --- | --- |
| 客户 email | `apitester+<时间戳>@example.com` | `DELETE FROM customers WHERE email LIKE 'apitester%';` |
| 用户 email | `apireg+<时间戳>@example.com` | `DELETE FROM users WHERE email LIKE 'apireg%';` |

软删除只改状态不删行，**会残留 `inactive` 记录**，因此清理需直接操作 DB 或依赖 `down -v` 重建。

---

## 附：文档来源说明

- 接口定义：`crm-backend/src/main/java/com/elcrm/controller/{User,Customer,Chat}Controller.java`
- WebSocket 实时对话：`crm-backend/src/main/java/com/elcrm/config/WebSocketConfig.java`、`websocket/{ChatHandshakeInterceptor,ChatSessionInfo,ChatWebSocketHandler}.java`
- 入参约束：`dto/{LoginDTO,RegisterDTO,CustomerDTO,CustomerQueryDTO}.java`
- 鉴权：`interceptor/LoginInterceptor.java`、`config/WebConfig.java`、`util/JwtUtil.java`
- 异常包装：`common/GlobalExceptionHandler.java`、`common/Result.java`
- SQL：`resources/mapper/{UserMapper,CustomerMapper}.xml`
- 表结构与种子数据：`docker/mysql/init.sql`
- RAG：`crm-rag-fastAPI/{main.py,config.py,rag_system.py}`
- 代理配置：`crm-frontend/nginx.conf`
- 编排：`docker-compose.yml`

上述文件均与运行中容器镜像（2026-11-02 构建）一致，接口结论以 `curl` 实测为准。
