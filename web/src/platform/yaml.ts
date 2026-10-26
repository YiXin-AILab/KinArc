/** 手写 YAML 序列化与语法高亮，仅供配置预览使用。 */

type YamlValue =
  | string
  | number
  | boolean
  | null
  | YamlValue[]
  | { [key: string]: YamlValue }

function isPlainObject(value: unknown): value is Record<string, YamlValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const BARE_STRING_RE = /^[A-Za-z0-9_./${}-][A-Za-z0-9_./${} -]*$/
const RESERVED_WORDS = new Set(['true', 'false', 'null', 'yes', 'no', 'on', 'off', '~'])

function scalarToYaml(value: string | number | boolean | null): string {
  if (value === null) return 'null'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'null'
  if (
    value.length > 0 &&
    BARE_STRING_RE.test(value) &&
    !RESERVED_WORDS.has(value.toLowerCase()) &&
    Number.isNaN(Number(value))
  ) {
    return value
  }
  return JSON.stringify(value)
}

/**
 * 将纯数据（对象/数组/标量）序列化为块风格 YAML 文本。
 * undefined 与空对象/空数组按占位输出，未知类型按 JSON 字符串输出。
 */
export function toYaml(value: unknown, indent = 0): string {
  const pad = '  '.repeat(indent)
  if (Array.isArray(value)) {
    if (value.length === 0) return `${pad}[]`
    return value
      .map((item) => {
        if (isPlainObject(item) || Array.isArray(item)) {
          const nested = toYaml(item, indent + 1)
          return `${pad}-${nested.startsWith('  ') ? `\n${nested}` : ` ${nested.trimStart()}`}`
        }
        return `${pad}- ${scalarToYaml(item as string | number | boolean | null)}`
      })
      .join('\n')
  }
  if (isPlainObject(value)) {
    const entries = Object.entries(value).filter(([, v]) => v !== undefined)
    if (entries.length === 0) return `${pad}{}`
    return entries
      .map(([key, item]) => {
        if (isPlainObject(item) && Object.keys(item).length > 0) {
          return `${pad}${key}:\n${toYaml(item, indent + 1)}`
        }
        if (Array.isArray(item) && item.length > 0) {
          return `${pad}${key}:\n${toYaml(item, indent + 1)}`
        }
        if (isPlainObject(item) || Array.isArray(item)) {
          return `${pad}${key}: ${toYaml(item, 0).trim()}`
        }
        return `${pad}${key}: ${scalarToYaml(item as string | number | boolean | null)}`
      })
      .join('\n')
  }
  return `${pad}${scalarToYaml(value as string | number | boolean | null)}`
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 将 YAML 文本转为带 span 类名（yk/ys/yn/yc）的高亮 HTML。 */
export function highlightYaml(source: string): string {
  return source
    .split('\n')
    .map((line) => highlightLine(line))
    .join('\n')
}

function highlightLine(line: string): string {
  const commentIndex = findCommentIndex(line)
  const code = commentIndex >= 0 ? line.slice(0, commentIndex) : line
  const comment = commentIndex >= 0 ? line.slice(commentIndex) : ''
  let html = highlightCode(code)
  if (comment) html += `<span class="yc">${escapeHtml(comment)}</span>`
  return html
}

function findCommentIndex(line: string): number {
  let inSingle = false
  let inDouble = false
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index]!
    if (char === "'" && !inDouble) inSingle = !inSingle
    else if (char === '"' && !inSingle) inDouble = !inDouble
    else if (
      char === '#' &&
      !inSingle &&
      !inDouble &&
      (index === 0 || line[index - 1] === ' ')
    ) {
      return index
    }
  }
  return -1
}

function highlightCode(code: string): string {
  const keyMatch = /^(\s*-?\s*)([A-Za-z0-9_.-]+)(:)(.*)$/.exec(code)
  if (keyMatch) {
    const [, prefix, key, colon, rest] = keyMatch
    return `${escapeHtml(prefix!)}<span class="yk">${escapeHtml(key!)}</span>${colon}${highlightValue(rest!)}`
  }
  const itemMatch = /^(\s*-)(\s+.*)$/.exec(code)
  if (itemMatch) {
    return `${escapeHtml(itemMatch[1]!)}${highlightValue(itemMatch[2]!)}`
  }
  return highlightValue(code)
}

function highlightValue(text: string): string {
  const trimmed = text.trim()
  if (trimmed === '') return escapeHtml(text)
  const leading = text.slice(0, text.length - text.trimStart().length)
  const trailing = text.slice(text.trimEnd().length)
  let cls = 'ys'
  if (/^(true|false|null|~)$/.test(trimmed) || /^-?\d+(\.\d+)?$/.test(trimmed)) {
    cls = 'yn'
  }
  return `${escapeHtml(leading)}<span class="${cls}">${escapeHtml(trimmed)}</span>${escapeHtml(trailing)}`
}
