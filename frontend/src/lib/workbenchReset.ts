/** Reset browser preferences without touching model settings or any saved test and chat records. */
const preferenceKeys = new Set([
  'api-agent.test-settings',
  'api-agent.settings.model_testing',
  'api-agent.console.settings',
  'api-agent.websocket.send-interval-ms',
  'api-agent.url-prefixes',
  'api-agent.engine',
  'api-agent.theme',
  'api-agent.sidebar-collapsed',
  'api-agent.palette',
])

export async function resetWorkbench(storage: Storage, caches?: CacheStorage): Promise<void> {
  if (caches) await Promise.all((await caches.keys()).map(key => caches.delete(key)))
  for (let index = storage.length - 1; index >= 0; index--) {
    const key = storage.key(index)
    if (key !== null && (preferenceKeys.has(key) || key.startsWith('api-agent.layout.'))) {
      storage.removeItem(key)
    }
  }
}
