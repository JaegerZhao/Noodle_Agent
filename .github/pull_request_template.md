## 变更目标

<!-- 说明用户问题、设计边界与为什么需要这次修改。 -->

## 主要改动

-

## 验证

- [ ] `npm run check:release`
- [ ] `npm run typecheck`
- [ ] `npm test`
- [ ] `python -m pytest services/noodle/tests`
- [ ] `docker compose config --quiet`
- [ ] 若涉及前端：`npm run smoke:web`
- [ ] 若涉及 Tool/API：`npm run smoke:real`

## 风险与回滚

<!-- 数据迁移、外部 API、Prompt、版本兼容和回滚方式。 -->

## 安全检查

- [ ] 未提交 `.env`、API Key、原始 PDF、SQLite、运行日志或用户数据
- [ ] 新写操作具备审批、版本前置条件和审计
