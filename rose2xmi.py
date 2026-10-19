#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rose2xmi -- 把 Rational Rose 的 Petal 模型文件 (.mdl) 转换成
            现代 Linux UML 工具 (Umbrello / ArgoUML / Modelio ...)
            可以原样打开、中文不乱码的 UTF-8 XMI 文件。

为什么需要它
------------
Rational Rose 的 .mdl 不是 XML，而是叫 Petal 的类 Lisp 文本格式；
中文版 Rose 按 GB2312/GBK 存储字符串，并在文件头写 `charSet 134`。

Umbrello 自带 Rose 导入 (umbrello/import_rose.cpp)，靠文件扩展名 .mdl 触发
(见 umldoc.cpp: `filetype.endsWith(".mdl") -> Import_Rose::loadFromMDL` )。
但它的 loadFromMDL() 先把 QTextStream 设成 ISO 8859-1，读到 charSet 行之后
才切换到 GB18030；QTextStream 中途换 codec 会破坏已经缓冲的多字节序列，
结果所有中文都变成 U+FFFD 替换字符（在 Umbrello 里显示成一排豆腐块）。

本脚本一次性用正确编码解码整个文件，自己解析 Petal，再输出干净的 UTF-8 XMI，
从而绕开上述二次解码的问题。

输出格式
--------
XMI 1.2 / UML 1.4 —— 这是 Umbrello 的默认保存方言（umldoc.cpp 非 uml2 分支），
同时也能被其他 UML 工具读取。

用法
----
    python3 rose2xmi.py 图书管理系统.mdl
    # -> 在 .mdl 同目录生成 图书管理系统.xmi

    python3 rose2xmi.py model.mdl -o out.xmi
    python3 rose2xmi.py *.mdl --force

仅依赖 Python 3 标准库。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile

SCRIPT_VERSION = "1.0"
XMI1_FILE_VERSION = "1.0"

# Rose 的坐标单位换算到 Qt：见 Umbrello petaltree2uml.cpp: const qreal Rose2Qt = 0.5;
ROSE2QT = 0.5

# charSet 编号 -> Python 编解码器（与 Umbrello encodinghelper.h 对齐）
CHARSET_CODEC = {
    0: "cp1252",       # ASCII / Default
    1: "cp1252",       # Default
    2: None,           # Symbol，无对应 codec
    77: "mac_roman",   # macintosh
    128: "cp932",      # Shift_JIS
    129: "cp949",      # EUC-KR
    130: "cp949",      # EUC-KR
    134: "gb18030",    # 简体中文（GB2312/GBK 的超集）
    136: "big5",       # 繁体中文
    161: "cp1253",     # windows-1253
    162: "cp1254",     # windows-1254
    163: "cp1258",     # windows-1258
}

# 探测顺序：只有 UTF-8 是自校验的，其余按中文环境常见度排
PROBE_ORDER = ("utf-8", "gb18030", "big5", "cp1252")

# Uml::AssociationType（umbrello/basictypes.h）
AT = {
    "Generalization": 500,
    "Aggregation": 501,
    "Dependency": 502,
    "Association": 503,
    "Association_Self": 504,
    "Containment": 509,
    "Composition": 510,
    "Realization": 511,
    "UniAssociation": 512,
    "Anchor": 513,
}

# Uml::DiagramType（umbrello/basictypes.h）
DT = {
    "ClassDiagram": 1,
    "UseCaseDiagram": 2,
    "State_Diagram": 5,
    "ActivityDiagram": 6,
    "Module_Diagram": 7,
    "Process_Diagram": 8,
}

# Rose 图 -> 它所属的 Umbrello 视图根
DIAGRAM_VIEW = {
    "ClassDiagram": "Logical View",
    "State_Diagram": "Logical View",
    "ActivityDiagram": "Logical View",
    "UseCaseDiagram": "Use Case View",
    "Module_Diagram": "Component View",
    "Process_Diagram": "Deployment View",
}

# UMLListViewItem::ListViewType（umbrello/umllistviewitem.h，从 799 起）。
# Umbrello 的树形视图是靠文件末尾 <XMI.extensions> 里的 <listview> 恢复的，
# 缺了它树里就看不到类图/用例图，也就没法双击打开。
LVT_VIEW_ROOT = 800              # lvt_View
LVT_CLASS = 813                  # lvt_Class
LVT_DATATYPE = 829               # lvt_Datatype
LVT_DATATYPE_FOLDER = 830        # lvt_Datatype_Folder
LVT_VIEW = {
    "Use Case View": 802,               # lvt_UseCase_View
    "Entity Relationship Model": 836,   # lvt_EntityRelationship_Model
    "Component View": 821,              # lvt_Component_View
    "Logical View": 801,                # lvt_Logical_View
    "Deployment View": 827,             # lvt_Deployment_View
}
LVT_DIAGRAM = {
    1: 807,   # Class               -> lvt_Class_Diagram
    2: 805,   # UseCase             -> lvt_UseCase_Diagram
    3: 810,   # Sequence            -> lvt_Sequence_Diagram
    4: 806,   # Collaboration       -> lvt_Collaboration_Diagram
    5: 808,   # State               -> lvt_State_Diagram
    6: 809,   # Activity            -> lvt_Activity_Diagram
    7: 819,   # Component           -> lvt_Component_Diagram
    8: 825,   # Deployment          -> lvt_Deployment_Diagram
    9: 834,   # EntityRelationship  -> lvt_EntityRelationship_Diagram
}
# 树里视图根的排列顺序，跟 Umbrello 自己写出来的一致。
#
# 注意：这里**故意不含** "Entity Relationship Model"。
# Umbrello 26.08.2 处理 <listitem type="836" .../> 时会段错误
# （loadChildrenFromXMI -> lvt_EntityRelationship_Model 分支），实测必崩。
# 好在 ER 视图根本不需要写进 listview：加载模型时 slotObjectCreated 已经
# 把它建到树里了，省掉这一条树里照样有，且不会崩。
LVT_VIEW_ORDER = ("Use Case View", "Component View",
                  "Logical View", "Deployment View")

# Rose 里最常见的标准数据类型，写进 Datatypes 包
BUILTIN_DATATYPES = (
    "int", "char", "bool", "float", "double", "short", "long",
    "unsigned int", "unsigned short", "unsigned long", "string", "void",
)


class Rose2XmiError(Exception):
    """转换过程中的致命错误。"""


def _umask():
    """读取当前 umask（读一次就还原，避免影响进程状态）。"""
    cur = os.umask(0)
    os.umask(cur)
    return cur


# --------------------------------------------------------------------------
# 1. 编码探测与解码
# --------------------------------------------------------------------------

def sniff_charset(raw: bytes):
    """从文件头 `(object Petal ... charSet N ...)` 里取出 charSet 编号。"""
    head = raw[:4096].decode("latin-1", errors="replace")
    m = re.search(r"\(\s*object\s+Petal\b(.*?)\)", head, re.S)
    if not m:
        return None
    m2 = re.search(r"\bcharSet\s+(\d+)", m.group(1))
    return int(m2.group(1)) if m2 else None


def decode_mdl(raw: bytes, forced: str = None):
    """按 charSet / 探测顺序解码，返回 (text, encoding_name, 说明)。"""
    if forced:
        try:
            return raw.decode(forced), forced, "命令行指定"
        except (LookupError, UnicodeDecodeError) as exc:
            raise Rose2XmiError("用 --encoding %s 解码失败: %s" % (forced, exc))

    charset = sniff_charset(raw)
    if charset is not None:
        codec = CHARSET_CODEC.get(charset, "sentinel")
        if codec is None:
            raise Rose2XmiError(
                "charSet %d 是 Symbol 字体编码，没有对应编解码器；请用 --encoding 指定"
                % charset)
        if codec != "sentinel":
            try:
                return raw.decode(codec), codec, "charSet %d" % charset
            except UnicodeDecodeError:
                # 声明的编码解不开，落到探测流程
                pass
        else:
            note = "未登记的 charSet %d" % charset
    else:
        note = "文件头没有 charSet"

    for enc in PROBE_ORDER:
        try:
            return raw.decode(enc), enc, note + "，探测命中"
        except UnicodeDecodeError:
            continue

    raise Rose2XmiError("无法确定文件编码，请用 --encoding 指定（如 gb18030 / big5）")


# --------------------------------------------------------------------------
# 2. Petal 词法与语法分析
# --------------------------------------------------------------------------

class QStr(str):
    """带引号的字符串字面量。"""


class Atom(str):
    """裸 token（TRUE / 50 / int ...）。"""


class Ref(str):
    """@N 图形局部引用。"""


def tokenize(text: str):
    """把 Petal 文本切成 token 流。逗号视作空白（坐标写成 `(3586, 586)`）。"""
    toks = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r,":
            i += 1
            continue
        if c in "()":
            toks.append((c, line))
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    if nxt == "n":
                        buf.append("\n")
                        j += 2
                        continue
                    if nxt == "t":
                        buf.append("\t")
                        j += 2
                        continue
                    buf.append(nxt)      # \" \\ \ 等
                    j += 2
                    continue
                if ch == '"':
                    break
                if ch == "\n":
                    line += 1
                buf.append(ch)
                j += 1
            if j >= n:
                raise Rose2XmiError("第 %d 行：字符串没有闭合的引号" % line)
            toks.append((QStr("".join(buf)), line))
            i = j + 1
            continue
        # 裸 token
        j = i
        while j < n and text[j] not in ' \t\r\n()",':
            j += 1
        word = text[i:j]
        toks.append((Ref(word) if word.startswith("@") else Atom(word), line))
        i = j
    return toks


def _parse_sexpr(toks, i):
    """从 toks[i] == '(' 开始，解析成一个列表。返回 (items, next_index)。"""
    line = toks[i][1]
    i += 1
    items = []
    while True:
        if i >= len(toks):
            raise Rose2XmiError("第 %d 行附近：括号不匹配（文件被截断？）" % line)
        tok = toks[i][0]
        if tok == ")":
            return items, i + 1
        if tok == "(":
            sub, i = _parse_sexpr(toks, i)
            items.append(sub)
        else:
            items.append(tok)
            i += 1


class ListValue:
    """`(list NAME item...)` 形式。"""

    __slots__ = ("name", "items")

    def __init__(self, name, items):
        self.name = name
        self.items = items

    def __repr__(self):
        return "ListValue(%r, %d items)" % (self.name, len(self.items))


class PNode:
    """一个 `(object TYPE arg... key value ...)` 节点。"""

    __slots__ = ("type", "iargs", "attrs")

    def __init__(self, type_, iargs, attrs):
        self.type = type_          # 对象类型名，如 Class / ClassView
        self.iargs = iargs         # [TYPE, name, extra...]（与 Umbrello initialArgs 一致）
        self.attrs = attrs         # {key: value}，value 为 str / PNode / ListValue

    @property
    def name(self) -> str:
        return self.iargs[1] if len(self.iargs) > 1 else ""

    @property
    def view_tag(self):
        """末尾的 @N 局部引用（若有）。"""
        return self.iargs[-1] if self.iargs and isinstance(self.iargs[-1], Ref) else None

    def get(self, key, default=""):
        v = self.attrs.get(key, default)
        if isinstance(v, (PNode, ListValue)):
            return default
        return v

    def node(self, key):
        v = self.attrs.get(key)
        return v if isinstance(v, PNode) else None

    def lst(self, key):
        v = self.attrs.get(key)
        return v if isinstance(v, ListValue) else None

    def __repr__(self):
        return "PNode(%s, %r, %d attrs)" % (self.type, self.name, len(self.attrs))


def _to_value(x):
    """把原始 sexpr 元素转成 str / PNode / ListValue。"""
    if isinstance(x, list):
        if x and str(x[0]) == "object":
            return _to_object(x)
        if x and str(x[0]) == "list":
            # 列表名可以省略，例如 `ProcsNDevs (list (object Process_Diagram ...))`
            if len(x) > 1 and isinstance(x[1], (QStr, Atom)):
                name, raw_items = str(x[1]), x[2:]
            else:
                name, raw_items = "", x[1:]
            return ListValue(name, [_to_value(v) for v in raw_items])
        # (value cardinality "1") / (value Text "abc") -> 只取真正的值
        if x and str(x[0]) == "value":
            return str(x[-1]) if len(x) > 1 else ""
        # 立即值元组，如 (3586, 586) -> "3586 586"（与 Umbrello 一致）
        return " ".join(str(v) for v in x)
    return x


_KEYLIKE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _to_object(x):
    """x[0] == 'object'：拆出 initial args 与 key/value 属性。"""
    if len(x) < 2:
        raise Rose2XmiError("`(object` 后面缺少类型名")
    type_ = str(x[1])
    rest = x[2:]
    # initial args = 开头的 字符串 / @引用（Rose 的对象名总是带引号）
    i = 0
    iargs = [type_]
    while i < len(rest) and isinstance(rest[i], (QStr, Ref)):
        iargs.append(rest[i])
        i += 1
    # 宽容处理：`(object Class Foo quid "X")` 这种未加引号的简单名字。
    # 只有当下一个 token 长得像属性名时才认定它是名字，避免把
    # `(object Petal version 50 ...)` 里的 `version` 误当成对象名。
    if len(iargs) == 1 and i + 2 < len(rest) and isinstance(rest[i], (Atom, Ref)):
        if _KEYLIKE.match(str(rest[i + 1])):
            iargs.append(rest[i])
            i += 1

    attrs = {}
    while i < len(rest):
        key = rest[i]
        if key == ")" or isinstance(key, list):
            i += 1
            continue
        if i + 1 >= len(rest):
            attrs[str(key)] = ""
            break
        attrs[str(key)] = _to_value(rest[i + 1])
        i += 2
    return PNode(type_, iargs, attrs)


def parse_petal(text: str) -> PNode:
    """解析整个 Petal 文件，返回根对象（通常是 Design）。"""
    toks = tokenize(text)
    i = 0
    roots = []
    while i < len(toks):
        if toks[i][0] == "(":
            items, i = _parse_sexpr(toks, i)
            node = _to_object(items) if items and str(items[0]) == "object" else None
            if node is not None:
                roots.append(node)
        else:
            i += 1
    if not roots:
        raise Rose2XmiError("没有找到任何 Petal 对象；这个文件可能不是 Rose .mdl")
    for r in roots:
        if r.type == "Design":
            return r
    return roots[0]


# --------------------------------------------------------------------------
# 3. 中间模型
# --------------------------------------------------------------------------

class UAttr:
    __slots__ = ("quid", "name", "type", "visibility", "initial")

    def __init__(self, quid, name, type_="", visibility="private", initial=""):
        self.quid, self.name = quid, name
        self.type, self.visibility, self.initial = type_, visibility, initial


class UOp:
    __slots__ = ("quid", "name", "visibility", "params")

    def __init__(self, quid, name, visibility="public", params=None):
        self.quid, self.name = quid, name
        self.visibility, self.params = visibility, params or []


class UClass:
    __slots__ = ("quid", "name", "attrs", "ops", "stereotype", "abstract", "package")

    def __init__(self, quid, name):
        self.quid, self.name = quid, name
        self.attrs, self.ops = [], []
        self.stereotype = ""
        self.abstract = False
        self.package = None


class URole:
    __slots__ = ("quid", "name", "target_quid", "multiplicity",
                 "navigable", "aggregation")

    def __init__(self, quid):
        self.quid = quid
        self.name = ""
        self.target_quid = ""
        self.multiplicity = ""
        self.navigable = True
        self.aggregation = "none"


class UAssoc:
    __slots__ = ("quid", "name", "roles", "stereotype", "doc")

    def __init__(self, quid, name):
        self.quid, self.name = quid, name
        self.roles = []          # 最多两个 URole
        self.stereotype = ""
        self.doc = ""


class UGeneralization:
    __slots__ = ("quid", "child_quid", "parent_quid")

    def __init__(self, quid, child_quid, parent_quid):
        self.quid, self.child_quid, self.parent_quid = quid, child_quid, parent_quid


class UWidget:
    __slots__ = ("kind", "ref_quid", "x", "y", "w", "h", "view_tag")

    def __init__(self, kind, ref_quid, x, y, w, h, view_tag=None):
        self.kind, self.ref_quid = kind, ref_quid
        self.x, self.y, self.w, self.h = x, y, w, h
        self.view_tag = view_tag


class UAssocWidget:
    __slots__ = ("assoc_quid", "type", "aid", "bid", "points")

    def __init__(self, assoc_quid, type_, aid, bid, points):
        self.assoc_quid, self.type = assoc_quid, type_
        self.aid, self.bid, self.points = aid, bid, points


class UDiagram:
    __slots__ = ("quid", "name", "dtype", "widgets", "assoc_widgets", "view")

    def __init__(self, quid, name, dtype, view):
        self.quid, self.name, self.dtype, self.view = quid, name, dtype, view
        self.widgets = []
        self.assoc_widgets = []


class UModel:
    def __init__(self):
        self.classes = {}
        self.assocs = {}
        self.generalizations = []
        self.diagrams = []
        self.warnings = []
        self.root_name = "UML Model"

    def warn(self, msg):
        self.warnings.append(msg)


# --------------------------------------------------------------------------
# 4. 从 Petal 树构建 UML 模型
# --------------------------------------------------------------------------

def _clean(s):
    """去掉 Rose 字符串里残留的引号。"""
    return s.replace('"', "") if isinstance(s, str) else ""


def _name(s):
    """Rose 给未命名元素起名 $UNNAMED$n，显示时应当留空。"""
    s = _clean(s)
    return "" if s.startswith("$UNNAMED") else s


def _num(node, key, default=0.0):
    v = node.get(key)
    if v == "" or v is None:
        return default
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(v))
    return float(m.group(1)) if m else default


def _location(node):
    """`location (x, y)` -> (x, y)，Rose 坐标（未缩放）。"""
    v = node.get("location")
    if not v:
        return None
    parts = str(v).split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _stack(v):
    """PNode 的 view_tag 展开成 @N 字符串。"""
    return v if isinstance(v, Ref) else ("@" + str(v) if str(v).startswith("@") else None)


def collect_nodes(root, wanted):
    """深度优先收集指定类型的 PNode（保持文档顺序）。"""
    found = []
    seen = set()

    def walk(n):
        if id(n) in seen:
            return
        seen.add(id(n))
        if n.type in wanted:
            found.append(n)
        for v in n.attrs.values():
            if isinstance(v, PNode):
                walk(v)
            elif isinstance(v, ListValue):
                for it in v.items:
                    if isinstance(it, PNode):
                        walk(it)

    walk(root)
    return found


def build_model(root: PNode) -> UModel:
    m = UModel()
    if root.name:
        m.root_name = root.name

    # ---- 类 ----
    for cn in collect_nodes(root, {"Class"}):
        quid = _clean(cn.get("quid"))
        if not quid:
            continue
        c = UClass(quid, _name(cn.name))
        if "abstract" in cn.attrs and str(cn.get("abstract")).upper() == "TRUE":
            c.abstract = True
        ca = cn.lst("class_attributes")
        if ca:
            for a in ca.items:
                if not isinstance(a, PNode) or a.type != "ClassAttribute":
                    continue
                c.attrs.append(UAttr(_clean(a.get("quid")), a.name,
                                     _clean(a.get("type")),
                                     _vis(a.get("exportControl") or a.get("visibility")),
                                     _clean(a.get("initValue"))))
        opr = cn.lst("operations")
        if opr:
            for o in opr.items:
                if not isinstance(o, PNode) or o.type != "Operation":
                    continue
                c.ops.append(UOp(_clean(o.get("quid")), o.name,
                                 _vis(o.get("opExportControl"))))
        m.classes[quid] = c

    # ---- 继承 ----
    for cn in collect_nodes(root, {"Class"}):
        child = _clean(cn.get("quid"))
        sc = cn.lst("superclasses")
        if not sc:
            continue
        for ir in sc.items:
            if not isinstance(ir, PNode) or ir.type != "Inheritance_Relationship":
                continue
            parent = _clean(ir.get("quidu"))
            if not parent:
                # 退而求其次：按 "Logical View::父类" 限定名找
                parent = _resolve_name(m, _clean(ir.get("supplier")))
            if not parent or parent not in m.classes:
                m.warn("跳过无法解析父类的继承关系 (supplier=%r)"
                       % _clean(ir.get("supplier")))
                continue
            gq = _clean(ir.get("quid")) or ("gen_" + child + "_" + parent)
            m.generalizations.append(UGeneralization(gq, child, parent))

    # ---- 关联 ----
    for an in collect_nodes(root, {"Association"}):
        quid = _clean(an.get("quid"))
        if not quid:
            continue
        a = UAssoc(quid, _name(an.name))
        a.doc = _clean(an.get("documentation"))
        rl = an.lst("roles")
        if not rl or len(rl.items) < 2:
            m.warn("关联 %s 的 roles 少于 2 个，已跳过" % quid)
            continue
        for rn in rl.items[:2]:
            if not isinstance(rn, PNode) or rn.type != "Role":
                continue
            r = URole(_clean(rn.get("quid")))
            nm = rn.name
            if nm and not nm.startswith("$UNNAMED"):
                r.name = nm
            label = _clean(rn.get("label"))
            if label:
                r.name = label
            r.target_quid = _clean(rn.get("quidu")) or _resolve_name(
                m, _clean(rn.get("supplier")))
            cc = rn.get("client_cardinality")
            if cc:
                mm = re.search(r'"([^"]*)"', str(cc))
                r.multiplicity = (mm.group(1) if mm
                                  else str(cc).replace("value cardinality", "").strip())
            nav = str(rn.get("is_navigable")).upper()
            if nav == "FALSE":
                r.navigable = False
            if str(rn.get("is_aggregate")).upper() == "TRUE":
                r.aggregation = "aggregate"
            if _clean(rn.get("Containment")) == "By Value":
                r.aggregation = "composite"
            a.roles.append(r)
        if len(a.roles) < 2:
            m.warn("关联 %s 的有效 Role 少于 2 个，已跳过" % quid)
            continue
        a.stereotype = _clean(an.get("stereotype"))
        m.assocs[quid] = a

    # ---- 图 ----
    for dn in collect_nodes(root, set(DT.keys())):
        dtype = DT[dn.type]
        quid = _clean(dn.get("quid"))
        d = UDiagram(quid, dn.name or dn.type, dtype,
                     DIAGRAM_VIEW.get(dn.type, "Logical View"))
        items = dn.lst("items")
        by_tag = {}
        if items:
            for it in items.items:
                if isinstance(it, PNode):
                    t = it.view_tag
                    if t:
                        by_tag[t] = it
        for it in (items.items if items else []):
            if not isinstance(it, PNode):
                continue
            if it.type in ("ClassView", "CategoryView", "UseCaseView"):
                ref = _clean(it.get("quidu"))
                loc = _location(it)
                if loc is None:
                    continue
                w = _num(it, "width", 0.0)
                h = _num(it, "height", 0.0)
                wq = w * ROSE2QT if w else 120.0
                hq = h * ROSE2QT if h else 60.0
                # Rose 的 location 是对象中心
                xq = loc[0] * ROSE2QT - wq / 2.0
                yq = loc[1] * ROSE2QT - hq / 2.0
                d.widgets.append(UWidget("classwidget", ref, xq, yq, wq, hq,
                                         it.view_tag))
            elif it.type in ("AssociationViewNew", "InheritView", "RealizeView",
                             "UsesView"):
                if it.type == "AssociationViewNew":
                    atype = AT["UniAssociation"]
                elif it.type == "RealizeView":
                    atype = AT["Realization"]
                elif it.type == "UsesView":
                    atype = AT["Dependency"]
                else:
                    atype = AT["Generalization"]
                aq = _clean(it.get("quidu"))
                aid = bid = None
                points = []
                rvl = it.lst("roleview_list")
                if rvl:
                    sup = cli = None
                    # roles[0] -> supplier, roles[1] -> client（见 handleAssocView）
                    for idx, rv in enumerate(rvl.items[:2]):
                        if not isinstance(rv, PNode):
                            continue
                        tag = rv.get("supplier")
                        if idx == 0:
                            sup = sup or tag
                        else:
                            cli = cli or tag
                    aid, bid = cli, sup
                else:
                    aid = it.get("client")
                    bid = it.get("supplier")
                aid = _stack(aid)
                bid = _stack(bid)
                # 端点缺坐标时用 vertices 兜底
                verts = it.lst("vertices")
                if verts:
                    for p in verts.items:
                        if isinstance(p, str):
                            pp = p.split()
                            if len(pp) == 2:
                                try:
                                    points.append((float(pp[0]) * ROSE2QT,
                                                   float(pp[1]) * ROSE2QT))
                                except ValueError:
                                    pass
                if aid and bid:
                    d.assoc_widgets.append(
                        UAssocWidget(aq, atype, aid, bid, points))
        m.diagrams.append(d)

    return m


def _vis(v):
    v = _clean(v).lower()
    if v in ("public", "protected", "private"):
        return v
    return "public"


def _resolve_name(m: UModel, qualified: str):
    """把 "Logical View::图书" 解析成 quid。"""
    if not qualified:
        return ""
    short = qualified.split("::")[-1]
    for quid, c in m.classes.items():
        if c.name == short:
            return quid
    return ""


# --------------------------------------------------------------------------
# 5. XMI 输出
# --------------------------------------------------------------------------

def esc(text) -> str:
    """XML 属性转义。"""
    s = "" if text is None else str(text)
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
          .replace('"', "&quot;"))
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\n", "&#xa;").replace("\t", "&#x9;")
    return s


class XmlOut:
    def __init__(self):
        self.buf = []
        self.depth = 0

    def raw(self, s):
        self.buf.append(s)

    def open(self, tag, attrs=None):
        self.buf.append("%s<%s%s>" % (" " * self.depth, tag, self._a(attrs)))
        self.depth += 1

    def close(self, tag):
        self.depth -= 1
        self.buf.append("%s</%s>" % (" " * self.depth, tag))

    def empty(self, tag, attrs=None):
        self.buf.append("%s<%s%s/>" % (" " * self.depth, tag, self._a(attrs)))

    def text(self, tag, value, attrs=None):
        self.buf.append("%s<%s%s>%s</%s>" % (
            " " * self.depth, tag, self._a(attrs), esc(value), tag))

    @staticmethod
    def _a(attrs):
        if not attrs:
            return ""
        out = []
        for k, v in attrs:
            if v is None:
                continue
            out.append(' %s="%s"' % (k, esc(v)))
        return "".join(out)

    def render(self):
        return "\n".join(self.buf) + "\n"


def _id(quid, fallback):
    return "q" + quid if quid else fallback


def write_xmi(m: UModel, out_path: str, source_name: str):
    x = XmlOut()
    x.raw('<?xml version="1.0" encoding="UTF-8"?>')
    x.open("XMI", [("verified", "false"), ("xmi.version", "1.2"),
                   ("xmlns:UML", "http://schema.omg.org/spec/UML/1.4")])
    x.open("XMI.header")
    x.open("XMI.documentation")
    x.text("XMI.exporter", "rose2xmi %s (from %s)" % (SCRIPT_VERSION, source_name))
    x.text("XMI.exporterVersion", XMI1_FILE_VERSION)
    x.text("XMI.exporterEncoding", "UnicodeUTF8")
    x.close("XMI.documentation")
    x.empty("XMI.metamodel", [("xmi.name", "UML"), ("xmi.version", "1.4"),
                              ("href", "UML.xml")])
    x.close("XMI.header")

    x.open("XMI.content")
    x.open("UML:Model", [("xmi.id", "m1"), ("name", m.root_name),
                         ("isSpecification", "false"), ("isAbstract", "false"),
                         ("isLeaf", "false"), ("isRoot", "false")])
    x.open("UML:Namespace.ownedElement")
    for sid, sname in (("folder", "folder"), ("datatype", "datatype")):
        x.empty("UML:Stereotype", [("xmi.id", sid), ("name", sname),
                                   ("visibility", "public"),
                                   ("isSpecification", "false"),
                                   ("namespace", "m1"), ("isAbstract", "false"),
                                   ("isLeaf", "false"), ("isRoot", "false")])

    # ---- Logical View ----
    x.open("UML:Model", _view_attrs("Logical View", "m1"))
    x.open("UML:Namespace.ownedElement")
    _write_datatypes(x)
    _write_classes(x, m, "Logical View")
    _write_associations(x, m, "Logical View")
    _write_generalizations(x, m, "Logical View")
    x.close("UML:Namespace.ownedElement")
    _write_diagrams(x, m, "Logical View")
    x.close("UML:Model")

    # ---- 其余视图（保持存在，便于 Umbrello 展示同样的结构）----
    for view in ("Use Case View", "Component View", "Deployment View"):
        x.open("UML:Model", _view_attrs(view, "m1"))
        x.open("UML:Namespace.ownedElement")
        x.close("UML:Namespace.ownedElement")
        _write_diagrams(x, m, view)
        x.close("UML:Model")

    x.close("UML:Namespace.ownedElement")
    x.close("UML:Model")
    x.close("XMI.content")

    # ---- 文档级扩展 ----
    x.open("XMI.extensions", [("xmi.extender", "umbrello")])
    # viewid = 打开文件时默认显示的图。挑内容最多的那张（通常是类图），
    # 空图（Rose 默认生成的几个空用例/部署图）不选。
    cand = max(m.diagrams,
               key=lambda d: len(d.widgets) + len(d.assoc_widgets),
               default=None)
    doc = [("documentation", "")]
    if cand is not None:
        doc.insert(0, ("viewid", _id(cand.quid, "d1")))
    doc.append(("uniqueid", "0"))
    x.empty("docsettings", doc)
    # 树形视图的内容；没有它 Umbrello 左侧树里就看不到图表
    _write_listview(x, m)
    x.close("XMI.extensions")

    x.close("XMI")
    return x.render()


def _diagrams_of(m, view):
    return [d for d in m.diagrams if d.view == view]


def _write_listview(x, m):
    """写 <listview>。

    Umbrello 左侧的树形视图就是靠这个恢复的：缺了它，模型里的类和图虽然
    都加载进来了（所以 --export 还能导出图片），但树里是空的，也就没法从
    树上双击打开图表。
    """
    x.open("listview")
    x.open("listitem", [("open", "1"), ("type", str(LVT_VIEW_ROOT)),
                        ("id", "Views")])
    for view in LVT_VIEW_ORDER:
        lvt = LVT_VIEW.get(view)
        if lvt is None:
            continue
        ds = _diagrams_of(m, view)
        is_logical = (view == "Logical View")
        if not ds and not is_logical:
            x.empty("listitem", [("open", "1"), ("type", str(lvt)),
                                 ("id", view)])
            continue
        x.open("listitem", [("open", "1"), ("type", str(lvt)), ("id", view)])
        if is_logical:
            # Datatypes 文件夹写成自闭合：里面的具体数据类型由加载模型时的
            # slotObjectCreated 建到树上，再列一遍反而会和 Umbrello 自带的
            # 同名类型对不上（出现 findItem(id ...) returns null）。
            x.empty("listitem", [("open", "0"),
                                 ("type", str(LVT_DATATYPE_FOLDER)),
                                 ("id", "Datatypes")])
        for i, d in enumerate(ds):
            lt = LVT_DIAGRAM.get(d.dtype)
            if lt is None:
                continue
            x.empty("listitem", [("open", "0"), ("type", str(lt)),
                                 ("id", _id(d.quid, "d%d" % i)),
                                 ("label", d.name)])
        if is_logical:
            for i, (quid, _c) in enumerate(m.classes.items()):
                x.empty("listitem", [("open", "1"), ("type", str(LVT_CLASS)),
                                     ("id", _id(quid, "c%d" % i))])
        x.close("listitem")
    x.close("listitem")
    x.close("listview")


def _view_attrs(name, ns):
    return [("xmi.id", name), ("name", name), ("stereotype", "folder"),
            ("visibility", "public"), ("isSpecification", "false"),
            ("namespace", ns), ("isAbstract", "false"), ("isLeaf", "false"),
            ("isRoot", "false")]


def _write_datatypes(x):
    x.open("UML:Package", [("xmi.id", "Datatypes"), ("name", "Datatypes"),
                           ("stereotype", "folder"), ("visibility", "public"),
                           ("isSpecification", "false"),
                           ("namespace", "Logical View"), ("isAbstract", "false"),
                           ("isLeaf", "false"), ("isRoot", "false")])
    x.open("UML:Namespace.ownedElement")
    for i, dt in enumerate(BUILTIN_DATATYPES):
        x.empty("UML:DataType", [("xmi.id", "dt%d" % i), ("name", dt),
                                 ("stereotype", "datatype"),
                                 ("visibility", "public"),
                                 ("isSpecification", "false"),
                                 ("namespace", "Datatypes"),
                                 ("isAbstract", "false"), ("isLeaf", "false"),
                                 ("isRoot", "false")])
    x.close("UML:Namespace.ownedElement")
    x.close("UML:Package")


def _write_classes(x, m, ns):
    for i, (quid, c) in enumerate(m.classes.items()):
        cid = _id(quid, "c%d" % i)
        attrs = [("xmi.id", cid), ("name", c.name), ("visibility", "public"),
                 ("isSpecification", "false"), ("namespace", ns),
                 ("isAbstract", "true" if c.abstract else "false"),
                 ("isLeaf", "false"), ("isRoot", "false")]
        has_bodies = bool(c.attrs or c.ops)
        if not has_bodies:
            x.empty("UML:Class", attrs)
            continue
        x.open("UML:Class", attrs)
        x.open("UML:Classifier.feature")
        for j, a in enumerate(c.attrs):
            x.empty("UML:Attribute", [
                ("xmi.id", _id(a.quid, "a%d_%d" % (i, j))),
                ("name", a.name), ("visibility", a.visibility),
                ("isSpecification", "false"),
                ("type", a.type or None), ("initialValue", a.initial or None)])
        for j, o in enumerate(c.ops):
            x.empty("UML:Operation", [
                ("xmi.id", _id(o.quid, "o%d_%d" % (i, j))),
                ("name", o.name), ("visibility", o.visibility),
                ("isSpecification", "false"), ("isQuery", "false"),
                ("isAbstract", "false"), ("isLeaf", "false"),
                ("isRoot", "false")])
        x.close("UML:Classifier.feature")
        x.close("UML:Class")


def _write_associations(x, m, ns):
    for i, (quid, a) in enumerate(m.assocs.items()):
        aid = _id(quid, "as%d" % i)
        roles = a.roles
        # Umbrello 约定：roleList[0] 是 roleB，roleList[1] 是 roleA，
        # 而 <UML:Association.connection> 里的顺序必须是 A 再 B。
        ends = [roles[1], roles[0]]
        x.open("UML:Association", [("xmi.id", aid), ("name", a.name),
                                   ("visibility", "public"),
                                   ("isSpecification", "false"),
                                   ("namespace", ns)])
        x.open("UML:Association.connection")
        for k, r in enumerate(ends):
            agg = r.aggregation if r.aggregation in ("composite", "aggregate") else "none"
            x.empty("UML:AssociationEnd", [
                ("xmi.id", _id(r.quid, "e%d_%d" % (i, k))),
                ("type", _id(r.target_quid, None)),
                ("name", r.name),
                ("visibility", "public"),
                ("isSpecification", "false"),
                ("isNavigable", "true" if r.navigable else "false"),
                ("changeability", "changeable"),
                ("aggregation", agg),
                ("multiplicity", r.multiplicity or None)])
        x.close("UML:Association.connection")
        x.close("UML:Association")


def _write_generalizations(x, m, ns):
    for i, g in enumerate(m.generalizations):
        x.empty("UML:Generalization", [
            ("xmi.id", _id(g.quid, "g%d" % i)),
            ("name", ""), ("discriminator", ""),
            ("visibility", "public"), ("isSpecification", "false"),
            ("namespace", ns),
            ("child", _id(g.child_quid, None)),
            ("parent", _id(g.parent_quid, None))])


def _write_diagrams(x, m, view):
    ds = [d for d in m.diagrams if d.view == view]
    if not ds:
        return
    x.open("XMI.extension", [("xmi.extender", "umbrello")])
    x.open("diagrams")
    for i, d in enumerate(ds):
        did = _id(d.quid, "d%d" % i)
        # 图的画布尺寸按 widget 范围估一个，避免 Umbrello 里画布过小
        mx = max([w.x + w.w for w in d.widgets] + [1000.0])
        my = max([w.y + w.h for w in d.widgets] + [800.0])
        x.open("diagram", [
            ("xmi.id", did), ("name", d.name),
            ("type", str(d.dtype)), ("documentation", ""),
            ("localid", "-1"), ("showgrid", "0"), ("snapgrid", "0"),
            ("snapcsgrid", "0"), ("snapx", "20"), ("snapy", "20"),
            ("zoom", "100"), ("canvasheight", str(int(my))),
            ("canvaswidth", str(int(mx))), ("isopen", "1"),
            ("showatts", "1"), ("showops", "1"), ("showpackage", "1"),
            ("showpubliconly", "0"), ("showstereotype", "1"),
            ("showscope", "1"), ("showattributes", "1"),
            ("showoperations", "1"), ("showattsig", "1"), ("showopsig", "1"),
            ("usefillcolor", "1"), ("fillcolor", "#ffff00"),
            ("linecolor", "#ff0000"), ("linewidth", "0"),
            ("textcolor", "#000000"), ("backgroundcolor", "#ffffff"),
            ("griddotcolor", "#d3d3d3"),
            ("font", "Sans Serif,10,-1,0,50,0,0,0,0,0")])
        x.open("widgets")
        for w in d.widgets:
            wid = _id(w.ref_quid, None)
            if not wid:
                continue
            x.empty("classwidget", [
                ("xmi.id", wid),
                ("x", str(int(round(w.x)))), ("y", str(int(round(w.y)))),
                ("width", str(int(round(w.w)))),
                ("height", str(int(round(w.h)))),
                ("showattributes", "1"), ("showoperations", "1"),
                ("showpubliconly", "0"), ("showpackage", "1"),
                ("showstereotype", "1"), ("showscope", "1"),
                ("showattsigs", "601"), ("showopsigs", "601"),
                ("isinstance", "0"), ("usefillcolor", "1"),
                ("fillcolor", "#ffff00"), ("linecolor", "none"),
                ("linewidth", "0"), ("textcolor", "none"),
                ("font", "Sans Serif,10,-1,0,75,0,0,0,0,0"),
                ("usesdiagramfillcolor", "0"),
                ("usesdiagramusefillcolor", "0")])
        x.close("widgets")
        x.empty("messages")
        x.open("associations")
        tag2widget = {w.view_tag: w for w in d.widgets if w.view_tag}
        for aw in d.assoc_widgets:
            # @N 是图内局部引用，需要换算成对应 ClassView 的 quidu
            aid = _tag_to_widget(d, aw.aid, m)
            bid = _tag_to_widget(d, aw.bid, m)
            if not aid or not bid:
                continue
            # 连线端点取两端 widget 的中心，Umbrello 载入后会自己裁剪到边框。
            # Rose 的 RoleView 顶点不可靠（分布在两个子节点上），而且 Umbrello
            # 导入 Rose 时同样是从图形几何重建连线。
            wa, wb = tag2widget.get(aw.aid), tag2widget.get(aw.bid)
            if wa and wb:
                p0 = (wa.x + wa.w / 2.0, wa.y + wa.h / 2.0)
                p1 = (wb.x + wb.w / 2.0, wb.y + wb.h / 2.0)
            elif aw.points:
                p0, p1 = aw.points[0], aw.points[-1]
            else:
                p0 = p1 = None
            x.open("assocwidget", [
                # xmi.id 指向对应的 UML:Association / UML:Generalization，
                # Umbrello 靠它把图上的连线和模型里的关系绑在一起。
                # 缺了它会被当成 pre-1.2 旧格式，从而凭空再造一条关系。
                ("xmi.id", _id(aw.assoc_quid, None)),
                ("widgetaid", aid), ("widgetbid", bid),
                ("type", str(aw.type)), ("documentation", ""),
                ("roleAdoc", ""), ("roleBdoc", ""),
                ("visibilityA", "0"), ("visibilityB", "0"),
                ("changeabilityA", "900"), ("changeabilityB", "900"),
                ("indexa", "1"), ("indexb", "1"),
                ("totalcounta", "1"), ("totalcountb", "1"),
                ("linecolor", "none"), ("linewidth", "none"),
                ("textcolor", "none"), ("usefillcolor", "0"),
                ("fillcolor", "#000000"),
                ("usesdiagramfillcolor", "0"),
                ("usesdiagramusefillcolor", "0")])
            x.open("linepath")
            if p0 and p1:
                x.empty("startpoint", [("startx", str(int(round(p0[0])))),
                                       ("starty", str(int(round(p0[1]))))])
                x.empty("endpoint", [("endx", str(int(round(p1[0])))),
                                     ("endy", str(int(round(p1[1]))))])
            else:
                x.empty("startpoint", [("startx", "0"), ("starty", "0")])
                x.empty("endpoint", [("endx", "0"), ("endy", "0")])
            x.close("linepath")
            x.close("assocwidget")
        x.close("associations")
        x.close("diagram")
    x.close("diagrams")
    x.close("XMI.extension")


def _tag_to_widget(d, tag, m):
    """把图内 @N 引用换成该 widget 的 XMI id（等于其 UML 对象 id）。"""
    if not tag:
        return None
    t = tag if tag.startswith("@") else "@" + str(tag)
    for w in d.widgets:
        if w.view_tag == t:
            return _id(w.ref_quid, None)
    return None


# --------------------------------------------------------------------------
# 6. CLI
# --------------------------------------------------------------------------

def convert(src: str, dst: str, forced_enc=None, no_diagrams=False,
            quiet=False, force=False):
    if not os.path.isfile(src):
        raise Rose2XmiError("找不到输入文件: %s" % src)
    if os.path.exists(dst) and not force:
        raise Rose2XmiError("输出已存在: %s（用 --force 覆盖）" % dst)

    with open(src, "rb") as fh:
        raw = fh.read()
    text, enc, how = decode_mdl(raw, forced_enc)

    root = parse_petal(text)
    model = build_model(root)
    if no_diagrams:
        model.diagrams = []

    xml = write_xmi(model, dst, os.path.basename(src))

    d = os.path.dirname(os.path.abspath(dst)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".rose2xmi-", suffix=".xmi", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(xml)
        # mkstemp 建的是 0600，改成普通文件权限（再受 umask 约束）
        os.chmod(tmp, 0o666 & ~_umask())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    if not quiet:
        print("[rose2xmi] %s -> %s" % (src, dst))
        print("           编码: %s (%s)" % (enc, how))
        print("           类 %d / 关联 %d / 继承 %d / 图 %d"
              % (len(model.classes), len(model.assocs),
                 len(model.generalizations), len(model.diagrams)))
        for w in model.warnings:
            print("           警告: %s" % w, file=sys.stderr)
    return model


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="rose2xmi",
        description="把 Rational Rose Petal (.mdl) 转成 UTF-8 XMI（Umbrello 可正确打开）")
    ap.add_argument("inputs", nargs="+", help="一个或多个 .mdl 文件")
    ap.add_argument("-o", "--output", help="输出路径（仅在单个输入时可用）")
    ap.add_argument("--encoding", help="强制输入编码（如 gb18030 / big5）")
    ap.add_argument("--no-diagrams", action="store_true",
                    help="只转换模型，不输出图形布局")
    ap.add_argument("-f", "--force", action="store_true", help="允许覆盖输出")
    ap.add_argument("-q", "--quiet", action="store_true", help="安静模式")
    ap.add_argument("--version", action="version",
                    version="rose2xmi %s" % SCRIPT_VERSION)
    args = ap.parse_args(argv)

    if args.output and len(args.inputs) > 1:
        ap.error("-o/--output 只能配合单个输入文件使用")

    rc = 0
    for src in args.inputs:
        if args.output:
            dst = args.output
        else:
            base, _ = os.path.splitext(src)
            dst = base + ".xmi"
        try:
            convert(src, dst, args.encoding, args.no_diagrams,
                    args.quiet, args.force)
        except Rose2XmiError as exc:
            print("[rose2xmi] 错误: %s" % exc, file=sys.stderr)
            rc = 1
        except OSError as exc:
            print("[rose2xmi] 写文件失败: %s" % exc, file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
