/* ESLint 配置（此前缺失，导致 `npm run lint` 无法解析 .vue / ESM，全员 203 解析错误）。
 * 作用：让 lint 真正可用（vue-eslint-parser 解析 SFC + espree 解析 ESM）。
 * 规则以 vue3-recommended 为基线，并接入 @vue/eslint-config-prettier 关闭与 Prettier 冲突项。
 */
module.exports = {
  root: true,
  parser: "vue-eslint-parser",
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: "module",
  },
  env: {
    browser: true,
    node: true,
    es2022: true,
  },
  extends: ["plugin:vue/vue3-recommended", "@vue/eslint-config-prettier"],
  rules: {
    // 历史组件多为单单词（Login/Home 等），不强约束多词命名
    "vue/multi-word-component-names": "off",
    // 富文本/HTML 渲染场景使用 v-html，按安全审查放行（已有 DOMPurify 净化）
    "vue/no-v-html": "off",
  },
};
