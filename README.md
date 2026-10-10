个人常用越狱插件备份源。

源地址:
https://wm0104.github.io/repo/

ipa地址:
https://wm0104.github.io/wm0104.ipa/

如有侵权，请联系我删除相关内容。

## 自动发布

将 `.deb` 上传到 `debs/` 并提交至 `main` 后，Actions 会自动校验软件包、生成 `Packages`（及 bz2/xz/zst 压缩索引）、生成带 SHA256 的 `Release`，最后统一部署到 GitHub Pages。删除 deb 或其他 main 提交也会重新部署。可在 Actions → Publish APT repository → Run workflow 手动发布。

索引只存在于 Pages 发布产物，不再由机器人提交回源码分支。请勿手动维护 `Packages*` 或 `Release`。GitHub Settings → Pages 的 Source 必须设置为 **GitHub Actions**，不能使用旧的分支部署。

构建或校验失败时不会部署，线上保留上次成功版本；详见 Actions 日志。更新插件时应提高 deb 内部的 Version，不要静默替换同版本文件。新包必须包含 Package、Version、Architecture、Maintainer 和 Description，架构须为 iphoneos-arm64e。同包同版本同架构不允许重复。

两个已有包的缺字段暂作为精确版本例外并输出警告：cn.wkk.swipeextenderx 0.0.2（Description）、llld.keyboard 2.0（Maintainer）。本流程不修改这些第三方 deb，更新版本应补全字段。

本源面向 RootHide，包架构字段不代表完成真机兼容性验证。请自行确认插件与 iOS、bootstrap 和目标 App 版本的兼容性。

本地构建（需要 Python 3、dpkg-dev、zstd）：

```sh
python3 scripts/build_repo.py --output _site
```

网页资源可放在 `assets/`，详情页可放在 `depictions/`，两者会一并部署。构建不会执行 deb 的安装脚本。Release 目前未签名，SHA256 完整性检查不等于独立的来源认证。
