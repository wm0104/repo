个人常用越狱插件备份源。

源地址:
https://wm0104.github.io/repo/

ipa地址:
https://wm0104.github.io/wm0104.ipa/

trollfools源：
https://wm0104.github.io/trollfools-repo/

如有侵权，请联系我删除相关内容。

## 上传检查与有限自动修复

网页、客户端或 git 上传均由同一 Actions 流程扫描 `debs/*.deb`，不按包名、版本或文件名设置修复特例。

仅对 control 归档中的普通 `control` 文件 mode `0000 → 0644`、已有普通 `preinst/postinst/prerm/postrm` 脚本 `0000 → 0755` 自动修复；已知 control 根目录缺少读取/遍历权限时仅增加 `0555` 位。正常及非零自定义文件权限保持原样。未知、嵌套、链接、特殊类型控制成员要求人工检查。

支持 plain/gzip/xz/zstd control；只就地修改解压 tar 的 mode/checksum 字段再压缩。成员名称、顺序、uid/gid、时间、脚本内容、元数据不改变；data 和其他 ar 成员（包含原 header/payload/padding）逐字节保持。正常包完整 SHA256 不变，重复运行不会产生新修改。不执行包脚本，不安装插件。

审计报告在 Actions 日志和 Summary 分类显示 `auto-fixed / warnings / blocking`，逐包提供路径和原因。检查有界 ar/压缩/tar 完整性、安全路径、control 重复字段/多段、必要字段、架构、重复包身份及完整 data 可读性；保留原 builder 的两项已有 legacy 缺字段豁免，其余缺字段不补、不猜。多个坏包尽量一次汇总；存在 blocking 时不应用部分修复、不提交、不部署。无法通用修复所有 deb 故障，检查不代表真机 ABI 兼容、授权或签名认证。拒绝 ar 长文件名扩展和歧义控制 tar 扩展；限制单 deb/data 512 MiB、control 16 MiB。

成功后仅按审计清单暂存修复的原路径并由 Actions bot 提交；构建先通过原始严格校验，再 non-force push。捕获 checkout HEAD，提交前及 push 前比对远端 main，远端已推进则拒绝旧树发布；部署开始前再次检查最终修复 commit。GITHUB_TOKEN bot push 不触发自身递归。仓库与 Pages deb 使用同一修复字节。远端检查到实际 Pages 激活间仍存在很短的不可事务化竞态，新 main 的串行工作流会随后发布，不能声称 GitHub Pages 原子锁定远端分支。

运行：`python3 -m unittest discover -s testsuite -v`；`python3 scripts/repair_deb_permissions.py`；`python3 scripts/build_repo.py --output _site`。
