# Mihomo 注册出口国家过滤设计

## 目标

在不破坏旧版美国出口模式的前提下，为注册机增加可配置的国家排除模式。当前本地测试模式排除美国（US）和香港（HK），允许日本、台湾、新加坡及其他经过真实出口检测的国家。

## 方案

- 保留 `select_mihomo_us_proxy` 和 `mihomo_us` 作为兼容入口。
- 新增通用 Mihomo 选择器，按节点名称排除配置中的国家标记；节点名称只负责候选初筛。
- 新增 `MIHOMO_REGISTRATION_ROUTE`、`MIHOMO_REGISTRATION_GROUP`、`MIHOMO_REGISTRATION_EXCLUDED_COUNTRIES` 配置。
- 切换节点后，通过 `auth.openai.com/cdn-cgi/trace` 读取实际 OpenAI 同域出口国家。
- 无法确认出口、出口属于排除列表或透明路由未切换成功时，注册立即失败，不回退直连。
- 显式代理和 Mihomo 透明路由均使用同一套国家校验。

## 兼容性

- 未设置新配置时继续使用原有美国模式。
- 配置解析使用现有 `os.environ`/`.env` 覆盖机制，不把环境值写入代码或日志。
- 不改变 Windows、macOS、Linux 的浏览器启动和进程终止分支。

## 测试

- 节点候选过滤：US/HK 排除，JP/SG/TW 保留。
- 当前节点排除后进行轮换。
- 无候选、控制器异常、出口无法确认、出口为 US/HK 均拒绝。
- 兼容美国模式的既有测试继续通过。
- 测试只使用伪造的 Mihomo 响应和出口数据，不访问真实代理、邮箱或注册流程。
