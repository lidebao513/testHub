import DOMPurify from 'dompurify'
import { marked } from 'marked'

// M6/M7：所有 v-html / Markdown 渲染必须经过 DOMPurify 白名单净化，阻断 XSS。
// 配置说明：
// - USE_PROFILES.html：仅允许安全 HTML 子集（标题/段落/列表/表格/链接/代码等）。
// - ADD_ATTR：保留链接 target（外链新标签）与 style（JSON 高亮配色），DOMPurify 会进一步净化 CSS。
const PURIFY_CONFIG = {
  USE_PROFILES: { html: true },
  ADD_ATTR: ['target', 'style'],
}

// 净化任意 HTML 字符串。所有直接 v-html 原始字段的地方都须经此函数。
export function sanitizeHtml(html) {
  if (html == null) return ''
  return DOMPurify.sanitize(String(html), PURIFY_CONFIG)
}

// Markdown → HTML 并净化。marked 本身不做 XSS 防护，必须先 sanitize 再交给 v-html。
export function renderMarkdown(src) {
  if (!src) return ''
  const raw = marked.parse(String(src))
  return DOMPurify.sanitize(raw, PURIFY_CONFIG)
}
