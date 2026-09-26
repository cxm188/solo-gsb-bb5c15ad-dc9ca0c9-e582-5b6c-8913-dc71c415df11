"""镜像同步预演：把现存布局同步为期望布局的操作序列规划（纯内存，不访问文件系统）。

语义概述：

* 现存清单（existing）与期望清单（incoming）都先整体校验：不安全路径、
  同键（Windows 比较键）重复、文件占据其他条目的祖先路径，任一命中即拒绝
  （API 层映射为 422）。两侧路径均复用 Windows 规范化规则。
* 两侧清单分别插入**虚拟目录树**；两侧未显式列出的父目录都按**隐含目录**
  处理（现存侧它们必然存在；期望侧它们是需要保留/补齐的目录）。
* 操作序列分三个阶段，依次拼接：
  1. **删除**（``DELETE``）：期望布局外的现存节点，以及类型互换处
     （同键但文件/目录类型不同）的旧节点。按路径深度**从深到浅**，
     保证先删子节点再删父目录；同深度按比较键排序。
  2. **目录**（``CREATE_DIRECTORY`` / ``KEEP_DIRECTORY``）：期望布局中的
     全部目录（显式条目 + 隐含父目录），按深度**从浅到深**、同深度按比较键
     排序；现存树中已有同键目录的标记保留，否则创建（类型互换处的旧文件
     已在删除阶段移除）。
  3. **文件**（``CREATE_FILE`` / ``OVERWRITE_FILE``）：按 **incoming 顺序**
     写入期望清单中的文件；同键已有现存文件标记覆盖，否则新建（类型互换处
     的旧目录已在删除阶段移除）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .normalize import NormalizedEntry
from .overlay import (
    DIR_TYPE,
    FILE_TYPE,
    OverlayNodeSource,
    build_existing_tree,
    build_tree,
)


class MirrorAction(str, Enum):
    DELETE = "DELETE"  # 删除期望布局外的现存节点 / 类型互换处的旧节点
    CREATE_DIRECTORY = "CREATE_DIRECTORY"  # 创建期望布局所需的目录
    KEEP_DIRECTORY = "KEEP_DIRECTORY"  # 期望布局所需、现存的目录，保留不动
    CREATE_FILE = "CREATE_FILE"  # 写入新文件
    OVERWRITE_FILE = "OVERWRITE_FILE"  # 覆盖同键现存文件


@dataclass(frozen=True)
class MirrorOperation:
    """镜像同步计划中的单步操作。"""

    action: MirrorAction
    kind: str  # "file" | "directory"
    canonical_path: str  # 展示用规范路径（正斜杠分隔）
    comparison_key: str  # Windows 等价比较键（小写折叠）
    source: OverlayNodeSource  # 删除步骤指向现存侧，其余步骤指向期望侧
    index: int | None  # 来源清单中的下标（从 1 开始）；隐含目录为 None
    depth: int  # 路径深度（段数），用于阶段内排序


def _depth(key: str) -> int:
    """比较键（正斜杠分隔）的路径段数。"""
    return key.count("/") + 1


def plan_mirror_operations(
    existing: list[NormalizedEntry], incoming: list[NormalizedEntry]
) -> list[MirrorOperation]:
    """规划把现存布局同步为期望布局的操作序列（纯内存）。

    调用方保证两侧清单均已通过 :func:`app.overlay.validate_manifest`
    校验（否则请求以 422 被拒绝），此处直接构建虚拟目录树。
    """
    existing_tree = build_existing_tree(existing)
    desired_tree = build_tree(incoming, OverlayNodeSource.INCOMING)

    # 1) 删除：期望布局外的现存节点 + 类型互换处的旧节点（深的先删，先子后父）。
    deletes: list[MirrorOperation] = []
    for key, node in existing_tree.items():
        desired = desired_tree.get(key)
        if desired is not None and desired.kind == node.kind:
            continue  # 同键同类型：保留（目录复用 / 文件被覆盖）
        deletes.append(
            MirrorOperation(
                action=MirrorAction.DELETE,
                kind=node.kind,
                canonical_path=node.canonical_path,
                comparison_key=key,
                source=node.source,
                index=node.index,
                depth=_depth(key),
            )
        )
    deletes.sort(key=lambda op: (-op.depth, op.comparison_key))

    # 2) 目录：期望布局中的目录（显式 + 隐含父目录），浅的先建，先父后子。
    mkdirs: list[MirrorOperation] = []
    for key, node in desired_tree.items():
        if node.kind != DIR_TYPE:
            continue
        existing_node = existing_tree.get(key)
        action = (
            MirrorAction.KEEP_DIRECTORY
            if existing_node is not None and existing_node.kind == DIR_TYPE
            else MirrorAction.CREATE_DIRECTORY
        )
        mkdirs.append(
            MirrorOperation(
                action=action,
                kind=DIR_TYPE,
                canonical_path=node.canonical_path,
                comparison_key=key,
                source=node.source,
                index=node.index,
                depth=_depth(key),
            )
        )
    mkdirs.sort(key=lambda op: (op.depth, op.comparison_key))

    # 3) 文件：按 incoming 顺序写入；同键已有现存文件标记覆盖，否则新建。
    writes: list[MirrorOperation] = []
    for entry in incoming:
        if entry.entry_type != FILE_TYPE:
            continue
        key = entry.comparison_key
        existing_node = existing_tree.get(key)
        action = (
            MirrorAction.OVERWRITE_FILE
            if existing_node is not None and existing_node.kind == FILE_TYPE
            else MirrorAction.CREATE_FILE
        )
        writes.append(
            MirrorOperation(
                action=action,
                kind=FILE_TYPE,
                canonical_path=entry.canonical_path,
                comparison_key=key,
                source=OverlayNodeSource.INCOMING,
                index=entry.index,
                depth=len(entry.key_parts),
            )
        )

    return deletes + mkdirs + writes
