"""清理 P0 前端验证产生的临时数据（按名称/用户名精确删除，不动真实数据）。

仅删除 ui_verify_p0 用户、P0VerifyProject 项目及其级联（集合/请求/草稿用例/场景）、
P0VerifyImport 导入记录；不触碰其它用户或项目。
"""
import django, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

from apps.api_testing.models import (
    ApiProject, ApiCollection, ApiRequest, ApiSpecImport,
    GeneratedApiTestCase, TestSuite,
)
from django.contrib.auth import get_user_model

U = get_user_model()
report = {}

# 1. 临时导入记录
report['ApiSpecImport(P0VerifyImport)'] = ApiSpecImport.objects.filter(name='P0VerifyImport').delete()

# 2. 临时项目 + 级联集合 + 请求 + 草稿用例 + 场景（按名称）
projs = ApiProject.objects.filter(name='P0VerifyProject')
req_deleted = 0
for proj in projs:
    cols = ApiCollection.objects.filter(project=proj)
    req_deleted += ApiRequest.objects.filter(collection__in=cols).delete()[0]
    cols.delete()
report['ApiRequest(in P0VerifyProject collections)'] = req_deleted
report['ApiProject(P0VerifyProject cascade)'] = projs.delete()

# 3. 兜底：ui_verify_p0 名下的草稿用例/场景（防止游离记录）
report['GeneratedApiTestCase(ui_verify_p0)'] = GeneratedApiTestCase.objects.filter(created_by__username='ui_verify_p0').delete()
report['TestSuite(ui_verify_p0)'] = TestSuite.objects.filter(created_by__username='ui_verify_p0').delete()

# 4. 删除临时用户
report['User(ui_verify_p0)'] = U.objects.filter(username='ui_verify_p0').delete()

# 5. 复核
report['remaining_P0VerifyProject'] = ApiProject.objects.filter(name='P0VerifyProject').count()
report['remaining_P0VerifyImport'] = ApiSpecImport.objects.filter(name='P0VerifyImport').count()
report['remaining_ui_verify_p0'] = U.objects.filter(username='ui_verify_p0').count()

for k, v in report.items():
    print(f'{k}: {v}')
print('CLEANUP DONE')
