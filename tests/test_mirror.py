"""mirror-preview：镜像同步预演测试（操作序列规划，纯内存）。"""

import unicodedata

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/mirror-preview", json=payload)


def _mirror(existing, incoming):
    return _post({"existing": existing, "incoming": incoming})


def _actions(resp):
    """[(action, canonical_path)]，按返回顺序。"""
    return [(op["action"], op["canonical_path"]) for op in resp["operations"]]


# ---------------------------------------------------------------------------
# 基本流程：删除 → 建目录 → 写文件
# ---------------------------------------------------------------------------


def test_full_sync_flow_and_summary():
    resp = _mirror(
        existing=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "assets", "type": "directory"},
            {"path": "old/junk.bin", "type": "file"},
        ],
        incoming=[
            {"path": "docs/readme.txt", "type": "file"},  # 覆盖现存文件
            {"path": "assets", "type": "directory"},  # 保留现存目录
            {"path": "assets/logo.png", "type": "file"},  # 新文件
            {"path": "new", "type": "directory"},  # 新目录
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "windows"
    assert _actions(body) == [
        ("DELETE", "old/junk.bin"),
        ("DELETE", "old"),  # 隐含目录随之一并删除（先子后父）
        ("KEEP_DIRECTORY", "assets"),
        ("KEEP_DIRECTORY", "docs"),  # 期望清单未列出的父目录：隐含保留
        ("CREATE_DIRECTORY", "new"),
        ("OVERWRITE_FILE", "docs/readme.txt"),
        ("CREATE_FILE", "assets/logo.png"),
    ]
    assert [op["step"] for op in body["operations"]] == list(range(1, 8))
    assert body["summary"] == {
        "total": 7,
        "delete": 2,
        "create_directory": 1,
        "keep_directory": 2,
        "create_file": 1,
        "overwrite_file": 1,
    }


def test_operations_carry_source_index_and_keys():
    resp = _mirror(
        existing=[
            {"path": "Docs\\ReadMe.txt", "type": "file"},
            {"path": "stale.bin", "type": "file"},
        ],
        incoming=[{"path": "docs/readme.TXT", "type": "file"}],
    )
    ops = {op["action"]: op for op in resp.json()["operations"]}
    delete = ops["DELETE"]
    assert delete["type"] == "file"
    assert delete["canonical_path"] == "stale.bin"
    assert delete["comparison_key"] == "stale.bin"
    assert delete["source"] == "existing"
    assert delete["index"] == 2  # 现存清单下标（从 1 开始）
    keep = ops["KEEP_DIRECTORY"]
    assert keep["canonical_path"] == "docs"
    assert keep["source"] == "implicit"  # 期望清单未列出的父目录
    assert keep["index"] is None
    overwrite = ops["OVERWRITE_FILE"]
    assert overwrite["canonical_path"] == "docs/readme.TXT"
    assert overwrite["comparison_key"] == "docs/readme.txt"
    assert overwrite["source"] == "incoming"
    assert overwrite["index"] == 1  # 期望清单下标（从 1 开始）


def test_delete_phase_deep_first_then_key_order():
    resp = _mirror(
        existing=[
            {"path": "b/y.txt", "type": "file"},
            {"path": "a/deep/x.txt", "type": "file"},
            {"path": "c.txt", "type": "file"},
        ],
        incoming=[{"path": "keep.txt", "type": "file"}],
    )
    assert _actions(resp.json()) == [
        ("DELETE", "a/deep/x.txt"),  # 深度 3 最先
        ("DELETE", "a/deep"),  # 深度 2，同深度按比较键 a/deep < b/y.txt
        ("DELETE", "b/y.txt"),
        ("DELETE", "a"),  # 深度 1，同深度按比较键 a < b < c.txt
        ("DELETE", "b"),
        ("DELETE", "c.txt"),
        ("CREATE_FILE", "keep.txt"),
    ]


def test_mkdir_phase_shallow_first_then_key_order():
    resp = _mirror(
        existing=[],
        incoming=[
            {"path": "z/deep/f.txt", "type": "file"},
            {"path": "a", "type": "directory"},
            {"path": "m", "type": "directory"},
        ],
    )
    assert _actions(resp.json()) == [
        ("CREATE_DIRECTORY", "a"),  # 深度 1，同深度按比较键 a < m < z
        ("CREATE_DIRECTORY", "m"),
        ("CREATE_DIRECTORY", "z"),
        ("CREATE_DIRECTORY", "z/deep"),  # 深度 2
        ("CREATE_FILE", "z/deep/f.txt"),
    ]


def test_files_written_in_incoming_order_not_sorted():
    resp = _mirror(
        existing=[],
        incoming=[
            {"path": "z.txt", "type": "file"},
            {"path": "a.txt", "type": "file"},
            {"path": "m/k.txt", "type": "file"},
        ],
    )
    assert _actions(resp.json()) == [
        ("CREATE_DIRECTORY", "m"),
        ("CREATE_FILE", "z.txt"),  # 文件阶段保持 incoming 顺序
        ("CREATE_FILE", "a.txt"),
        ("CREATE_FILE", "m/k.txt"),
    ]


# ---------------------------------------------------------------------------
# 类型互换：旧节点先删，再按期望类型重建
# ---------------------------------------------------------------------------


def test_type_swap_file_to_directory():
    resp = _mirror(
        existing=[{"path": "swap", "type": "file"}],
        incoming=[
            {"path": "swap", "type": "directory"},
            {"path": "swap/child.txt", "type": "file"},
        ],
    )
    assert _actions(resp.json()) == [
        ("DELETE", "swap"),  # 旧文件
        ("CREATE_DIRECTORY", "swap"),
        ("CREATE_FILE", "swap/child.txt"),
    ]
    ops = resp.json()["operations"]
    assert ops[0]["type"] == "file"
    assert ops[0]["source"] == "existing"
    assert ops[0]["index"] == 1


def test_type_swap_directory_to_file():
    resp = _mirror(
        existing=[
            {"path": "swap", "type": "directory"},
            {"path": "swap/child.txt", "type": "file"},
        ],
        incoming=[{"path": "swap", "type": "file"}],
    )
    assert _actions(resp.json()) == [
        ("DELETE", "swap/child.txt"),  # 期望布局外的现存节点，先删
        ("DELETE", "swap"),  # 类型互换处的旧目录
        ("CREATE_FILE", "swap"),  # 旧节点是目录，不算同类型覆盖
    ]
    body = resp.json()
    assert body["summary"]["overwrite_file"] == 0
    assert body["summary"]["create_file"] == 1


def test_type_swap_with_implicit_existing_directory():
    # 现存文件 a/b/c.txt 使 a/b 成为隐含目录；期望布局中 a/b 是文件。
    resp = _mirror(
        existing=[{"path": "a/b/c.txt", "type": "file"}],
        incoming=[{"path": "a/b", "type": "file"}],
    )
    assert _actions(resp.json()) == [
        ("DELETE", "a/b/c.txt"),
        ("DELETE", "a/b"),  # 隐含目录 vs 期望文件：类型互换
        ("KEEP_DIRECTORY", "a"),
        ("CREATE_FILE", "a/b"),
    ]


# ---------------------------------------------------------------------------
# 隐含目录：现存侧删除、期望侧保留/补齐
# ---------------------------------------------------------------------------


def test_implicit_existing_dirs_deleted_outside_desired_layout():
    resp = _mirror(
        existing=[{"path": "a/b/c.txt", "type": "file"}],
        incoming=[{"path": "d.txt", "type": "file"}],
    )
    assert _actions(resp.json()) == [
        ("DELETE", "a/b/c.txt"),
        ("DELETE", "a/b"),  # 隐含目录也在期望布局外，一并删除
        ("DELETE", "a"),
        ("CREATE_FILE", "d.txt"),
    ]
    ops = resp.json()["operations"]
    assert all(op["source"] == "implicit" for op in ops[1:3])
    assert all(op["index"] is None for op in ops[1:3])


def test_implicit_desired_parents_created_or_kept():
    resp = _mirror(
        existing=[{"path": "x/y/old.txt", "type": "file"}],  # x、x/y 为现存隐含目录
        incoming=[{"path": "x/y/new.txt", "type": "file"}],
    )
    assert _actions(resp.json()) == [
        ("DELETE", "x/y/old.txt"),
        ("KEEP_DIRECTORY", "x"),  # 隐含父目录已存在：保留
        ("KEEP_DIRECTORY", "x/y"),
        ("CREATE_FILE", "x/y/new.txt"),
    ]


def test_explicit_incoming_directory_created_when_missing():
    resp = _mirror(
        existing=[],
        incoming=[
            {"path": "pkg", "type": "directory"},
            {"path": "pkg/mod", "type": "directory"},
        ],
    )
    assert _actions(resp.json()) == [
        ("CREATE_DIRECTORY", "pkg"),
        ("CREATE_DIRECTORY", "pkg/mod"),
    ]
    body = resp.json()
    assert body["summary"] == {
        "total": 2,
        "delete": 0,
        "create_directory": 2,
        "keep_directory": 0,
        "create_file": 0,
        "overwrite_file": 0,
    }


# ---------------------------------------------------------------------------
# 期望清单校验（422）
# ---------------------------------------------------------------------------


def test_incoming_unsafe_path_rejected_422():
    r = _mirror(existing=[], incoming=[{"path": "../escape", "type": "file"}])
    assert r.status_code == 422
    r = _mirror(
        existing=[],
        incoming=[
            {"path": "ok.txt", "type": "file"},
            {"path": "C:/win", "type": "file"},
        ],
    )
    assert r.status_code == 422


def test_incoming_duplicate_keys_rejected_422():
    r = _mirror(
        existing=[],
        incoming=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "Docs\\ReadMe.TXT", "type": "file"},  # 大小写/分隔符变体同键
        ],
    )
    assert r.status_code == 422
    # 目录与文件同键也算重复。
    r = _mirror(
        existing=[],
        incoming=[
            {"path": "a", "type": "directory"},
            {"path": "a", "type": "file"},
        ],
    )
    assert r.status_code == 422


def test_incoming_file_ancestor_rejected_422():
    r = _mirror(
        existing=[],
        incoming=[
            {"path": "a/b", "type": "file"},
            {"path": "a/b/c.txt", "type": "file"},
        ],
    )
    assert r.status_code == 422
    # 目录占据祖先路径是正常布局，不拒绝。
    r = _mirror(
        existing=[],
        incoming=[
            {"path": "a/b", "type": "directory"},
            {"path": "a/b/c.txt", "type": "file"},
        ],
    )
    assert r.status_code == 200


def test_existing_validation_reused_422():
    # 现存清单沿用 overlay-preview 校验。
    r = _mirror(
        existing=[{"path": "../escape", "type": "file"}],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422
    r = _mirror(
        existing=[
            {"path": "a", "type": "directory"},
            {"path": "A", "type": "directory"},
        ],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422
    r = _mirror(
        existing=[
            {"path": "a/b", "type": "file"},
            {"path": "a/b/c.txt", "type": "file"},
        ],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422


def test_mirror_symlink_type_rejected_422():
    r = _post(
        {
            "existing": [{"path": "lnk", "type": "symlink"}],
            "incoming": [{"path": "a.txt", "type": "file"}],
        }
    )
    assert r.status_code == 422
    r = _post(
        {
            "existing": [],
            "incoming": [{"path": "lnk", "type": "symlink"}],
        }
    )
    assert r.status_code == 422


def test_incoming_required_and_nonempty():
    assert _post({"incoming": []}).status_code == 422
    assert _post({"existing": []}).status_code == 422


# ---------------------------------------------------------------------------
# Windows 路径规范化复用与边界
# ---------------------------------------------------------------------------


def test_windows_normalization_reused_on_both_sides():
    nfd_ring = unicodedata.normalize("NFD", "Å")
    resp = _mirror(
        existing=[
            {"path": "Docs\\ReadMe.txt", "type": "file"},
            {"path": "docs/cafÅ.txt", "type": "file"},
            {"path": "DATA", "type": "directory"},
        ],
        incoming=[
            {"path": "docs/readme.TXT", "type": "file"},  # 大小写折叠后同键 → 覆盖
            {"path": f"docs/caf{nfd_ring}.txt", "type": "file"},  # NFC 归一后同键 → 覆盖
            {"path": "data", "type": "directory"},  # 大小写折叠后同键 → 保留
        ],
    )
    assert resp.status_code == 200
    assert _actions(resp.json()) == [
        ("KEEP_DIRECTORY", "data"),
        ("KEEP_DIRECTORY", "docs"),
        ("OVERWRITE_FILE", "docs/readme.TXT"),
        ("OVERWRITE_FILE", "docs/cafÅ.txt"),
    ]
    ops = resp.json()["operations"]
    assert ops[2]["comparison_key"] == "docs/readme.txt"
    assert ops[3]["canonical_path"] == "docs/cafÅ.txt"


def test_empty_existing_means_all_create():
    resp = _mirror(
        existing=[],
        incoming=[
            {"path": "x/y/z.txt", "type": "file"},
            {"path": "x/y", "type": "directory"},
        ],
    )
    assert _actions(resp.json()) == [
        ("CREATE_DIRECTORY", "x"),
        ("CREATE_DIRECTORY", "x/y"),
        ("CREATE_FILE", "x/y/z.txt"),
    ]
    # existing 字段可整体省略。
    r = _post({"incoming": [{"path": "a.txt", "type": "file"}]})
    assert r.status_code == 200
    assert _actions(r.json()) == [("CREATE_FILE", "a.txt")]


def test_identical_layouts_only_keep_and_overwrite():
    resp = _mirror(
        existing=[
            {"path": "a", "type": "directory"},
            {"path": "a/f.txt", "type": "file"},
        ],
        incoming=[
            {"path": "a", "type": "directory"},
            {"path": "a/f.txt", "type": "file"},
        ],
    )
    assert _actions(resp.json()) == [
        ("KEEP_DIRECTORY", "a"),
        ("OVERWRITE_FILE", "a/f.txt"),
    ]


# ---------------------------------------------------------------------------
# 原接口行为保持不变
# ---------------------------------------------------------------------------


def test_other_endpoints_untouched():
    r = client.post(
        "/api/v1/preflight",
        json={
            "entries": [
                {"path": "Docs\\ReadMe.txt", "type": "file"},
                {"path": "docs/readme.txt", "type": "file"},
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["summary"]["collisions"] == 1

    r = client.post(
        "/api/v1/overlay-preview",
        json={
            "existing": [{"path": "docs/readme.txt", "type": "file"}],
            "incoming": [{"path": "docs/readme.txt", "type": "file"}],
        },
    )
    assert r.status_code == 200
    assert r.json()["entries"][0]["status"] == "OVERWRITE"
