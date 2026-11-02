import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/api'
import { track } from '@/utils/tracker'

export const useUserStore = defineStore('user', () => {
  // M8 安全改造：
  // - access token 仅内存态，绝不落 localStorage（页面刷新即失效，靠 refresh cookie 静默续期）。
  // - refresh token 由后端写入 HttpOnly + SameSite=Lax Cookie，浏览器同源自动携带，前端绝不持有，
  //   因此 XSS 无法窃取长效凭证。前端不再有任何 refresh/access 的 localStorage 持久化。
  const user = ref(null)
  const accessToken = ref('')
  const tokenExpiresAt = ref(0)

  // token 刷新定时器
  let refreshTimer = null

  const isAuthenticated = computed(() => !!accessToken.value && !!user.value)

  // 检查 token 是否即将过期（5 分钟内）
  const isTokenExpiringSoon = computed(() => {
    if (!tokenExpiresAt.value) return false
    return (tokenExpiresAt.value - Date.now()) < 5 * 60 * 1000
  })

  // 检查 token 是否已过期
  const isTokenExpired = computed(() => {
    if (!tokenExpiresAt.value) return false
    return Date.now() > tokenExpiresAt.value
  })

  // 启动自动刷新 token 定时器
  const startAutoRefresh = () => {
    if (refreshTimer) clearInterval(refreshTimer)
    // 每 2 分钟检查一次；refresh 由 HttpOnly cookie 提供，无需前端判断其存在。
    refreshTimer = setInterval(async () => {
      if (isTokenExpiringSoon.value && accessToken.value) {
        try {
          await refreshAccessToken()
        } catch (error) {
          console.error('自动刷新token失败:', error)
        }
      }
    }, 2 * 60 * 1000)
  }

  // 停止自动刷新定时器
  const stopAutoRefresh = () => {
    if (refreshTimer) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }
  }

  // 通过 HttpOnly refresh cookie 续期，前端无需也不持有 refresh token
  const refreshAccessToken = async () => {
    const response = await api.post('/auth/token/refresh/', {})
    accessToken.value = response.data.access
    tokenExpiresAt.value = Date.now() + 30 * 60 * 1000
    return response.data.access
  }

  // 统一写入登录态（access 仅内存态；refresh 已由后端种入 HttpOnly Cookie）
  const _applyAuthState = (data) => {
    accessToken.value = data.access || ''
    user.value = data.user || null
    tokenExpiresAt.value = Date.now() + 30 * 60 * 1000
  }

  const login = async (credentials) => {
    try {
      const response = await api.post('/auth/login/', credentials)
      _applyAuthState(response.data)
      startAutoRefresh()
      track('login_success', {
        event_type: 'business',
        module: 'auth',
        page_path: '/login',
        success: true,
        metadata: { login_type: 'password' },
      })
      return response.data
    } catch (error) {
      throw error
    }
  }

  const smsLogin = async (data) => {
    try {
      const response = await api.post('/auth/sms-login/', data)
      _applyAuthState(response.data)
      startAutoRefresh()
      track('login_success', {
        event_type: 'business',
        module: 'auth',
        page_path: '/login',
        success: true,
        metadata: { login_type: 'sms' },
      })
      return response.data
    } catch (error) {
      throw error
    }
  }

  const register = async (userData) => {
    try {
      // 注册改走正式接口 /api/auth/register/（原 test-register 已废弃）。
      // 后端返回 {success, user, access, refresh}；refresh 已种入 HttpOnly Cookie，前端忽略它。
      const response = await api.post('/auth/register/', userData)
      _applyAuthState(response.data)
      startAutoRefresh()
      track('register_success', {
        event_type: 'business',
        module: 'auth',
        page_path: '/register',
        success: true,
      })
      return response.data
    } catch (error) {
      throw error
    }
  }

  // 添加标记防止 logout 过程中的循环调用
  let isLoggingOut = false

  const logout = async () => {
    if (isLoggingOut) return
    isLoggingOut = true
    stopAutoRefresh()
    try {
      // refresh token 已不在前端；后端从 HttpOnly cookie 读取并拉黑，这里无需传 body。
      // 仅当 access 未过期时尝试调用 logout API，过期则直接清本地态避免 401 死循环。
      if (accessToken.value && !isTokenExpired.value) {
        try {
          await api.post('/auth/logout/', {})
        } catch (apiError) {
          console.error('Logout API调用失败:', apiError)
        }
      }
    } finally {
      accessToken.value = ''
      user.value = null
      tokenExpiresAt.value = 0
      // M8：不再清除 localStorage 中的 token（本就未持久化），仅清理展示用 user。
      localStorage.removeItem('user')
      isLoggingOut = false
      window.location.href = '/login'
    }
  }

  const fetchUser = async () => {
    try {
      const response = await api.get('/users/me/')
      user.value = response.data
      localStorage.setItem('user', JSON.stringify(user.value))
    } catch (error) {
      await logout()
      throw error
    }
  }

  const fetchProfile = async () => {
    try {
      const response = await api.get('/auth/profile/')
      user.value = response.data
      localStorage.setItem('user', JSON.stringify(user.value))
      return response.data
    } catch (error) {
      if (error.response?.status === 401) {
        await logout()
      }
      throw error
    }
  }

  const initAuth = async () => {
    // M8：仅从 localStorage 恢复展示用 user（非凭证）。
    if (!user.value) {
      const savedUser = localStorage.getItem('user')
      if (savedUser) {
        try {
          user.value = JSON.parse(savedUser)
        } catch (e) {
          console.error('解析用户信息失败:', e)
        }
      }
    }

    if (accessToken.value) {
      // 内存中仍有 access：过期则刷新，否则直接拉取用户信息
      if (isTokenExpired.value) {
        try {
          await refreshAccessToken()
        } catch (error) {
          console.error('Token刷新失败:', error)
          return
        }
      }
      if (!user.value) {
        try {
          await fetchProfile()
        } catch (error) {
          console.error('获取用户信息失败:', error)
          await logout()
        }
      }
      startAutoRefresh()
    } else {
      // M8：页面刷新后内存 access 已丢失，无条件尝试用 HttpOnly refresh cookie 静默续期。
      // 不依赖 localStorage 中是否有 user——refresh cookie 是恢复登录态的唯一可信来源。
      try {
        await refreshAccessToken()
        if (!user.value) {
          try {
            await fetchProfile()
          } catch (error) {
            console.error('获取用户信息失败:', error)
            await logout()
            return
          }
        }
        startAutoRefresh()
      } catch (error) {
        // 无 refresh cookie 或已过期：保持未登录，无需处理
      }
    }
  }

  return {
    user,
    accessToken,
    tokenExpiresAt,
    isAuthenticated,
    isTokenExpiringSoon,
    isTokenExpired,
    login,
    smsLogin,
    register,
    logout,
    refreshAccessToken,
    fetchProfile,
    initAuth,
    startAutoRefresh,
    stopAutoRefresh,
  }
})
