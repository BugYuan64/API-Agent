/** URL prefix storage and composition shared by all three test workbenches. */
export const URL_PREFIX_STORAGE = 'api-agent.url-prefixes'
const schemeOnly = /^(?:https?|wss?):\/\/$/i
const hostOnly = /^(?:https?|wss?):\/\/(?:\[[^\]]+\]|[^:/]+)$/i

/** Describe which parts are still editable after the selected prefix. */
export function urlSuffixPlaceholder(prefix: string): string {
  if (schemeOnly.test(prefix)) return '主机:端口/路径'
  return hostOnly.test(prefix) ? ':端口/路径（可留空）' : '/路径（可留空）'
}

export function normalizeUrlPrefix(input: string): string | null {
  const value = input.trim()
  if (schemeOnly.test(value)) return value.toLowerCase()
  if (!/^(https?|wss?):\/\/[^/]/i.test(value) || /[\s?#\\]/.test(value)) return null
  try {
    const parsed = new URL(value)
    if (parsed.username || parsed.password || !parsed.hostname || parsed.port === '0') return null
    const authority = value.split('/')[2]
    if (!authority || authority.includes('@')) return null
    const explicitPort = authority.match(/:(\d+)$/)?.[1]
    const port = explicitPort ? `:${Number(explicitPort)}` : ''
    return `${parsed.protocol}//${parsed.hostname}${port}${parsed.pathname.replace(/\/+$/, '')}`
  } catch {
    return null
  }
}

/** Convert a full address to an editable suffix when the user picks a prefix. */
export function urlSuffix(input: string, prefix: string, previousPrefix = ''): string {
  if (schemeOnly.test(previousPrefix) || schemeOnly.test(prefix)) {
    input = joinTargetUrl(previousPrefix, input) || input
  }
  const value = input.trim()
  const scheme = /^(https?|wss?):\/\//i.exec(value)
  if (!prefix || !scheme) return input
  if (schemeOnly.test(prefix)) return value.slice(scheme[0].length)
  if (value === prefix || value.startsWith(prefix + '/') || value.startsWith(prefix + '?') ||
    (value.startsWith(prefix + ':') && joinTargetUrl(prefix, value.slice(prefix.length)))) {
    return value.slice(prefix.length)
  }
  try {
    const parsed = new URL(value)
    return `${parsed.pathname === '/' ? '' : parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return input
  }
}

/** Join the address suffix. A host-only prefix can take an explicit port before its path. */
export function joinTargetUrl(prefix: string, input: string): string {
  const suffix = input.trim()
  if (!prefix) return suffix
  if (schemeOnly.test(prefix)) {
    if (!suffix || /^[/?#]/.test(suffix) || /^[a-z][a-z\d+.-]*:\/\//i.test(suffix) || /[\s\\]/.test(suffix)) return ''
    try {
      const url = new URL(prefix + suffix)
      return url.hostname && !url.username && !url.password && url.port !== '0' ? prefix + suffix : ''
    } catch {
      return ''
    }
  }
  if (!suffix || suffix.startsWith('?')) return prefix + suffix
  if (suffix.startsWith(':')) {
    const port = suffix.match(/^:(\d+)(?=[/?]|$)/)?.[1]
    return hostOnly.test(prefix) && port && Number(port) > 0 && Number(port) <= 65535 && !suffix.includes('\\')
      ? prefix + suffix : ''
  }
  if (suffix.startsWith('//') || /^[a-z][a-z\d+.-]*:/i.test(suffix) || suffix.includes('\\')) return ''
  return `${prefix}/${suffix.replace(/^\//, '')}`
}

export function readUrlPrefixes(storage: Pick<Storage, 'getItem'>): string[] {
  try {
    const saved: unknown = JSON.parse(storage.getItem(URL_PREFIX_STORAGE) ?? '[]')
    if (!Array.isArray(saved)) return []
    return [...new Set(saved.map(value => typeof value === 'string' ? normalizeUrlPrefix(value) : null)
      .filter((value): value is string => value !== null))].slice(0, 30)
  } catch {
    return []
  }
}
