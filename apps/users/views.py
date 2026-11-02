from rest_framework import generics, status, permissions
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.response import Response
from django.contrib.auth import login
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from .models import User
from .serializers import UserSerializer, UserCreateSerializer, LoginSerializer
from .permissions import IsOwnerOrAdmin

# JWT 相关导入
from rest_framework_simplejwt.tokens import RefreshToken
from django.conf import settings

# 图形验证码 & 短信验证码
from .captcha import create_captcha
from .sms import send_register_verify_code
from . import sms as _sms_module
from .redis_client import get_redis
from .token_exchange import decode_exchange_user_id, encode_exchange_user_id
import re
import uuid
import logging

logger = logging.getLogger(__name__)
_GENERIC_ERROR = '注册失败，请稍后重试或联系管理员'

# --------------------------------------------------------------------------- #
# refresh_token 的 HttpOnly Cookie 处理（M8：绝不把 refresh 存 localStorage）
# --------------------------------------------------------------------------- #
_REFRESH_COOKIE = 'refresh_token'
_REFRESH_MAX_AGE = 14 * 24 * 3600  # 与 simplejwt refresh 生命周期对齐


def set_refresh_cookie(response, refresh_token_str):
    """把 refresh token 写入 HttpOnly + SameSite=Lax 的 Cookie。

    - secure：生产（DEBUG=False）强制 HTTPS；本地 HTTP 开发不要求 secure。
    - JS 无法读取该 Cookie，XSS 偷不到长效凭证；浏览器在同源请求自动携带。
    """
    response.set_cookie(
        _REFRESH_COOKIE,
        refresh_token_str,
        max_age=_REFRESH_MAX_AGE,
        httponly=True,
        secure=not settings.DEBUG,
        samesite='Lax',
        path='/',
    )


def clear_refresh_cookie(response):
    response.delete_cookie(_REFRESH_COOKIE, path='/')


def _validate_phone(phone):
    if not phone:
        return False, '请输入手机号'
    if not re.match(r'^1[3-9]\d{9}$', phone):
        return False, '手机号格式不正确'
    return True, ''


def _flatten_errors(errors):
    """把 DRF 的 errors（可能嵌套）压平成一句可读文案。"""
    messages = []
    stack = [errors]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('non_field_errors', '__all__'):
                    stack.insert(0, value)
                else:
                    sub = _flatten_errors(value) if isinstance(value, (dict, list)) else str(value)
                    messages.append(f'{key}: {sub}')
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        else:
            messages.append(str(node))
    return '；'.join(m for m in messages if m) or _GENERIC_ERROR


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_current_user(request):
    serializer = UserSerializer(request.user)
    return Response(serializer.data)


@method_decorator(csrf_exempt, name='dispatch')
class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all().order_by('username')
    serializer_class = UserCreateSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        data = request.data if isinstance(request.data, dict) else {}

        # ---- 1) 手机号 + 短信验证码前置校验（保留防滥用闸门） ----
        phone = str(data.get('phone') or '').strip()
        verify_code = str(data.get('verify_code') or '').strip()
        verify_code_token = str(data.get('verify_code_token') or '').strip()

        ok, phone_error = _validate_phone(phone)
        if not ok:
            return Response({'success': False, 'error': phone_error}, status=status.HTTP_400_BAD_REQUEST)
        if not verify_code or not verify_code_token:
            return Response({'success': False, 'error': '请输入短信验证码'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            valid, code_error = _sms_module.validate_verify_code(phone, verify_code_token, verify_code)
        except Exception:
            logger.exception('短信验证码校验异常 phone=%s', phone)
            return Response({'success': False, 'error': _GENERIC_ERROR}, status=status.HTTP_400_BAD_REQUEST)
        if not valid:
            return Response({'success': False, 'error': code_error or '验证码错误'}, status=status.HTTP_400_BAD_REQUEST)

        # ---- 2) 字段校验委托正式序列化器（不再自建账号绕过校验） ----
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'success': False, 'error': _flatten_errors(serializer.errors)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = serializer.save()

        # ---- 3) 签发 JWT 并种 HttpOnly refresh Cookie ----
        refresh = RefreshToken.for_user(user)
        access = str(refresh.access_token)
        resp = Response({
            'success': True,
            'user': UserSerializer(user).data,
            'access': access,
            'refresh': str(refresh),
        }, status=status.HTTP_201_CREATED)
        set_refresh_cookie(resp, str(refresh))
        return resp

@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def login_view(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.validated_data['user']
    login(request, user)

    # JWT Token (优先使用JWT)
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)

    resp = Response({
        'user': UserSerializer(user).data,
        'access': access_token,       # JWT access token
        'refresh': refresh_token,     # JWT refresh token（同时种 HttpOnly Cookie）
        'message': '登录成功'
    })
    set_refresh_cookie(resp, refresh_token)
    return resp

@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@authentication_classes([])
@csrf_exempt
def logout_view(request):
    """用户退出登录：将 refresh token 加入黑名单（若能从 body 或 HttpOnly Cookie 取得），并清除 Cookie。"""
    refresh = request.data.get('refresh') or request.COOKIES.get(_REFRESH_COOKIE)
    if refresh:
        try:
            RefreshToken(refresh).blacklist()
        except Exception:
            pass

    resp = Response({'message': '退出成功'})
    clear_refresh_cookie(resp)
    return resp


# --------------------------------------------------------------------------- #
# refresh token 续期（M8）：从 HttpOnly Cookie 读取 refresh，前端无需持有它。
# 禁用会话认证以避免 CSRF；兼容 body 中显式传入 refresh（移动端等）。
# --------------------------------------------------------------------------- #
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@authentication_classes([])
def cookie_token_refresh(request):
    refresh = request.data.get('refresh') or request.COOKIES.get(_REFRESH_COOKIE)
    if not refresh:
        return Response({'error': '未登录或会话已过期'}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        token = RefreshToken(refresh)
        access = str(token.access_token)
    except Exception:
        return Response({'error': '会话已过期，请重新登录'}, status=status.HTTP_401_UNAUTHORIZED)
    return Response({'access': access})

@api_view(['GET'])
def profile_view(request):
    if not request.user.is_authenticated:
        return Response({'error': '未登录'}, status=status.HTTP_401_UNAUTHORIZED)
    
    serializer = UserSerializer(request.user)
    return Response(serializer.data)

class UserListView(generics.ListCreateAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsOwnerOrAdmin]

    def get_queryset(self):
        # H6：非管理员仅能列出自己，避免任意登录用户枚举全部账号。
        qs = User.objects.all().order_by('username')
        if not self.request.user.is_staff:
            qs = qs.filter(id=self.request.user.id)
        return qs


class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all().order_by('username')
    serializer_class = UserSerializer
    permission_classes = [IsOwnerOrAdmin]


# ========== 图形验证码 & 短信验证码 ==========

@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def get_captcha(request):
    """获取图形验证码"""
    result = create_captcha()
    return Response(result)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def send_register_code(request):
    """发送短信验证码（注册/登录通用）"""
    phone = request.data.get('phone', '').strip()
    captcha_token = request.data.get('captcha_token', '')
    captcha_code = request.data.get('captcha_code', '')
    mode = request.data.get('mode', 'register')  # 'register' 或 'login'

    if not phone:
        return Response({'success': False, 'error': '请输入手机号'}, status=status.HTTP_400_BAD_REQUEST)
    if not captcha_token or not captcha_code:
        return Response({'success': False, 'error': '请输入图形验证码'}, status=status.HTTP_400_BAD_REQUEST)

    # 登录模式：校验手机号是否已注册
    if mode == 'login':
        if not User.objects.filter(phone=phone).exists():
            return Response({'success': False, 'error': '该手机号未注册，请先注册'}, status=status.HTTP_400_BAD_REQUEST)

    # 注册模式：校验手机号未被注册
    if mode == 'register':
        if User.objects.filter(phone=phone).exists():
            return Response({'success': False, 'error': '该手机号已被注册'}, status=status.HTTP_400_BAD_REQUEST)

    result = send_register_verify_code(phone, captcha_token, captcha_code)

    if result.get('success'):
        return Response(result)
    else:
        return Response(result, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def sms_login_view(request):
    """短信验证码登录：校验短信码 → 返回 JWT（未注册提示用户先注册）"""
    phone = request.data.get('phone', '').strip()
    verify_code = request.data.get('verify_code', '').strip()
    verify_code_token = request.data.get('verify_code_token', '').strip()

    # 校验手机号
    if not phone:
        return Response({'error': '请输入手机号'}, status=status.HTTP_400_BAD_REQUEST)
    if not re.match(r'^1[3-9]\d{9}$', phone):
        return Response({'error': '手机号格式不正确'}, status=status.HTTP_400_BAD_REQUEST)

    # 校验短信验证码
    if not verify_code or not verify_code_token:
        return Response({'error': '请输入短信验证码'}, status=status.HTTP_400_BAD_REQUEST)

    valid, error = validate_verify_code(phone, verify_code_token, verify_code)
    if not valid:
        return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

    # 查找用户
    try:
        user = User.objects.get(phone=phone)
    except User.DoesNotExist:
        return Response({'error': '该手机号未注册，请先注册'}, status=status.HTTP_400_BAD_REQUEST)

    if not user.is_active:
        return Response({'error': '用户已被禁用'}, status=status.HTTP_400_BAD_REQUEST)

    # 生成 JWT
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)

    resp = Response({
        'user': UserSerializer(user).data,
        'access': access_token,
        'refresh': refresh_token,
        'message': '登录成功',
    })
    set_refresh_cookie(resp, refresh_token)
    return resp


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def exchange_token_view(request):
    """
    一次性授权码交换
    action=create:  用当前 JWT 换取一次性 code（需登录），60 秒有效
    action=redeem:  用 code 换取 JWT（无需登录），一次性使用
    """
    action = request.data.get('action', '')
    redis_client = get_redis()

    if action == 'create':
        if not request.user.is_authenticated:
            return Response({'error': '未登录'}, status=status.HTTP_401_UNAUTHORIZED)
        code = str(uuid.uuid4()).replace('-', '')[:8]
        # M4 修复：统一以字符串落库，避免 int/bytes 混用导致 redeem 阶段 500。
        redis_client.setex(f"exchange:{code}", 60, encode_exchange_user_id(request.user.id))
        return Response({'code': code})

    elif action == 'redeem':
        code = request.data.get('code', '').strip()
        if not code:
            return Response({'error': '缺少授权码'}, status=status.HTTP_400_BAD_REQUEST)

        raw = redis_client.get(f"exchange:{code}")
        if raw is None:
            return Response({'error': '授权码无效或已过期'}, status=status.HTTP_400_BAD_REQUEST)

        # 立即删除，保证一次性使用
        redis_client.delete(f"exchange:{code}")

        # M4 修复：Redis get 可能返回 bytes（decode_responses=False）或 str，
        # 统一解码后转 int；非法值转 400 而非 500。
        try:
            user_id = decode_exchange_user_id(raw)
        except ValueError:
            return Response({'error': '授权码无效或已过期'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'error': '用户不存在'}, status=status.HTTP_400_BAD_REQUEST)

        if not user.is_active:
            return Response({'error': '用户已被禁用'}, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        resp = Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserSerializer(user).data,
        })
        set_refresh_cookie(resp, str(refresh))
        return resp

    else:
        return Response({'error': '无效的 action'}, status=status.HTTP_400_BAD_REQUEST)