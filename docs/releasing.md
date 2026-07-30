# 平台 Wheel 发布

## 支持矩阵

| 平台 | 架构 | MVP |
|---|---:|---:|
| Linux | x86_64 | 必须 |
| Linux | arm64 | 必须 |
| macOS | arm64 | 延后 |
| macOS | x86_64 | 延后 |
| Windows | x86_64 | 不支持 |

Wheel 使用 `py3-none-<platform>`：Python 层不依赖 CPython ABI，但包内 Runtime 是原生
可执行文件，所以绝不能发布为 `py3-none-any`。

MVP 的 Linux artifact 明确标记为 `linux_x86_64` / `linux_aarch64`，运行基线是
Debian 12（glibc 2.36）。Runtime 当前会使用该镜像提供的系统共享库，因此它不是通用
manylinux Wheel，也不应在受控产品镜像之外宣称跨发行版兼容。

## 可复现输入

发布构建必须固定：

- Efferva Git revision；
- `tinypoint/codex` fork revision；
- Rust toolchain；
- 构建平台和架构。

当前 Codex fork revision 由 `.github/workflows/wheels.yml` 的 `CODEX_REVISION` 指定。
构建 Hook 把两个源码 revision、平台 tag 与 Runtime SHA-256 同时写入包内
`efferva/_build_info.json` 和 Wheel 的 `.dist-info/extra_metadata`。线上可以执行：

```python
from efferva import runtime_build_info

print(runtime_build_info())
```

这用于定位问题，不要求产品使用方理解或提供源码 hash。

## 本机构建

固定 Debian 12 构建当前 Docker Engine 架构的 Linux Wheel：

```bash
make wheel
```

产物写入 `dist/docker`。MVP 暂不内置发布 CI；内部 Registry 发布流程稳定后再增加。

内部 Registry 发布只允许手动触发，并要求：

- workflow input `publish=true`；
- `repository_url` 指向内部上传端点；
- Repository secrets `EFFERVA_REGISTRY_USERNAME` 和
  `EFFERVA_REGISTRY_PASSWORD`。

未配置 Registry 地址和凭据时流水线只构建、测试并保存 artifact，不会猜测发布目标。公开
PyPI 发布应在内部 Registry 与真实产品接入稳定后作为单独审批流程增加。

## 后续公共发布工程

以下工作不属于当前 Linux Wheel MVP：

- 在 manylinux 构建环境中确定最低 glibc 基线；
- 静态链接不在 manylinux 白名单内的依赖，或用 `auditwheel` 正确打包共享库；
- 对产物运行 manylinux 合规审计和跨发行版安装矩阵；
- 合规后再把 Linux tag 从 `linux_*` 改为相应 `manylinux_*`。

在这些验收完成前，内部 Registry 的 Linux Wheel 只供 Efferva 提供的 Debian 12
产品镜像消费。
