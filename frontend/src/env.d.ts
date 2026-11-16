/** 构建期类型入口：引入 Vite 客户端声明并描述 Vue 单文件组件导入。仅供 TypeScript 检查使用，不提供运行时组件或校验。 */
/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, unknown>
  export default component
}
