# 图标与 Emoji 素材来源

`catalog.json` 是离线搜索目录，不会在界面运行时请求第三方站点。

- SVG 图标来自 [Lucide Icons](https://github.com/lucide-icons/lucide) 固定提交 `a79b2d131dab2bf20cb224bd0937b439a9c4fa99` 的 `icons/` 目录。原始许可证完整保存在 `licenses/lucide.txt`：主体为 ISC，文件中列出的 Feather 衍生图标同时保留 MIT 声明。
- Emoji 候选来自 Unicode 官方 [emoji-test.txt 15.1](https://unicode.org/Public/emoji/15.1/emoji-test.txt)，仅使用 fully-qualified 且 Emoji 版本不高于 15.1 的条目，过滤肤色及头发组件重复项。Unicode License V3 的官方副本保存在 `licenses/unicode.txt`。

构建时只移除了 Lucide SVG 的 `class` 属性，并未改变路径、描边或填充语义；所有目录内 SVG 都需要通过工作台 `appearance.validate_icon` 的受限标签与属性校验。

只读工作台图标来自 Lucide 固定提交 a79b2d131dab2bf20cb224bd0937b439a9c4fa99，保留原 SVG；许可见 LUCIDE-LICENSE。与本次 Figma 只读设计使用的图标同源。
