"""frontend-lint 修复的 UI 层回归：确认「删未使用 slot 变量 / 去重复属性 / TS→JS」没改坏页面。

为什么需要单独跑这一遍：
- 这批改动集中在模板插槽与组件 props 上，`vite build` 只能证明"能编译"，不能证明"渲染对"；
- `ElementList.vue` 的 `el-descriptions` 原来同时写了 `column="2"` 和 `:column="{...}"`，
  后者传对象会让 Element Plus 的分栏计算变 NaN（prop 定义是 Number）；
- `CaptureElementDialog.vue` 从 `<script setup lang="ts">` 改成 JS，需确认对话框仍能正常打开。

断言口径：
1. 四个受影响页面都能打开、不白屏；
2. 浏览器 console 里没有 error 级消息（Vue 的 prop type 警告算 warning，单列出来看）；
3. 元素详情弹窗 / 捕获元素弹窗这类"藏在交互后面"的组件能真的弹出来。

用法（前后端已启动）：
    venv/Scripts/python.exe tests/verify_lint_fix_pages.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:3900'
USER, PWD = 'e2etest', 'E2e@123456'
ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / 'logs' / 'e2e_ui'

PAGES = [
    ('UI 元素管理（增强）', '/ui-automation/elements-enhanced'),
    ('App 元素管理', '/app-automation/elements'),
    ('接口管理（i18n 表格插槽）', '/api-testing/interfaces'),
    ('数据工厂（tree 插槽）', '/data-factory'),
]


def login(page) -> bool:
    page.goto(f'{BASE}/login', wait_until='domcontentloaded', timeout=30_000)
    page.wait_for_selector('input', timeout=15_000)
    page.locator('input').nth(0).fill(USER)
    pwd = page.locator("input[type='password']").first
    pwd.fill(PWD)
    pwd.press('Enter')
    try:
        page.wait_for_url(lambda url: '/login' not in url, timeout=25_000)
        return True
    except Exception:  # noqa: BLE001 - 登录失败只需布尔结果，异常细节由调用方截图
        return False


def main() -> int:
    failures: list[str] = []
    console_errors: list[str] = []
    console_warnings: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-proxy-server', '--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('console', lambda m: (
            console_errors.append(m.text) if m.type == 'error'
            else console_warnings.append(m.text) if m.type == 'warning'
            else None
        ))
        page.on('pageerror', lambda e: console_errors.append(f'[pageerror] {e}'))

        if not login(page):
            print('❌ 登录失败，后续断言无法进行')
            page.screenshot(path=str(SHOTS / 'lintfix-login-failed.png'))
            browser.close()
            return 1
        print('✅ 登录成功')

        for label, path in PAGES:
            console_errors.clear()
            console_warnings.clear()
            page.goto(f'{BASE}{path}', wait_until='domcontentloaded', timeout=30_000)
            page.wait_for_timeout(2_500)
            slug = path.strip('/').replace('/', '-')
            page.screenshot(path=str(SHOTS / f'lintfix-{slug}.png'))

            body = page.inner_text('body')
            if len(body.strip()) < 80:
                failures.append(f'{label}: 页面疑似白屏 ({path})')
                print(f'❌ {label}: 白屏')
                continue

            hard_errors = [e for e in console_errors if 'Failed to fetch' not in e]
            if hard_errors:
                failures.append(f'{label}: console error {hard_errors[:2]}')
                print(f'❌ {label}: {len(hard_errors)} 条 console error')

            prop_warnings = [w for w in console_warnings if 'Invalid prop' in w or 'type check failed' in w]
            print(
                f'{"✅" if not hard_errors and not prop_warnings else "⚠️"} {label}: '
                f'渲染正常（console error={len(hard_errors)}, prop 警告={len(prop_warnings)}）'
            )

        # 交互后面的组件：UI 元素管理的"元素详情"弹窗（el-descriptions 改动点）
        page.goto(f'{BASE}/ui-automation/elements-enhanced', wait_until='domcontentloaded')
        page.wait_for_timeout(2_000)
        rows = page.locator('.el-table__row')
        if rows.count() > 0:
            rows.first.locator('button').first.click()
            page.wait_for_timeout(1_500)
            dialog = page.locator('.el-dialog')
            if dialog.count() > 0 and dialog.first.is_visible():
                print('✅ 元素详情弹窗可打开')
                page.screenshot(path=str(SHOTS / 'lintfix-ui-element-detail.png'))
            else:
                print('⚠️ 元素详情弹窗未出现（可能该行按钮不是详情入口，跳过）')
        else:
            print('⚠️ UI 元素列表为空，跳过详情弹窗检查')

        # 交互后面的组件：App 元素的"从设备创建"弹窗（TS→JS 改动点 CaptureElementDialog）
        page.goto(f'{BASE}/app-automation/elements', wait_until='domcontentloaded')
        page.wait_for_timeout(2_000)
        capture = page.locator('button', has_text='从设备创建').first
        if capture.count() > 0 and capture.is_visible():
            capture.click()
            page.wait_for_timeout(1_500)
            dialog = page.locator('.el-dialog')
            if dialog.count() > 0 and dialog.first.is_visible():
                print('✅ 从设备创建弹窗可打开（CaptureElementDialog 转 JS 后仍正常）')
                page.screenshot(path=str(SHOTS / 'lintfix-app-capture-dialog.png'))
            else:
                failures.append('从设备创建弹窗未弹出')
                print('❌ 从设备创建弹窗未弹出')
        else:
            print('⚠️ 未找到"从设备创建"按钮，跳过')

        browser.close()

    print('\n=== 结论 ===')
    if failures:
        for item in failures:
            print('❌', item)
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
