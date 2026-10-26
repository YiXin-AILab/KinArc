# 多模态配置

media 定义输入媒体的归档条件、模型传输方式和 Asset 句柄可见性。存储后端及下载安全策略由 Runtime 管理。

| `media` 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `llm_transport` | auto | `auto` 尽量保留可读取 URL；`base64` 在 adapter 边界转换 |
| `include_asset_id_in_context` | false | 严格布尔值；在模型发送副本中加入 Asset 句柄提示 |
| `archive_rules` | `[]` | 不匹配则不因该规则主动归档 |
| `archive_rules[].block_types` | 必填 | image/audio/video/document 的非空、不重复组合 |
| `archive_rules[].source_types` | 必填 | url/base64 的非空、不重复组合 |

匹配输入在进入 Inbox 前归档到 AssetStore；已是 AssetSource 的内容不再次归档。

## 规则与请求语义

一条规则同时匹配 block_types 和 source_types；多条 archive_rules 任意命中即可归档。
空规则列表不关闭多模态输入或 Runtime 媒体安全限制。
include_asset_id_in_context 使用 YAML 布尔值 true / false。

传输方式不改变模型的媒体能力。base64 可能增加下载耗时和请求体大小，失败由 adapter 处理。

include_asset_id_in_context=true 在模型发送副本中、相应 Asset 媒体前加入
<asset_reference>；不向 MessageManager 原始内容或公开消息新增该提示文本。
摘要模型会被要求一起保留句柄与媒体摘要，Runtime 不解析或校验摘要 XML。
关闭时已压缩且丢失的句柄，后续再打开不会自动恢复。

## 完整媒体示例

合并到现有 AgentConfig：

```yaml
media:
  llm_transport: auto
  include_asset_id_in_context: true
  archive_rules:
    - block_types: [image, audio, video, document]
      source_types: [url, base64]
```

此规则覆盖全部媒体类型和输入来源。

## 存储与业务扩展

root、bucket、鉴权、max_file_bytes、下载/上传 timeout、scheme 和网络策略由 Runtime/Asset adapter 管理。
媒体业务处理通过 Tool 扩展，存储后端通过宿主 adapter 扩展，均须保持资产引用和授权合约。

## 验证要求

覆盖每种 block/source、规则不命中、已归档输入、base64 转换、超限和下载失败，
以及模型副本有句柄而持久化/公开内容不新增提示的边界。
需要工具消费 asset_id 时，同时验证该工具的查找和授权；句柄可见不等于可访问。
