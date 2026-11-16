/** 浏览器应用入口：创建 Vue 实例，注册 Pinia 与路由并加载全局样式，挂载到 #app。依赖宿主页提供挂载节点；页面请求和失败反馈由各模块负责。 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import './styles/tokens.css'
import './styles/base.css'

createApp(App).use(createPinia()).use(router).mount('#app')
