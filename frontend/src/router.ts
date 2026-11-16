/** 前端页面路由表与标题同步：依赖 Vue Router，延迟加载页面并兼容旧入口。history 路由刷新依赖服务端 SPA 回落；此处不处理页面数据请求。 */
import { createRouter, createWebHistory } from 'vue-router'

/** 路由即页面。标题写进 meta 供顶栏与 document.title 使用。
 *  采用 history 模式：后端已为未匹配路径回落 index.html（见 main.py 的 SPA 处理）。 */
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/request' },
    {
      path: '/request',
      name: 'request',
      component: () => import('@/views/RequestConsole.vue'),
      meta: { title: '接口测试' },
    },
    {
      path: '/testcases',
      name: 'testcases',
      component: () => import('@/views/TestCasesView.vue'),
      meta: { title: '用例测试' },
    },
    // 旧入口统一进入文档驱动任务测试。
    { path: '/plan', redirect: '/testcases' },
    { path: '/agent', redirect: '/testcases' },
    {
      path: '/long-connection',
      name: 'long-connection',
      component: () => import('@/views/LongConnection.vue'),
      meta: { title: 'WebSocket 调试' },
    },
    {
      path: '/projects',
      name: 'projects',
      component: () => import('@/views/ProjectsView.vue'),
      meta: { title: '项目与环境' },
    },
    {
      path: '/runs',
      name: 'runs',
      redirect: '/projects',
    },
    {
      path: '/runs/:runId',
      name: 'run-detail',
      redirect: '/projects',
    },
    {
      path: '/history',
      name: 'history',
      component: () => import('@/views/HistoryView.vue'),
      meta: { title: '请求历史' },
    },
    {
      path: '/executions',
      name: 'executions',
      component: () => import('@/views/ExecutionsView.vue'),
      meta: { title: '执行结果' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
      meta: { title: '系统设置' },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFound.vue'),
      meta: { title: '页面不存在' },
    },
  ],
})

/** 导航完成后按目标路由 meta 更新文档标题；缺少标题时回退控制台，不更改导航结果。 */
router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '控制台'
  document.title = `${title} · API-Agent`
})
