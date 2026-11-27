/** Shared user-facing headings and common hints for the workbench surfaces. */
export const COPY = {
  app: { name: 'API 工作台', subtitle: '本地测试控制台' },
  navigation: {
    singleTests: '单点测试',
    intelligentTests: '智能群测',
    modelTesting: '大模型测试',
    records: '记录',
    settings: '设置',
    request: '接口测试',
    websocket: '长连接测试',
    executionResults: '执行结果',
    systemSettings: '系统设置',
  },
  assistant: { heading: '小Y', replyName: '小Y', inputPlaceholder: '向小Y提问' },
  modelSettings: {
    assistant: '小Y 对话模型',
    assistantUses: 'AI 对话、AI 总结',
    generation: '接口文档与 test 生成模型',
    follow: '跟随小Y',
    custom: '自定义模型',
    toggle: '自定义接口文档与 test 生成模型',
    required: '请选择或填写生成模型',
  },
  workbenchReset: {
    action: '重置本地设置',
    confirm: '清除当前站点的测试设置、布局、主题、配色、引擎选择、URL 前缀和应用缓存？大模型配置、测试历史和小Y对话将保留。',
    failed: '未能完成本地重置',
  },
  empty: {
    consoleResponse: '发送请求后在这里查看响应正文。',
    taskResults: '上传文档并开始测试，逐条结果和 AI 总结将在这里显示。',
  },
  errors: {
    historyLoad: '历史加载失败',
    requestHistoryLoad: '读取历史失败',
    taskStart: '启动失败',
  },
  feedback: {
    clearedCurrentResult: '已清除当前显示，可从测试历史重新查看。',
  },
} as const
