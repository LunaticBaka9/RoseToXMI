# RoseToXML — 把 Rational Rose `.mdl` 转成 Umbrello 能正确打开的 XMI

## 这个目录解决什么问题

Rational Rose 的 `.mdl` **不是 XML**，而是它自有的 Petal 格式（一种类 Lisp 的文本）：

```
(object Petal
    version     50
    _written    "Rose 2006.0.0.060314"
    charSet     134)          ← 134 = GB2312/GBK 简体中文

(object Design "Logical View"
    root_category (object Class_Category "Logical View"
        logical_models (list unit_reference_list
            (object Class "图书"
                quid "6ACECDF9036E"
                class_attributes (list class_attribute_list
                    (object ClassAttribute "图书ID" quid "6ACED02801CE")
                    ...
```

## 用法

```bash
# 最常用：在 .mdl 同目录生成同名 .xmi
python3 rose2xmi.py 图书管理系统.mdl

# 指定输出
python3 rose2xmi.py model.mdl -o out.xmi

# 批量
python3 rose2xmi.py *.mdl --force

# 只要模型，不要图形布局
python3 rose2xmi.py model.mdl --no-diagrams

# 强制输入编码（文件头没有 charSet 或声明错时）
python3 rose2xmi.py model.mdl --encoding big5
```


| 选项                | 说明                               |
| ------------------- | ---------------------------------- |
| `-o, --output PATH` | 输出路径（仅单个输入时可用）       |
| `--encoding ENC`    | 强制输入编码，如`gb18030` / `big5` |
| `--no-diagrams`     | 只转模型，不输出图形布局           |
| `-f, --force`       | 允许覆盖已存在的输出（默认拒绝）   |
| `-q, --quiet`       | 安静模式                           |
| `--version`         | 版本号                             |

只用 Python 3 标准库，不联网、不装包。默认**不会**修改或删除原始 `.mdl`。

## 验证

`verify_umbrello.sh` 用真实 Umbrello 无界面加载并导出图片：

```bash
./verify_umbrello.sh 图书管理系统.xmi /tmp/out
```

Umbrello 是 GUI 程序，脚本用 `QT_QPA_PLATFORM=offscreen` + `--export png` 驱动它，
所以能真实反映「打开」的效果：

* 退出码 0 且生成了 PNG → 文件被成功解析；
* 生成的 PNG 文件名是正确中文 → 编码正确（乱码时文件名里会出现 `U+FFFD`）；
* 打开 PNG 看内容 → 图和文字是否正确。

手动等价命令：

```bash
QT_QPA_PLATFORM=offscreen flatpak run --filesystem=home --command=umbrello6 \
    org.kde.umbrello --export png --directory /tmp/out 图书管理系统.xmi
```

## 输出格式

**XMI 1.2 / UML 1.4**，UTF-8。选它的依据：这是 Umbrello 的默认保存方言
（`umldoc.cpp` 里 `uml2 == false` 的分支），读入端向后兼容，同时也能被
ArgoUML / Modelio / StarUML 等工具读取。

### 映射关系


| Rose Petal                                                                                    | XMI                                                                      |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `Design` 的 `root_category` / `root_usecase_package` / `root_subsystem` / `process_structure` | `UML:Model` 视图根（Logical / Use Case / Component / Deployment）        |
| `Class_Category`                                                                              | `UML:Package`                                                            |
| `Class`                                                                                       | `UML:Class`                                                              |
| `ClassAttribute`                                                                              | `UML:Attribute`（在 `UML:Classifier.feature` 里）                        |
| `Operation`                                                                                   | `UML:Operation`                                                          |
| `Association` + 两个 `Role`                                                                   | `UML:Association` + `UML:Association.connection` → `UML:AssociationEnd` |
| `Inheritance_Relationship`                                                                    | `UML:Generalization`（`child` / `parent`）                               |
| `ClassDiagram` / `UseCaseDiagram` / `Module_Diagram` / `Process_Diagram`                      | `diagram type="1/2/7/8"`                                                 |
| `ClassView`                                                                                   | `<classwidget xmi.id="<类 id>">`                                         |
| `AssociationViewNew` / `InheritView` + `RoleView`                                             | `<assocwidget xmi.id="<关系 id>">` + `<linepath>`                        |

坐标按 Umbrello 的换算系数处理（`petaltree2uml.cpp: const qreal Rose2Qt = 0.5`），
并且 Rose 的 `location` 表示对象**中心**，所以左上角 = `中心 × 0.5 − 尺寸/2`。

### 编码识别

优先读文件头 `(object Petal ... charSet N)`，映射表与 Umbrello 的
`encodinghelper.h` 一致（`134→gb18030`、`136→big5`、`128→cp932`、`129/130→cp949` …）；
读不到或解不开时，按 `utf-8 → gb18030 → big5 → cp1252` 顺序做严格解码探测。

## 树形视图里看不到类图 / 用例图？（已修复）

**症状**：转换后的 XMI 在 Umbrello 里能打开，「图表」窗口里也能列出所有图，
但左侧**树形视图**里就是没有这些图，因而无法从树上双击打开。

**根因**：原来的输出漏了文件末尾 `<XMI.extensions>` 里的 **`<listview>`** 元素。

关键在于 Umbrello 的两条路径不对称（`umbrello/umldoc.cpp`）：

```cpp
void UMLDoc::addView(UMLView *view)      // ← 从 XMI 加载图表走这里
{
    ...
    f->addView(view);
    m_diagramsModel->addDiagram(view);   // 图表窗口有数据了
    ...
    // 注意：这里【没有】emit sigDiagramCreated !
}

bool UMLDoc::createDiagram(...)          // ← 交互式新建图表才走这里
{
    ...
    addView(pView);
    Q_EMIT sigDiagramCreated(...);       // 只有这里会通知树形视图
}
```

而树形视图里的图表项只由 `UMLListView::slotDiagramCreated()`
（连到 `sigDiagramCreated`）创建。所以**从文件加载进来的图表永远不会自动进树**，
必须靠文件里的 `<listview>` 显式恢复。类/属性/操作不受影响，
因为 `sigObjectCreated` 是会正常发出的。

**修复**：现在会输出完整的 `<listview>`，格式与 Umbrello 自己保存的完全一致，
给每张图一条 `<listitem>`，按类型给出正确的 `ListViewType`：

| 图类型 | listitem type | 含义 |
|---|---|---|
| 类图 | 807 | `lvt_Class_Diagram` |
| 用例图 | 805 | `lvt_UseCase_Diagram` |
| 构件图 | 819 | `lvt_Component_Diagram` |
| 部署图 | 825 | `lvt_Deployment_Diagram` |

另外 `<docsettings viewid="...">` 现在指向**内容最多的那张图**（通常是类图），
打开文件时会直接显示它，而不是空白的默认图。

> ⚠️ 一个 Umbrello 的坑：`<listitem type="836" id="Entity Relationship Model"/>`
> 会让 Umbrello 26.08.2 **段错误**（`loadChildrenFromXMI()` 走到
> `lvt_EntityRelationship_Model` 分支）。所以本脚本**故意不输出**这一条。
> ER 视图照样在树里，因为它是通过 `slotObjectCreated` 正常建出来的。

## 已知限制

* **不解析外部子模型**（`.cat` / `.sub`，即 Petal 里的 `file_name` 引用）：
  脚本会给出 warning 并跳过，不会中断整个转换。
  这类模型需要先设好 Rose 的路径变量再单独处理。
* 序列图 / 协作图 / 状态图 / 活动图的**图形元素**尚未映射
  （类图、用例图、构件图、部署图已覆盖；这些图的模型元素不受影响）。
* Rose 里 `ClassAttribute` 经常只有名字没有类型
  （本案例 55 个属性全都只有 `quid`），转换后类型为空是**忠实于源文件**的，
  Umbrello 日志里的 `UMLAttribute::load1(...): cannot find type.` 属于正常现象。

## 关于 Umbrello 的 `Missing widget for association` 警告

用 Umbrello 加载 XMI 时，日志里常出现：

```
Missing widget for association:  ""  ( "q6ACECE5F0332" )
```

这是 Umbrello `UMLDoc::Private::checkAssociationWidgetsAfterLoad()` 的**自愈**步骤：
加载后发现有模型里的关系在图上没有对应 widget，就自动补一个。

实测结论：**这是 Umbrello 26.08.2 自身的 XMI 1.x 加载行为，不是转换错误。**

* 用 Umbrello 官方仓库自带的 `models/diagrams/class/all_associations.xmi` 测试，
  同样报 6 条该警告；
* 手写一个最小 XMI（2 个类 + 1 条关联 + 1 张类图，字段完全按官方格式）测试，
  同样报 1 条；
* 但两者导出的图片里关联线**只画了一条，位置和方向都正确**，没有重复。

所以这个警告可以忽略，判断转换是否成功请看导出的 PNG。
