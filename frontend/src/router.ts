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
      meta: { title: '接口调试' },
    },
    {
      path: '/plan',
      name: 'plan',
      component: () => import('@/views/PlanRunner.vue'),
      meta: { title: '用例计划' },
    },
    {
      path: '/long-connection',
      name: 'long-connection',
      component: () => import('@/views/LongConnection.vue'),
      meta: { title: '长连接测试' },
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
      component: () => import('@/views/RunsView.vue'),
      meta: { title: '模拟任务' },
    },
    {
      path: '/runs/:runId',
      name: 'run-detail',
      component: () => import('@/views/RunDetail.vue'),
      meta: { title: '任务详情' },
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
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFound.vue'),
      meta: { title: '页面不存在' },
    },
  ],
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '控制台'
  document.title = `${title} · API-Agent`
})
