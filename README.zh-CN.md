# slot-scheduler

English README: [README.md](README.md)

`slot-scheduler` 是一个面向混合算力环境的小型实验调度器，适合直接放在代码仓库里使用。

它针对的是一种很常见、但往往很乱的真实环境：

- 有些任务要走 Slurm
- 有些任务只能走普通 SSH
- 有些任务就在本机跑

与其把这些逻辑全部硬编码进某个项目脚本里，不如把资源清单、后端启动方式和队列调度逻辑单独拆出来，这就是 `slot-scheduler` 的定位。

## 定位

`slot-scheduler` 并不打算和 ClearML 这一类完整实验平台竞争。

ClearML、SkyPilot、基于 Ray 的平台，以及类似系统，通常都能提供比它更多的功能：

- 实验追踪
- artifact / 模型管理
- Web UI 和 dashboard
- pipeline 和更高层的编排能力
- 集群级或云级控制平面

这个项目存在的原因，是这些系统在某些场景下会显得太重。

这里真正追求的是：

- 不需要部署 server
- 不需要维护数据库
- 不要求训练代码接入某个 SDK
- 不假设所有算力都归同一个集群管理器统一管理
- 使用 plain files、plain YAML、plain shell / Python 命令

所以更准确地说，`slot-scheduler` 是一个面向混乱研究算力环境的轻量调度层，而不是一个完整的 MLOps 平台。

## 它能做什么

- 把每个可运行资源抽象成一个 `slot`
- 支持 `local`、`ssh + tmux`、`slurm` 三种 backend
- 从 YAML 读取 slot 和 job 定义
- 每个 slot 同一时刻只跑一个 job
- 轮询运行中的任务，并在 slot 空出来后自动补位
- 写入 JSONL 事件日志，便于排查和后处理
- 为每个 job 记录 console log 和 exit-code 标记文件

## 它暂时不做什么

- 不做自动 rsync / 代码同步
- 不做 DAG / 依赖调度
- 不做 autoscaling 或按 GPU 利用率的动态装箱
- 不引入数据库，状态就是普通文件

这是有意为之。第一版的目标就是做一个简单、透明、可 hack 的调度层，让你可以把它接到不同 repo 上使用。

## 适合什么场景

如果你希望做到下面这些事，它会比较合适：

- 不改现有训练脚本
- 直接调度普通 shell 或 Python 命令
- 在同一个 inventory 里混用 `local`、`ssh`、`slurm`
- 在一个普通 repo 里直接运行，不额外部署平台
- 让状态文件足够透明、可以直接 grep
- 需要快速改调度策略，而不是接一个庞大平台

## 什么时候该用别的工具

如果你需要下面这些能力，通常应该选更重的平台：

- 实验追踪、lineage、artifacts、dashboard
- 多用户权限管理和租户隔离
- 大规模托管云资源调度
- autoscaling 集群和基础设施优化
- pipeline / workflow graph / 生产级编排
- 面向非程序用户的 UI-first 工作流

这种情况下，ClearML、SkyPilot、Ray 生态、Dask、Runhouse、Ansible 或 GNU Parallel 往往更适合，具体取决于你的问题形态。

## 非目标

这个 repo 明确不打算变成：

- ClearML 替代品
- 实验追踪系统
- workflow engine
- 云资源控制平面
- 分布式运行时

它的价值主张更窄一些：在不强迫其他技术栈改变的前提下，让一小批异构机器尽可能稳定地跑实验。

## 目录结构

```text
slot-scheduler/
├── pyproject.toml
├── README.md
├── README.zh-CN.md
├── examples/
│   ├── inventory.leap2.yaml
│   ├── inventory.mixed.yaml
│   ├── inventory.txstate-ssh.yaml
│   └── jobs.demo.yaml
├── src/slot_scheduler/
│   ├── backends.py
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── scheduler.py
│   └── state.py
└── tests/
```

## 配置格式

Inventory 示例：

```yaml
defaults:
  password_env: LEAP2_PASSWORD
  poll_seconds: 30

host_policies:
  - host: gpu2-001
    max_active_fraction: 0.5

slots:
  - name: gpu1-001
    backend: slurm
    host: gpu1-001
    node: gpu1-001
    gpu: 0
    partition: gpu1
    gres: gpu:1
    cpus_per_task: 48
    time_limit: 24:00:00
    workdir: /mmfs1/home/sqp17/Projects/VLMLP
    tags: [a100, leap2]

  - name: gpu2-001-g0
    backend: ssh
    host: gpu2-001
    gpu: 0
    workdir: /var/tmp/sqp17/code/VLMLP
    run_root: /var/tmp/sqp17/slot-scheduler/runs
    tags: [ssh, spillover]
```

`host_policies` 是可选项。如果某台机器没有配置策略，`slot-scheduler` 会默认贪婪地使用它的全部 slots。只要配了策略，就会限制这台机器同一时刻最多能占用多少个 slot。

当前支持的 host policy 字段有：

- `max_active_slots`：该机器最多允许同时活跃的 slot 数
- `max_active_fraction`：按该机器声明的 slot 数量计算比例上限

例如，一台 2-GPU 机器如果配置了 `max_active_fraction: 0.5`，那它同一时刻最多只会运行 1 个 slot。

如果你已经在 `~/.ssh/config` 里配置了 SSH alias，也可以直接这么写：

```yaml
slots:
  - name: sun
    backend: ssh
    host: sun
    run_root: /home/sqp17/slot-scheduler/runs
    tags: [ssh, sshkey]
```

Jobs 示例：

```yaml
jobs:
  - name: smoke-a
    command: ["bash", "-lc", "hostname && sleep 5"]

  - name: only-on-slurm
    slots: [gpu1-001]
    command: ["bash", "-lc", "nvidia-smi && sleep 10"]

  - name: ssh-or-local
    backends: [ssh, local]
    retries: 1
    env:
      OMP_NUM_THREADS: "8"
    command: ["python", "-c", "print('hello from slot-scheduler')"]
```

每个 job 都可以按需加这些约束：

- `slots`：只允许跑在指定 slot 上
- `backends`：只允许跑在指定 backend 上
- `required_tags`：要求目标 slot 必须带这些 tags
- `retries`：任务非零退出后允许自动重试的次数

## 使用方式

创建一个运行目录并启动：

```bash
uv run slot-scheduler run \
  --inventory examples/inventory.mixed.yaml \
  --jobs examples/jobs.demo.yaml \
  --run-dir .runs/demo
```

查看一个 run 的简要状态：

```bash
uv run slot-scheduler status --run-dir .runs/demo
```

只做 dry-run，不真正启动任务：

```bash
uv run slot-scheduler run \
  --inventory examples/inventory.mixed.yaml \
  --jobs examples/jobs.demo.yaml \
  --run-dir .runs/demo \
  --dry-run
```

如果你的 SSH 机器已经通过 `~/.ssh/config` 和 key-based login 打通了，就不需要额外配置密码环境变量。

## 状态文件

每次运行都会写这些内容：

- `state.jsonl`：事件日志
- `console/*.log`：本地 backend 的日志，以及 SSH / Slurm 任务对应的目标日志路径
- `status/*.exitcode`：exit-code 标记文件

`state.jsonl` 的设计目标是简单、可 grep、方便你以后自己做统计、脚本处理或接 dashboard。

## 给 VLMLP 的说明

这个 repo 故意只做到通用调度层为止。像下面这些内容：

- 从历史 metrics 里挑最优策略
- 拼 `run_experiment.py` 的命令行参数
- 往远端机器同步 repo 文件

都更适合放在具体实验 repo 里，或者放在 `slot-scheduler` 之上的一层薄适配器里。
