"""清理 UI 验证产生的测试数据（按名称/标记精确删除，不动真实数据）。"""
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

from apps.api_testing.models import (
    ApiProject, ApiCollection, ApiRequest, ApiSpecImport,
    GeneratedApiTestCase, TestSuite,
)
from apps.requirement_analysis.models import (
    RequirementDocument, FunctionalInventory, UIElementDescription, ClarificationItem,
)
from django.contrib.auth import get_user_model

U = get_user_model()
report = {}

# 1. openapi 导入记录（按名称）
n = ApiSpecImport.objects.filter(name='UI验证导入').delete()
report['ApiSpecImport'] = n

# 2. 临时项目 + 级联集合 + 其下请求 + 场景（按名称）
projs = ApiProject.objects.filter(name='UI验证临时项目')
req_deleted = 0
for proj in projs:
    cols = ApiCollection.objects.filter(project=proj)
    req_deleted += ApiRequest.objects.filter(collection__in=cols).delete()[0]
    cols.delete()
report['ApiRequest(in temp collections)'] = req_deleted
report['ApiProject(cascade)'] = projs.delete()

# 3. 需求三件套：删除 uiverify 名下临时文档（级联删其三个清单），避免污染真实文档
uiverify_docs = RequirementDocument.objects.filter(uploaded_by__username='uiverify')
for m in (FunctionalInventory, UIElementDescription, ClarificationItem):
    rep = m.objects.filter(document__in=uiverify_docs).delete()
    report[f'{m.__name__}(uiverify)'] = rep
report['RequirementDocument(uiverify)'] = uiverify_docs.delete()

# 4. uiverify 用户 + 其可能创建的草稿用例/场景
report['GeneratedApiTestCase(uiverify)'] = GeneratedApiTestCase.objects.filter(created_by__username='uiverify').delete()
report['TestSuite(uiverify)'] = TestSuite.objects.filter(created_by__username='uiverify').delete()
report['User(uiverify)'] = U.objects.filter(username='uiverify').delete()

# 5. 复核
report['remaining_spec_imports'] = ApiSpecImport.objects.count()
report['remaining_temp_projects'] = ApiProject.objects.filter(name='UI验证临时项目').count()
report['remaining_uiverify'] = U.objects.filter(username='uiverify').count()

for k, v in report.items():
    print(f'{k}: {v}')
