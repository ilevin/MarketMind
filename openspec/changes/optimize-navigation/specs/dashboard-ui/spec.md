## REMOVED Requirements

### Requirement: 导航栏用户信息
**Reason**: 导航整体重构为全站两级导航（主导航分区 + 分区子导航 + 用户菜单），用户信息展示与入口的规范由新能力 `site-navigation` 统一承载，避免同一导航行为在两个能力中重复规范。
**Migration**: 用户名/角色展示、「修改密码」「退出登录」入口及「退出登录撤销 Session 跳转 `/login`」的要求迁移至 `site-navigation` 的『用户菜单』requirement；管理员管理入口的可见性要求迁移至 `site-navigation` 的『主导航分区与子导航条目』requirement。
