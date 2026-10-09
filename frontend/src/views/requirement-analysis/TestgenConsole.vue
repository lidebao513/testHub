<template>
  <div class="testgen-console-wrap">
    <iframe
      ref="iframeRef"
      :src="consoleSrc"
      class="testgen-iframe"
      title="testgen 代码生成平台"
      @load="onIframeLoad"
    ></iframe>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const userStore = useUserStore()
const iframeRef = ref(null)

// 后端 Django 运行在 8000 端口；用当前浏览器主机名推导出 iframe 地址，
// 这样通过 localhost 或局域网 IP 访问 SPA 时都能正确指向后端。
// headless 嵌入契约：必须带 ?embed=1（隐藏被嵌页面自带导航/品牌头），
// zone 决定只渲染哪个功能区，由 SPA 二级菜单路由 query 驱动。
const consoleSrc = computed(() => {
  const protocol = window.location.protocol
  const hostname = window.location.hostname
  // 指向后端 Django(8000) 的 /testgen/ 反向代理（页面），其 API 走 /api/v1/ 代理；
  // 不能沿用 SPA 自身端口(3000)，否则 vite 没有该路由。
  const zone = route.query.zone || 'generate'
  return `${protocol}//${hostname}:8000/testgen/?embed=1&zone=${encodeURIComponent(zone)}`
})

// 把 SPA 已登录的 JWT 注入 iframe（console.html 监听 TESTGEN_AUTH_TOKEN）
function sendToken() {
  const iframe = iframeRef.value
  if (!iframe || !iframe.contentWindow) return
  if (userStore.accessToken) {
    iframe.contentWindow.postMessage(
      { type: 'TESTGEN_AUTH_TOKEN', token: userStore.accessToken },
      '*'
    )
  }
}

function onIframeLoad() {
  // iframe 文档加载完成后父页再发令牌，确保对方已注册 message 监听
  sendToken()
}

function onMessage(e) {
  // 子页宣告就绪后回送令牌（覆盖父页 onload 早于子页监听的时序情况）
  if (e.data && e.data.type === 'TESTGEN_CONSOLE_READY') {
    sendToken()
  }
}

onMounted(() => {
  window.addEventListener('message', onMessage)
})

onBeforeUnmount(() => {
  window.removeEventListener('message', onMessage)
})
</script>

<style scoped>
.testgen-console-wrap {
  width: 100%;
  height: 100%;
}
.testgen-iframe {
  width: 100%;
  height: calc(100vh - 100px);
  border: none;
  background: #fff;
  border-radius: 8px;
}
</style>
