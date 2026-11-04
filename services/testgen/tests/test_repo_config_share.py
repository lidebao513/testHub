"""仓库配置共享测试：seed 播种 + 导出（不含凭证）+ 导入（幂等 upsert）。

#101「仓库配置共享（seed/导入导出）」的闭环测试。重点：
- 导出**绝不**带出 ``codeup_access_key`` 等凭证字段（安全红线，呼应 P0-1 凭据卫生）；
- 导入按 ``name`` 幂等 upsert，已存在则更新、不存在则新增；
- seed 幂等：仅当表空时播种，重复调用不重复插入。
"""

import pytest

from core import store


@pytest.fixture()
def clean_repos(fresh_db):
    """在 fresh_db 基础上额外清空 repo_configs（fresh_db 不清理该表）。"""
    conn = store.connect()
    conn.execute("DELETE FROM repo_configs")
    conn.commit()
    conn.close()
    yield


def test_seed_is_idempotent(clean_repos):
    """首次 seed 播种样例；再次 seed 不应重复插入。"""
    n1 = store.seed_repo_configs()
    assert n1 == 2
    rows = store.list_repo_configs()
    assert len(rows) == 2

    n2 = store.seed_repo_configs()
    assert n2 == 0, "表非空时 seed 必须幂等跳过"
    assert len(store.list_repo_configs()) == 2, "不得重复播种"


def test_export_omits_credentials(clean_repos):
    """导出只含 name/url/ref/remark；绝不含 codeup_access_key / id / 测试留痕。"""
    rid = store.create_repo_config(
        "repo-with-cred",
        "git@github.com:org/repo.git",
        ref="main",
        remark="含凭证的仓库",
        codeup_org_id="org-1",
        codeup_access_key="pt-SECRET_TOKEN_VALUE",
    )
    # 写入一条连接测试留痕，验证导出也不带
    store.record_repo_test(rid, True, "ok")

    exported = store.export_repo_configs()
    assert len(exported) == 1
    item = exported[0]

    assert set(item.keys()) == {"name", "url", "ref", "remark"}, (
        f"导出字段越界：{list(item.keys())}"
    )
    assert "codeup_access_key" not in item
    assert "codeup_org_id" not in item
    assert "id" not in item
    assert "last_test_ok" not in item
    # 凭证值绝不出现在导出任何字段里
    assert "pt-SECRET_TOKEN_VALUE" not in str(item)


def test_export_preserves_non_secret_fields(clean_repos):
    """导出应保留 name/url/ref/remark 原值。"""
    store.create_repo_config(
        "r1",
        "https://codeup.aliyun.com/ns/repo.git",
        ref="release/1.0",
        remark="发布仓",
    )
    item = store.export_repo_configs()[0]
    assert item == {
        "name": "r1",
        "url": "https://codeup.aliyun.com/ns/repo.git",
        "ref": "release/1.0",
        "remark": "发布仓",
    }


def test_import_idempotent_upsert(clean_repos):
    """导入：已存在 name 则更新，不存在则新增；返回 {imported, updated} 计数正确。"""
    # 先放一条旧配置
    store.create_repo_config("r1", "https://old.example.com/r1", ref="old-branch", remark="旧")

    items = [
        {"name": "r1", "url": "https://new.example.com/r1", "ref": "new-branch", "remark": "新"},
        {"name": "r2", "url": "https://new.example.com/r2", "ref": "", "remark": ""},
    ]
    res = store.import_repo_configs(items)
    assert res == {"imported": 1, "updated": 1}

    rows = {r["name"]: r for r in store.list_repo_configs()}
    assert rows["r1"]["url"] == "https://new.example.com/r1"
    assert rows["r1"]["ref"] == "new-branch"
    assert rows["r1"]["remark"] == "新"
    assert rows["r2"]["url"] == "https://new.example.com/r2"


def test_import_skips_incomplete_rows(clean_repos):
    """导入跳过缺 name 或 url 的行，不报错。"""
    items = [
        {"name": "ok", "url": "https://x/y"},
        {"url": "https://no-name"},  # 缺 name → 跳过
        {"name": "no-url"},  # 缺 url → 跳过
    ]
    res = store.import_repo_configs(items)
    assert res == {"imported": 1, "updated": 0}
    assert len(store.list_repo_configs()) == 1


def test_export_import_roundtrip_no_credential_leak(clean_repos):
    """往返：seed → 导出 → 清空 → 再导入，数量一致且全程无凭证进入导出物。"""
    store.seed_repo_configs()
    exported = store.export_repo_configs()
    assert len(exported) == 2

    # 清空后重新导入（模拟另一实例导入共享 JSON）
    conn = store.connect()
    conn.execute("DELETE FROM repo_configs")
    conn.commit()
    conn.close()

    res = store.import_repo_configs(exported)
    assert res["imported"] == 2
    assert res["updated"] == 0
    assert len(store.list_repo_configs()) == 2
    # 再次确认：导出物任何字段都不含凭证字样（样例本身无凭证，但管线必须保证）
    assert all(
        set(i.keys()) == {"name", "url", "ref", "remark"} for i in store.export_repo_configs()
    )
