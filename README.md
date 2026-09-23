# 养殖批次状态与日期回退修复

这是水产养殖管理系统，后端维护塘口、养殖批次、投苗、投喂、水质、用药、成本和出塘销售资料，前端提供日常录入与周期分析页面。数据默认保存在 SQLite 文件中。

## 批次状态机与一致性规则

- 状态流转：`active(养殖中) → harvested(已出塘) → closed(已关闭)`。回退（如已出塘改回养殖中）必须填写原因，每次变更都会生成审计版本（`GET /api/batches/{id}/revisions/`）。
- 日期一致性：预计/实际收获日期不得早于投苗日期；已出塘必须有实际收获日期；养殖中不得保留实际收获日期（回退时自动清空并留痕）。
- 关闭/已出塘批次禁止新增投喂、用药、水质记录；已关闭批次同时冻结成本与销售记录。
- 跨塘转移仅允许养殖中的批次，校验目标塘启用状态、两个塘口的有效期以及目标塘同一时段容量（塘口的容量与有效期在塘口管理中维护）。
- 乐观锁：批次更新必须携带 `version`，版本不一致返回 409 版本冲突，后提交者不会覆盖前者。
- 历史矛盾数据：`GET /api/batches/inconsistencies/` 识别（负周期、状态与日期矛盾、挂在停用塘口等），`POST /api/batches/normalize/` 安全归一（不可能的日期清空而非篡改，塘口归属问题只报告不自动迁移），归一过程同样写审计版本。启动时会为既有 SQLite 库自动补充新列。

## 测试命令

在仓库根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译与构建命令

先安装前端依赖，再检查后端并构建前端：

```bash
python3 -m compileall -q backend/app
npm --prefix frontend install --legacy-peer-deps
npm --prefix frontend run build
```

本地启动可使用 `docker compose up --build`。开发环境不得提交真实账号、连接凭据或生产数据。
