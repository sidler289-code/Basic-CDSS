# CDSS SQLite 数据库迁移与持久化方案（修订版）

## 1. 修订背景

旧方案的方向是正确的：把 `data/` 目录下的 JSON 文件迁移到 SQLite，并保持前端 API 契约不变。但旧方案本质上是“JSON 文件等价搬表”，只覆盖了 `chunks`、`terms`、`rules`、`segments` 等运行时数据，没有把项目 v4.0 的两个核心结构作为数据库一等公民：

- 医疗知识树：当前由 `node_paths` 隐含表达，用于父子增强、同级增强、路径过滤和证据溯源。
- 医学关联图：当前由 chunk 关联、规则和 `manual_red_flag_edges.yaml` 构建，用于 Graph Augmentation、红旗边和 must-retrieve 硬约束。

本修订版将 SQLite 定位为项目的唯一轻量级权威数据源：

- JSON 文件只作为迁移输入和备份，不再作为运行时主数据源。
- SQLite 持久化知识块、树路径、实体索引、规则、图节点、图边、图构建审计和版本号。
- 当前阶段直接使用 SQLite 建图和查询；Neo4j 只保留为后续便捷接入的可选投影，用于图谱可视化、人工审核和更友好的图谱管理。
- 前端 `/api/database/*` 契约先保持兼容，后续再逐步增加树/图管理能力。

## 2. 当前数据基线

按当前仓库数据核对，迁移验收基线如下：

| 数据类型 | 当前数量 | 来源 |
|---|---:|---|
| 普通知识块 | 33 | `data/chunks/*.json`，排除 `objects.json`、`shadow_chunks.json` |
| 表格/对象块 | 12 | `data/chunks/objects.json` |
| 影子块 | 52 | `data/chunks/shadow_chunks.json` |
| 术语 | 64 | `data/terms/*_terms.json` |
| 临床规则 | 9 | `data/rules/minimal_rules.json` |
| 预切分文件 | 1 | `data/segments/*.json` |
| 预切分段落 | 13 | `1777344704_pharmacotherapeutics.json` |
| 图节点 | 111 | 当前 graph build audit |
| 图边 | 42 | 当前 graph build audit，含 41 条 `RED_FLAG_FOR` |

根目录已有 `chunk切片库.db`，当前无表。

## 3. 对旧方案的主要修订

| 旧方案 | 修订方案 | 原因 |
|---|---|---|
| 7 张数据表，主要镜像 JSON | 分层表：来源、分段、知识、树、实体、规则、图、审计 | 更贴合 Tree-RAG + Graph Augmentation |
| `node_paths` 只存 JSON | 新增 `tree_nodes`、`item_tree_paths` | 树增强和路径过滤可直接 SQL 查询 |
| `primary_entities`、`embedded_associations` 只存 JSON | 新增 `item_entities` | 支撑 QU、筛选、图种子抽取、图构建 |
| 图仍依赖 YAML / build artifacts / Neo4j | 新增 `graph_nodes`、`graph_edges`、`graph_edge_evidence` | 当前直接用 SQLite 建图；Neo4j 仅保留可选投影接口 |
| `segments_json` 整包存储 | 改为 `segment_sets` + `document_segments` 行级存储 | 支持单段状态、错误、重试和编辑持久化 |
| 只有 `data_version` | 增加 `data_version`、`tree_version`、`graph_version`、`schema_version` | 让 FAISS、树缓存、图缓存独立失效 |
| SQL 直接塞进 `chunker.py` | 增加 repository 层 | 数据库管理更简单，业务代码更干净 |

## 4. 迁移边界

### 保持不变

- DeepSeek chunk API 调用逻辑：`backend/services/chunker.py` 中 `_call_chunk_api` 和主要切片流程不改。
- `src/pre_splitter.py` 的文档预切分算法不改。
- `src/models.py` 的 dataclass 接口优先保持不变。
- `frontend/` 现有 API 调用不改。
- `backend/routes/database.py` 第一阶段路由不改，底层 repository 替换实现。
- FAISS 缓存文件路径不改：`data/vectors/`。

### 改为 SQLite 权威来源

- 知识块、对象块、影子块。
- 术语词典和别名。
- 规则库。
- 文档导入记录、预切分段落和单段切片状态。
- 树节点和 item 到树路径的多重归属。
- 图节点、图边、图证据、图构建审计。
- 数据版本、schema 版本和变更审计。

## 5. 修订后的数据库模型

### 5.1 系统与版本表

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS db_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO db_meta (key, value) VALUES
    ('schema_version', '1'),
    ('data_version', '1'),
    ('tree_version', '1'),
    ('graph_version', '1');

CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    changed_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

设计说明：

- `schema_migrations` 支持后续正式迁移，不靠手工判断库结构。
- `db_meta` 不只服务 FAISS，还服务树和图的缓存失效。
- `change_log` 让数据库管理页的编辑、删除、重切片可以追溯。

### 5.2 来源文件与预切分表

```sql
CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stored_filename TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segment_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER NOT NULL,
    total_chars INTEGER NOT NULL DEFAULT 0,
    total_tokens_estimate INTEGER NOT NULL DEFAULT 0,
    segment_budget INTEGER NOT NULL DEFAULT 0,
    segment_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS document_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_set_id INTEGER NOT NULL,
    segment_index INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    char_count INTEGER NOT NULL DEFAULT 0,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (segment_set_id) REFERENCES segment_sets(id) ON DELETE CASCADE,
    UNIQUE(segment_set_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_source_files_stored_filename ON source_files(stored_filename);
CREATE INDEX IF NOT EXISTS idx_document_segments_set_status ON document_segments(segment_set_id, status);
```

设计说明：

- 旧方案的 `segments_json` 改为行级段落，数据库管理页可以直接更新单段状态。
- API 仍可把 `segment_sets + document_segments` 重新组装成旧的 JSON 返回格式。
- `source_files` 后续可统一管理 `data/imports`、切片来源、重复上传检测。

### 5.3 知识块核心表

```sql
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    display_content TEXT NOT NULL DEFAULT '',
    node_paths_json TEXT NOT NULL DEFAULT '[]',
    knowledge_type TEXT NOT NULL DEFAULT '',
    clinical_task_json TEXT NOT NULL DEFAULT '[]',
    primary_entities_json TEXT NOT NULL DEFAULT '{}',
    embedded_associations_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    authority_level TEXT NOT NULL DEFAULT 'guideline',
    risk_priority TEXT NOT NULL DEFAULT 'medium',
    target_file TEXT NOT NULL DEFAULT '',
    source_file_id INTEGER,
    segment_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE SET NULL,
    FOREIGN KEY (segment_id) REFERENCES document_segments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS table_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL UNIQUE,
    object_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    node_paths_json TEXT NOT NULL DEFAULT '[]',
    content_markdown TEXT NOT NULL DEFAULT '',
    summary_for_embedding TEXT NOT NULL DEFAULT '',
    size_tier TEXT NOT NULL DEFAULT 'small',
    retrieval_policy TEXT NOT NULL DEFAULT 'fetch_whole_object_if_any_child_matched',
    target_file TEXT NOT NULL DEFAULT 'objects.json',
    source_file_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS shadow_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shadow_chunk_id TEXT NOT NULL UNIQUE,
    parent_object_id TEXT NOT NULL,
    shadow_type TEXT NOT NULL DEFAULT '',
    embedding_content TEXT NOT NULL DEFAULT '',
    matched_position TEXT NOT NULL DEFAULT '',
    display_policy TEXT NOT NULL DEFAULT 'do_not_show_alone',
    target_file TEXT NOT NULL DEFAULT 'shadow_chunks.json',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_object_id) REFERENCES table_objects(object_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_target_file ON chunks(target_file);
CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(knowledge_type);
CREATE INDEX IF NOT EXISTS idx_chunks_authority ON chunks(authority_level);
CREATE INDEX IF NOT EXISTS idx_chunks_risk ON chunks(risk_priority);
CREATE INDEX IF NOT EXISTS idx_table_objects_target_file ON table_objects(target_file);
CREATE INDEX IF NOT EXISTS idx_shadow_parent ON shadow_chunks(parent_object_id);
```

设计说明：

- 保留 JSON 字段是为了兼容 `src.models.Chunk.from_dict()`，降低迁移风险。
- 但树路径和实体不再只藏在 JSON 中，会同步拆到 `item_tree_paths` 和 `item_entities`。
- `status` 支持软删除，数据库管理页默认展示 active 数据，必要时可恢复。

### 5.4 树结构表

```sql
CREATE TABLE IF NOT EXISTS tree_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path_key TEXT NOT NULL UNIQUE,
    parent_path_key TEXT,
    title TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    node_type TEXT NOT NULL DEFAULT 'knowledge',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_path_key) REFERENCES tree_nodes(path_key) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS item_tree_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,               -- chunk | table_object | shadow_chunk
    item_id TEXT NOT NULL,
    tree_path_key TEXT NOT NULL,
    path_json TEXT NOT NULL DEFAULT '[]',
    path_text TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (tree_path_key) REFERENCES tree_nodes(path_key) ON DELETE CASCADE,
    UNIQUE(item_type, item_id, tree_path_key)
);

CREATE INDEX IF NOT EXISTS idx_tree_nodes_parent ON tree_nodes(parent_path_key);
CREATE INDEX IF NOT EXISTS idx_tree_nodes_depth ON tree_nodes(depth);
CREATE INDEX IF NOT EXISTS idx_item_tree_item ON item_tree_paths(item_type, item_id);
CREATE INDEX IF NOT EXISTS idx_item_tree_path ON item_tree_paths(tree_path_key);
```

设计说明：

- `path_key` 建议使用稳定路径：例如 `慢病管理/高血压/诊断`。
- `item_tree_paths` 保留多重归属，完整表达 v3.1/v4.0 的 `node_paths`。
- `TreeEnhancer.get_parent_chunks()`、`get_sibling_chunks()` 后续可以从 SQL 或内存对象双路径实现。

### 5.5 术语与实体索引表

```sql
CREATE TABLE IF NOT EXISTS terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL,
    term_type TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    embedding_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS term_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (term_id) REFERENCES terms(term_id) ON DELETE CASCADE,
    UNIQUE(term_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS item_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,               -- chunk | table_object | shadow_chunk | rule
    item_id TEXT NOT NULL,
    term_id TEXT,
    entity_type TEXT NOT NULL,             -- disease | symptom | drug | population | vitals | lab | task | context
    entity_name TEXT NOT NULL,
    role TEXT NOT NULL,                    -- primary | direct | clinical_adjacent | background | trigger | action
    source_field TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (term_id) REFERENCES terms(term_id) ON DELETE SET NULL,
    UNIQUE(item_type, item_id, entity_type, entity_name, role, source_field)
);

CREATE INDEX IF NOT EXISTS idx_terms_type ON terms(term_type);
CREATE INDEX IF NOT EXISTS idx_term_aliases_alias ON term_aliases(normalized_alias);
CREATE INDEX IF NOT EXISTS idx_item_entities_entity ON item_entities(entity_type, entity_name);
CREATE INDEX IF NOT EXISTS idx_item_entities_item ON item_entities(item_type, item_id);
CREATE INDEX IF NOT EXISTS idx_item_entities_term ON item_entities(term_id);
```

设计说明：

- `item_entities` 是本次修订的关键表。它让 chunk 主要实体、直接关联实体、规则触发实体都能被统一查询。
- Graph Builder 不再需要从 JSON 字段临时猜实体，可优先从 `item_entities` 抽取。
- 术语别名拆表后，数据库管理页可以做别名去重、查找和补充。

### 5.6 规则表

```sql
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL UNIQUE,
    rule_type TEXT NOT NULL DEFAULT '',
    trigger_json TEXT NOT NULL DEFAULT '{}',
    action_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 50,
    scope_json TEXT NOT NULL DEFAULT '{}',
    conflict_group TEXT NOT NULL DEFAULT '',
    evidence_object_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rules_type ON rules(rule_type);
CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status);
CREATE INDEX IF NOT EXISTS idx_rules_priority ON rules(priority);
```

设计说明：

- `rules` 保持轻量，复杂触发条件继续存 JSON。
- 规则涉及的药物、疾病、context 同步写入 `item_entities(item_type='rule')`。
- 图边证据通过 `graph_edge_evidence` 关联到 rule。

### 5.7 图结构表

```sql
CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_node_id TEXT NOT NULL UNIQUE,
    term_id TEXT,
    node_type TEXT NOT NULL,               -- disease | symptom | drug | risk | context | task
    canonical_name TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    is_red_flag INTEGER NOT NULL DEFAULT 0,
    severity_baseline TEXT NOT NULL DEFAULT 'chronic',
    primary_care_relevance TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (term_id) REFERENCES terms(term_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.5,
    frequency TEXT NOT NULL DEFAULT 'common',
    miss_cost TEXT NOT NULL DEFAULT 'manageable',
    primary_care_relevance TEXT NOT NULL DEFAULT 'medium',
    is_pathognomonic INTEGER NOT NULL DEFAULT 0,
    must_rule_out INTEGER NOT NULL DEFAULT 0,
    level TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'auto',        -- manual | rule_auto | chunk_auto
    reviewed_by TEXT NOT NULL DEFAULT '',
    evidence_text TEXT NOT NULL DEFAULT '',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_node_id) REFERENCES graph_nodes(graph_node_id) ON DELETE CASCADE,
    FOREIGN KEY (target_node_id) REFERENCES graph_nodes(graph_node_id) ON DELETE CASCADE,
    UNIQUE(source_node_id, target_node_id, edge_type)
);

CREATE TABLE IF NOT EXISTS graph_edge_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,           -- chunk | rule | manual | object
    evidence_id TEXT NOT NULL,
    evidence_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (edge_id) REFERENCES graph_edges(id) ON DELETE CASCADE,
    UNIQUE(edge_id, evidence_type, evidence_id)
);

CREATE TABLE IF NOT EXISTS graph_builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_version TEXT NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    pruned_count INTEGER NOT NULL DEFAULT 0,
    degree_violation_count INTEGER NOT NULL DEFAULT 0,
    audit_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'success',
    built_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_type_weight ON graph_edges(edge_type, weight);
CREATE INDEX IF NOT EXISTS idx_graph_edges_status ON graph_edges(status);
```

设计说明：

- SQLite 图表是权威图源。`InMemoryGraphStore` 和 Neo4j 都从这里加载。
- `RED_FLAG_FOR`、`CONTRAINDICATED_IN`、`INTERACTS_WITH` 等边不再只存在 YAML 或 Cypher artifact 中。
- `graph_edge_evidence` 让每条边能追溯到 chunk、rule 或人工来源，符合医疗审计需要。
- `graph_builds` 保存构建审计摘要，替代只写 `data/graph/build_artifacts/*.json` 的脆弱方式。artifact 仍可作为导出物保留。

### 5.8 Neo4j 可选接入边界

当前阶段不把 Neo4j 放进主链路，也不要求部署 Neo4j。正式数据流为：

```text
SQLite graph_nodes / graph_edges / graph_edge_evidence
    ↓ 可选导出或同步
Neo4j 只读投影
    ↓
图谱可视化、人工审核、bad case 分析、图谱管理辅助
```

约束：

- SQLite 是唯一权威图源。
- `GraphBuilder` 只向 SQLite 图表写入构建结果。
- `SQLiteGraphStore` 是默认在线查询后端。
- Neo4j 不接受主流程写入，不作为必须部署组件。
- Neo4j 中的节点和边允许删除后从 SQLite 重新投影生成。
- 如果后续要在 Neo4j UI 中编辑图谱，编辑结果必须先导出为审核补丁，再由 `GraphRepository` 写回 SQLite，不能直接把 Neo4j 当权威库。

这样保留 Neo4j 的可视化和图谱管理价值，同时避免 SQLite 与 Neo4j 双源分裂。

## 6. Repository 层编码计划

不要把 SQLite 细节直接堆进 `backend/services/chunker.py`。建议新增以下模块：

| 文件 | 职责 |
|---|---|
| `src/database.py` | 连接、事务、schema 初始化、版本号、JSON helper |
| `src/repositories/knowledge_repository.py` | chunks、table_objects、shadow_chunks、树路径、实体索引 |
| `src/repositories/segment_repository.py` | source_files、segment_sets、document_segments |
| `src/repositories/rule_repository.py` | rules 和 rule entities |
| `src/repositories/graph_repository.py` | graph_nodes、graph_edges、edge evidence、graph builds |
| `scripts/migrate_to_sqlite.py` | 一次性 JSON/YAML/artifact 迁移 |
| `scripts/rebuild_graph_sqlite.py` | 从 SQLite 知识库重建 SQLite 图表，并可选导出 Cypher |
| `scripts/export_graph_to_neo4j.py` | 可选：把 SQLite 图表投影到 Neo4j，用于可视化和审核 |

### `src/database.py` 必备接口

```python
def init_db() -> None: ...
def get_connection() -> sqlite3.Connection: ...
def transaction(): ...
def get_version(key: str = "data_version") -> str: ...
def bump_version(key: str = "data_version") -> str: ...
def json_dumps(value) -> str: ...
def json_loads(value, default): ...
```

连接策略：

- `sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)`
- `PRAGMA journal_mode=WAL`
- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=5000`
- `row_factory = sqlite3.Row`
- 写操作统一走 `transaction()`

## 7. 现有代码改造计划

### 7.1 `backend/services/chunker.py`

第一阶段保持函数签名不变，只替换内部实现：

| 函数 | 新实现 |
|---|---|
| `_write_chunks()` | 调用 `KnowledgeRepository.upsert_chunks()`，同步写入 `chunks`、`tree_nodes`、`item_tree_paths`、`item_entities`，然后 bump `data_version/tree_version/graph_version` |
| `list_all_chunks()` | 调用 repository 查询；普通块、对象块、影子块通过 SQL UNION 或统一 DTO 返回 |
| `get_chunk_by_id()` | 按 item ID 从三类表定位并组装旧格式 |
| `update_chunk()` | 更新主表 + 重建该 item 的树路径和实体索引 |
| `delete_chunk()` | 优先软删除；需要物理删除时级联清理路径和实体索引 |
| `get_chunk_files()` | 按 `target_file` 聚合 |
| `get_stats()` | SQL COUNT + GROUP BY，补充树节点数、图边数 |
| `get_segments()` | 从 `segment_sets/document_segments` 重组旧 JSON |
| `save_segments()` | 行级 UPSERT 段落 |
| `delete_segments()` | 删除当前文件的 segment set |
| `_update_segment_status()` | 单段 UPDATE，不再读写整包 JSON |

### 7.2 `src/chunk_store.py`

改造加载源：

- `_load_chunks()` 从 `chunks` 加载，并用 JSON 字段重建 `Chunk`。
- `_load_objects_and_shadows()` 从 `table_objects`、`shadow_chunks` 加载。
- `_load_terms()` 从 `terms` 和 `term_aliases` 加载。
- `_source_mtimes()` 改为返回 `{"data_version": get_version("data_version"), "tree_version": get_version("tree_version")}`。

树增强逻辑第一阶段仍可使用内存对象；后续可逐步改为 repository 查询。

### 7.3 `src/rule_engine.py`

- `_load_rules()` 改为 `RuleRepository.list_active_rules()`。
- 保留 `Rule` dataclass，不改匹配逻辑。

### 7.4 `src/graph_builder.py`

修订方向：

- 从 `KnowledgeRepository` 读取 chunks 和 `item_entities`。
- 从 `RuleRepository` 读取 rules 和 rule entities。
- 从 `GraphRepository` 读取人工图边，不再只读 `manual_red_flag_edges.yaml`。
- 构建结果写回 `graph_nodes`、`graph_edges`、`graph_edge_evidence`、`graph_builds`。
- `export_cypher()` 仍保留，用于可选 Neo4j 投影。

### 7.5 `src/graph_store.py`

新增默认后端，当前阶段固定优先使用 SQLite：

```yaml
graph_store:
  backend: "sqlite"
```

实现 `SQLiteGraphStore`：

- `get_neighbors()` 直接查询 `graph_edges + graph_nodes`。
- `load_graph()` 批量写入 SQLite 图表。
- `health_check()` 返回 SQLite 图节点/边数量。

Neo4j 后端保留接口，但定位为可选投影和管理辅助：

- 开发和县域轻量部署：默认 SQLite。
- 需要可视化和复杂排查：从 SQLite 导出到 Neo4j。
- 需要图谱人工审核：可在 Neo4j 中查看、筛查和标注候选问题，但正式修改仍回写 SQLite。
- 需要切换在线查询后端：保留 `backend: "neo4j"` 配置能力，但上线前必须确认同步来源仍是 SQLite。

### 7.6 `src/main.py`

- 初始化顺序调整为：`init_db()` → `chunk_store.load_all()` → `rule_engine.load()` → `graph_store.load/health_check()`。
- 如果 `graph_edges` 为空，提示运行 `scripts/rebuild_graph_sqlite.py`，不要每次启动都隐式重建图。
- 图失败继续降级到 v3.2 路径。

### 7.7 `backend/main.py`

服务启动时调用：

```python
from src.database import init_db

@app.on_event("startup")
async def startup():
    init_db()
```

后续可增加数据库健康端点：

- `/api/database/health`
- `/api/database/versions`
- `/api/database/graph/stats`
- `/api/database/tree`

## 8. 迁移脚本计划

`scripts/migrate_to_sqlite.py` 执行顺序：

1. `init_db()` 建表。
2. 迁移 `data/imports` 到 `source_files`，能计算 hash 的文件写入 `sha256`。
3. 迁移 `data/segments/*.json` 到 `segment_sets` 和 `document_segments`。
4. 迁移 `data/terms/*_terms.json` 到 `terms` 和 `term_aliases`。
5. 迁移 `data/chunks/objects.json` 到 `table_objects`，同步树路径。
6. 迁移 `data/chunks/shadow_chunks.json` 到 `shadow_chunks`。
7. 迁移普通 `data/chunks/*.json` 到 `chunks`，同步树路径和实体索引。
8. 迁移 `data/rules/minimal_rules.json` 到 `rules`，同步 rule entities。
9. 迁移 `data/graph/manual_red_flag_edges.yaml` 到 `graph_edges`，标记 `created_by='manual'`。
10. 用当前 chunks/rules/manual edges 重建图，写入 `graph_nodes`、`graph_edges`、`graph_edge_evidence`、`graph_builds`。
11. 写入 `schema_migrations` 和版本号。
12. 输出迁移统计，并与基线数量比较。

幂等策略：

- 所有业务 ID 使用 UNIQUE。
- 迁移脚本默认 `UPSERT`。
- 如果目标库已有数据，默认进入 dry-run；传 `--apply` 后执行。
- 支持 `--reset` 清空 SQLite 业务表后重迁移，但必须显式传参。

## 9. 数据库管理简化策略

### 9.1 管理入口简化

数据库管理页第一阶段维持现有功能：

- 文件上传。
- 预切分。
- 单段切片。
- chunk 列表、搜索、筛选、详情、编辑、删除。
- stats 和 chunk-files。

第二阶段增加三个轻量面板：

- 树路径：展示 `tree_nodes`，可查看某节点下的 chunks。
- 实体索引：按 disease/symptom/drug 查询相关 chunks 和 graph edges。
- 图状态：展示节点数、边数、红旗边数、未审核边、最近构建时间。

### 9.2 持久化简化

- 切片结果一写入 SQLite 即持久化，不再依赖 JSON 文件写入成功。
- 单段切片状态是行级更新，失败重试不会覆盖整份 `segments_json`。
- 任何影响召回的数据变更，都统一调用版本 bump：
  - chunk/object/shadow 改动：`data_version`、`tree_version`、`graph_version`
  - term/rule/edge 改动：`data_version`、`graph_version`
  - 只改 source file 元信息：不影响向量缓存
- `change_log` 记录编辑前后 JSON，方便回滚和审计。

### 9.3 备份与恢复

推荐命令策略：

```bash
sqlite3 chunk切片库.db ".backup 'backups/chunk切片库_YYYYMMDD_HHMM.db'"
```

恢复时只需要替换 `.db` 文件，`data/vectors/` 可按版本自动重建。

## 10. 验证计划

### 10.1 迁移验证

运行：

```bash
python scripts/migrate_to_sqlite.py --dry-run
python scripts/migrate_to_sqlite.py --apply
```

确认输出不少于当前基线：

- chunks = 33
- table_objects = 12
- shadow_chunks = 52
- terms = 64
- rules = 9
- document_segments = 13
- graph_edges = 42 左右，以当前构建器结果为准

### 10.2 API 兼容验证

```bash
curl "http://localhost:8000/api/database/chunks?page=1&page_size=10"
curl "http://localhost:8000/api/database/stats"
curl "http://localhost:8000/api/database/chunk-files"
curl "http://localhost:8000/api/database/segments/1777344704_pharmacotherapeutics.pdf"
```

目标：

- 返回结构与旧前端兼容。
- 搜索、筛选、分页结果正常。
- 编辑 chunk 后重查能看到更新。
- 删除 chunk 后默认列表不再出现。

### 10.3 树验证

- 任意 chunk 的 `node_paths_json` 都能在 `item_tree_paths` 找到对应行。
- `is_primary=1` 的路径每个 chunk 至多一条。
- `tree_nodes.parent_path_key` 能形成完整父子链。
- 树增强结果与迁移前一致或更稳定。

### 10.4 图验证

- `graph_nodes` 中 85% 以上节点来自 `terms` 或人工 context。
- `graph_edges` 不超过 `config/graph_construction.yaml` 中上限。
- 每条 `graph_edges` 至少有一条 `graph_edge_evidence`，人工边可用 `evidence_text` 补足。
- `RED_FLAG_FOR` 命中仍能生成 `must_retrieve_topics`。
- SQLite 图查询失败时主流程降级，不影响普通 RAG。

### 10.5 缓存验证

- 首次启动构建 `data/vectors/`。
- 第二次启动命中缓存。
- 修改 chunk 后 `data_version` 增加，FAISS 缓存失效。
- 修改图边后 `graph_version` 增加，图 store 重新加载。

## 11. 实施顺序

1. 实现 `src/database.py` 和 schema 初始化。
2. 实现 `KnowledgeRepository`、`SegmentRepository`、`RuleRepository`。
3. 实现 `scripts/migrate_to_sqlite.py`，先 dry-run 后 apply。
4. 改造 `backend/services/chunker.py`，保持 API 返回兼容。
5. 改造 `src/chunk_store.py` 和 `src/rule_engine.py`。
6. 实现 `GraphRepository` 和 `SQLiteGraphStore`。
7. 改造 `src/graph_builder.py`，把图构建结果写入 SQLite。
8. 改造 `src/main.py` 图初始化逻辑，默认使用 SQLite graph store。
9. 预留 `scripts/export_graph_to_neo4j.py`，只做从 SQLite 到 Neo4j 的可选投影。
10. 增加健康检查和版本统计端点。
11. 完成 API、树增强、图扩展、FAISS 缓存验证。

## 12. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 表数量增加，首轮实现复杂 | 分阶段落地：先核心知识/分段，再树/实体，再图 |
| JSON 字段与拆分表不一致 | repository 写入时统一同步，迁移后以拆分表为查询来源 |
| 图边质量不可控 | `graph_builds`、`graph_edge_evidence`、`reviewed_by` 强制审计 |
| SQLite 并发写入锁 | WAL、busy_timeout、短事务、单写多读 |
| 前端兼容风险 | 第一阶段不改 route 和 response shape |
| Neo4j 与 SQLite 双源分裂 | SQLite 是唯一权威源，Neo4j 只允许从 SQLite 投影 |

## 13. 最终目标

修订后的 SQLite 方案不只是把 JSON 存进数据库，而是把 CDSS 的核心结构固化下来：

- 知识块负责内容。
- 树节点负责医学知识层级和多重归属。
- 实体索引负责查询理解、筛选和图种子。
- 图节点/图边负责跨树关联、红旗约束和高危召回。
- 版本和审计负责长期维护、缓存失效、回滚和质量控制。

这样数据库管理会更简单：所有运行时数据都在一个 SQLite 文件中，备份/迁移/回滚都围绕这个文件完成；同时它也更符合项目整体的 Association-Embedded Tree-RAG + Graph Augmentation 设计。
