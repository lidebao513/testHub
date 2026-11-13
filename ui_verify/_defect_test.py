import django, os, json, urllib.request, urllib.error
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

BASE = "http://127.0.0.1:8900"

def api(method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req)
        body = r.read().decode()
        return r.status, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            b = e.read().decode()
            return e.code, json.loads(b) if b.strip() else {}
        except Exception:
            return e.code, {}

# 1) login
_, login = api("POST", "/api/auth/login/", body={"username": "e2etest", "password": "E2e@123456"})
tok = login.get("access")
print("login_token_len", len(tok) if tok else 0)

# 2) create project (owner=e2etest)
st, proj = api("POST", "/api/projects/", tok, {"name": "FULLREG_PROJ_临时", "description": "全量回归", "status": "active"})
print("project_create", st)
from apps.projects.models import Project
PID = Project.objects.filter(name="FULLREG_PROJ_临时").latest("id").id
print("project_id(from DB)=", PID)

# 3) create defect under it
st, df = api("POST", "/api/defects/defects/", tok,
            {"title": "FULLREG_TEST_缺陷", "description": "全量回归写入验证",
             "project_id": PID, "severity": "major", "priority": "p2", "module": "regression"})
print("defect_create", st, "id=", df.get("id"))
DID = df.get("id")

# 4) read back
st, _ = api("GET", f"/api/defects/defects/{DID}/", tok)
print("defect_read", st)

# 5) delete defect + project
st, _ = api("DELETE", f"/api/defects/defects/{DID}/", tok)
print("defect_delete", st)
st, _ = api("DELETE", f"/api/projects/{PID}/", tok)
print("project_delete", st)

# 6) residue cleanup + check (ORM delete any remaining test artifacts)
from apps.defects.models import Defect
from apps.projects.models import Project
Defect.objects.filter(title__contains="FULLREG_TEST").delete()
Project.objects.filter(name__contains="FULLREG_PROJ").delete()
print("RESIDUE defect=", Defect.objects.filter(title__contains="FULLREG_TEST").count(),
      "project=", Project.objects.filter(name__contains="FULLREG_PROJ").count())
