/* ESLint 配置（此前缺失，导致 `npm run lint` 无法解析 .vue / ESM，全员 203 解析错误）。
 *
 * 职责划分（重要）：
 * - ESLint 只管**正确性**：未使用变量、ref 忘了 .value、重复属性、模板写错。
 * - Prettier 只管**格式**：缩进、引号、换行。配置见 `.prettierrc.json`，
 *   命令见 package.json 的 `format` / `format:check`。
 *
 * 为什么不把 prettier 挂成 ESLint 规则：全仓 204 个文件里有 162 个从未按 prettier
 * 排过版（`prettier --check` 实测），挂成规则会一次性产出约 1 万条 warn 把真实
 * 错误淹掉，而且在 CI 里 `--fix` 会静默改写源码。Prettier 官方也明确不建议这么用。
 * 存量格式待专项批次用 `npm run format` 一次性收敛。
 */
module.exports = {
  root: true,
  parser: 'vue-eslint-parser',
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'module'
  },
  env: {
    browser: true,
    node: true,
    es2022: true
  },
  extends: ['plugin:vue/vue3-recommended', '@vue/eslint-config-prettier'],
  rules: {
    // 见文件头：格式交给 Prettier CLI，不占 ESLint 通道
    'prettier/prettier': 'off',
    // 历史组件多为单单词（Login/Home 等），不强约束多词命名
    'vue/multi-word-component-names': 'off',
    // 富文本/HTML 渲染场景使用 v-html，按安全审查放行（已有 DOMPurify 净化）
    'vue/no-v-html': 'off'
  }
}
