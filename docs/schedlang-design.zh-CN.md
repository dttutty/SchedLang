# SchedLang 设计笔记

这份文档把 `SchedLang` 这个实验性调度 DSL 的设计系统化一下。

当前实现还很轻，只支持：

- `pool`
- `policy`
- `experiment`
- `matrix`

它已经够做一个“能编译、能跑”的前端原型，但还不是一门完整的调度语言。

这份文档主要回答三件事：

1. 这门语言的核心对象模型是什么
2. 哪些属于 `resource capability`，哪些属于 `task requirement`，哪些属于 `scheduling preference`
3. 下一版更像样的语法应该往哪边长

## 设计目标

`SchedLang` 不是为了替代 `slot-scheduler` 的 runtime，而是作为它前面的一层语言，专门表达“调度意图”。

它特别适合下面这种研究算力环境：

- 有本机 worker
- 有 SSH 机器
- 有 Slurm 节点
- 有像 EC2 Spot 这种可抢占的云资源
- 有共享服务器，不能随便占满
- 有些任务只要 1 张 GPU
- 有些任务只是“偏好”某类机器，但不是硬性要求

runtime 继续保持简单；语言负责把“我想怎么调度”说清楚。

## 三层语义

这门语言最重要的是把三种语义分开。

| 语义层 | 含义 | 例子 |
| --- | --- | --- |
| resource capability | 资源本身拥有什么 | `backend = ssh`、`provider = "runpod"`、`gpu_count = 8`、`gpu_mem_gb = 80`、`tags = ["shared", "a100"]` |
| task requirement | 任务必须满足什么 | `gpu_count >= 4`、`backend in ["ssh", "slurm"]`、`provider in ["runpod", "vast"]`、`host_tags contains "a100"` |
| scheduling preference | 在多个合法解里更想怎么选 | `prefer local`、`avoid shared`、`spread across hosts`、`pack small jobs` |

这三层一定要拆开，否则语言会很快变乱。

比如：

- “`sun` 是共享服务器”是资源事实
- “这个任务不能跑在共享服务器上”是任务要求
- “优先不用共享服务器，不得已再退回去”是调度偏好

对于可抢占资源，这个区分仍然成立：

- “这个 pool 是 Spot 支撑的”是资源事实
- “这个任务可以接受被抢占”是任务能力或要求
- “优先 On-Demand，不够再 spill 到 Spot”是调度偏好

## 生命周期与抢占语义

除了 placement，这门语言还需要能表达运行时生命周期。

这在下面这些资源上尤其重要：

- EC2 Spot Instances
- 其他云上的 preemptible VM
- 集群里低优先级、可撤销的队列

因为这时候问题不只是：

- 任务能不能跑在这里

还包括：

- 资源快没了的时候该怎么处理
- 任务能不能恢复
- 可恢复状态要写到哪里
- 恢复时是 restart 还是 resume

所以这里其实还存在一个横切的第四维：

| 生命周期关注点 | 含义 | 例子 |
| --- | --- | --- |
| preemption semantics | 资源可能怎么被打断 | `market = spot`、`preemptible = true`、`rebalance_signal = true` |
| drain semantics | 资源即将消失前要做什么 | `stop_accepting_new_work`、`checkpoint-and-exit`、`flush-metrics` |
| recovery semantics | 任务之后怎么继续 | `resume-from-checkpoint`、`restart-from-scratch`、`requeue-on-preemption` |

这一层会横跨前面的三层语义，因为它同时涉及：

- 资源事实
- 任务能力
- scheduler/runtime 的策略

看起来只是差一句话，实际语义完全不同。

## 核心对象模型

我建议这门语言围绕三组对象来设计。

### 资源侧对象

#### `HostCapability`

表示一台机器作为调度目标的能力。

建议字段：

- `name`
- `backend`
- `provider`
- `address` 或 `alias`
- `gpu_count`
- `gpu_mem_gb`
- `cpu_count`
- `ram_gb`
- `interconnect`
- `tags`
- `shared`
- `labels`
- `market`
- `preemptible`
- `interruption_behavior`
- `rebalance_signal`

它回答的问题是：

“这台机器到底是什么样的资源？”

比如：

- `sun` 是一台通过 SSH 登录的 8 卡机器
- `moon` 是一台通过 SSH 登录的 2 卡机器
- `gpu1-003` 是一台通过 Slurm 可达的单卡节点
- 一台 RunPod 机器仍然可以是 `backend = "ssh"`，同时带 `provider = "runpod"`
- 一个 EC2 worker pool 也可能是 `market = "spot"` 且 `preemptible = true`

#### `SlotCapability`

表示主机内部一个可分配单元。

建议字段：

- `name`
- `host`
- `gpu_indices`
- `exclusive`
- `capacity`
- `run_root`
- `workdir`
- `tags`

现在的 `slot-scheduler` 本质上就是：

- 一个 slot 同时最多一个 job

以后如果真的要支持“小任务共享一张卡”，可以通过 `capacity` 或更细粒度的 slot 模型来表达。

#### `Pool`

表示一组可复用的资源选择范围，或者一组任务默认约束。

它适合表达：

- 某个实验室的一批 SSH 机器
- 所有 A100 主机
- 所有共享主机
- 所有允许 spillover 的后备资源

### 任务侧对象

#### `TaskTemplate`

表示一个尚未展开的任务模板。

建议字段：

- `name`
- `run`
- `env`
- `cwd`
- `matrix`
- `retries`
- `requirements`
- `preferences`
- `checkpoint`
- `resume_policy`
- `preemption_policy`

它和当前实现里的 `experiment` 最接近。

#### `TaskInstance`

表示矩阵展开之后的一个具体任务。

建议字段：

- `name`
- `resolved_command`
- `resolved_env`
- `requirements`
- `preferences`
- `retry_budget`

runtime 真正调度的应该是 `TaskInstance`，不是模板本身。

#### `RequirementSet`

表示任务的硬约束，也就是“不满足就不能跑”。

建议字段：

- `backend`
- `host`
- `provider`
- `pool`
- `slot`
- `gpu_count`
- `gpu_mem_gb`
- `cpu_count`
- `ram_gb`
- `required_tags`
- `required_labels`
- `co_located`
- `same_host`

例如：

- 需要至少 4 张 GPU
- 需要至少 40 GB 显存
- 必须跑在 `ssh` 或 `slurm`
- 必须跑在带 `multi-gpu` 标签的主机上

#### `CheckpointPolicy`

表示任务怎样把“可恢复状态”持久化下来。

建议字段：

- `path`
- `storage`
- `interval`
- `on_rebalance`
- `on_interruption`
- `finalize_artifacts`

这里最重要的一条设计原则是：

**不要把“最后两分钟抢救文件”当作主恢复机制。**

更合理的模型应该是：

- 平时就持续写 checkpoint 到持久存储
- 被抢占时只做最后一次 flush / finalize

#### `ResumePolicy`

表示任务被中断后该怎样继续。

建议字段：

- `mode`
- `checkpoint_selector`
- `max_resume_age`
- `requeue_on_preemption`

典型模式：

- `restart`
- `resume`
- `resume-if-possible`

### 生命周期侧对象

#### `PreemptionPolicy`

表示当任务运行在可抢占资源上时，runtime 应该怎么处理。

建议字段：

- `tolerates_preemption`
- `preferred_market`
- `fallback_market`
- `on_rebalance`
- `on_interruption`
- `max_preemptions`

它回答的问题包括：

- 这个任务能不能跑在 Spot 上
- scheduler 是应该优先 Spot，还是尽量避免 Spot
- 收到 rebalance/interruption 时，是 checkpoint-and-requeue 还是直接 fail
- 被抢占几次之后，要不要升级到 On-Demand

#### `DrainPolicy`

表示一台即将消失的 worker 在优雅退出前，应该按什么顺序执行哪些动作。

建议字段：

- `stop_new_work`
- `checkpoint_timeout_sec`
- `flush_logs`
- `upload_small_artifacts`
- `graceful_exit`

它和 `CheckpointPolicy` 是故意分开的。

- `CheckpointPolicy` 关注“什么状态需要保存”
- `DrainPolicy` 关注“快没时间时按什么顺序收尾”

### 调度侧对象

#### `HostPolicy`

表示附着在资源上的运行策略或运营约束。

例如：

- `max_active_slots = 4`
- `max_active_fraction = 0.5`
- `reserved_for = ["large_jobs"]`

这部分当前 runtime 已经有一个很小的版本了。

#### `PreferenceSet`

表示软约束，也就是“合法解里更想怎么选”。

例如：

- 优先用 `a100`
- 优先用 `runpod` 这一类 provider
- 尽量别用 `shared`
- 同一组 sweep 尽量分散到不同主机
- 小任务尽量 pack 到少数机器上

和 requirement 不同，preference 不会让 placement 变非法，只会影响排序。

#### `PlacementDecision`

表示一次具体调度结果：

- 某个 `TaskInstance`
- 被放到哪个 host/slot
- 为什么是这个位置赢了

语言一旦复杂起来，可解释性就会变得非常重要。

## 硬约束 vs 软偏好

这两类东西一定要在语法层面分开。

### 硬约束

硬约束决定“能不能放”。

例如：

- 需要 4 张 GPU
- 需要大于等于 40 GB GPU 显存
- 必须用 `ssh` 或 `slurm`
- 必须是 `multi-gpu` 主机

如果没有任何资源满足这些条件，系统就应该明确告诉用户这个任务不可调度。

### 软偏好

软偏好决定“在能放的前提下更倾向哪里”。

例如：

- 优先 A100
- 优先本地机器
- 尽量不用共享机器
- 这一批实验尽量分散到不同 host

如果偏好满足不了，任务仍然应该在最好的合法位置继续跑，而不是直接失败。

## Spot 和其他可抢占资源

对于 EC2 Spot 这类资源，我觉得语言不应该把“被打断”当成附属细节。

比较合理的模型应该是：

1. 先通过单独的 provisioner 或 pool 申请可抢占容量
2. 把起来的 worker 注册成 runtime slots
3. 持续监听 rebalance/interruption 信号
4. 触发 drain
5. 做 checkpoint
6. 把任务 requeue 或 resume 到别处

这通常比“等到抢占通知来了再临时把文件收回来”更稳。

最后那 2 分钟太短，也不够可靠，不能作为主要持久化机制。

所以语言设计上更应该默认：

- 重要状态是持续 checkpoint 的
- 小的最终产物可以在 drain 阶段补上传
- 主恢复机制是 `checkpoint + requeue`
- 不是临时 emergency rsync

## 编译管线

比较完整的 `SchedLang` 编译流程应该长这样：

1. 把 `.sched` 源码解析成 AST
2. 归一化成一个 typed IR，里面显式区分资源对象、任务对象、策略对象
3. 展开 `matrix`，得到一批 `TaskInstance`
4. 解析 `pool` 和默认值继承
5. 把任务 requirement 和资源 capability 做匹配，得到候选 placement
6. 用 preference 和 host policy 给候选 placement 排序
7. 给每个任务挂上生命周期和恢复元数据
8. 输出：
   - 编译后的 `jobs.yaml`
   - 可选的派生 `inventory.yaml`
   - 可选的 placement plan / explain report
   - 可选的 drain / recovery plan

当前原型实际上只做了其中一小段：

- parse
- 编译成 `jobs.yaml`
- 把 `policy` 编译到派生 `inventory.yaml`

它还没有做：

- DSL 内显式声明 resource capability
- 更强的 requirement 求解
- preference 打分
- explainability 输出
- preemption-aware recovery planning

## 当前原型和长期模型的对应关系

| 当前构造 | 在长期模型里的大致角色 |
| --- | --- |
| `pool` | 一组可复用的任务侧默认约束，比如 `backends`、`required_tags`、`slots` |
| `policy` | 主机级运行策略 |
| `experiment` | 任务模板 |
| `matrix` | 任务展开 |

现在最缺的一层，其实是：

**DSL 里还没有真正显式的 resource model。**

目前资源能力主要还放在 `inventory.yaml` 里，这对第一阶段完全没问题；但如果想支持更复杂的规则，后面大概率还是要把一部分资源语义提升进 DSL。

## 更像样的语法草案

下面这套语法是设计草案，不是当前已经实现的语法。

### 资源声明

```text
host sun {
  backend = "ssh"
  alias = "sun"
  gpu_count = 8
  gpu_mem_gb = 24
  cpu_count = 128
  tags = ["shared", "lab", "txstate"]
}

host moon {
  backend = "ssh"
  alias = "moon"
  gpu_count = 2
  gpu_mem_gb = 48
  tags = ["shared", "large-mem", "txstate"]
}
```

### 资源组

```text
pool txstate_shared {
  hosts = ["sun", "moon", "gauss", "markov"]
  backends = ["ssh"]
}
```

### 运营策略

```text
policy shared_half {
  hosts = ["sun", "moon"]
  max_active_fraction = 0.5
}
```

### 可抢占云资源池

```text
pool aws_spot_train {
  backend = "ec2-fleet"
  market = "spot"
  preemptible = true
  allocation_strategy = "price-capacity-optimized"
  rebalance_signal = true
}
```

### 带显式 requirement / preference 的任务模板

```text
experiment train_large {
  use_pool = "txstate_shared"

  requires {
    gpu_count >= 4
    backend in ["ssh", "slurm"]
    host_tags contains "multi-gpu"
  }

  prefers {
    host_tags contains "a100"
    avoid_host_tags = ["shared"]
    placement = "spread"
  }

  env {
    OMP_NUM_THREADS = "16"
  }

  checkpoint {
    path = "s3://my-bucket/checkpoints/${task}"
    interval = "5m"
    on_rebalance = "save"
    on_interruption = "save"
  }

  preemption {
    tolerates_preemption = true
    on_rebalance = "checkpoint-and-requeue"
    on_interruption = "checkpoint-and-exit"
  }

  resume {
    mode = "resume-if-possible"
    requeue_on_preemption = true
  }

  run = """
bash -lc "python train.py --model big"
"""
}
```

### 矩阵展开

```text
experiment vlmlp_grid {
  use_pool = "txstate_shared"

  matrix {
    dataset = ["ETTh2", "ETTm2"]
    pred_len = [96, 192, 336, 720]
    seed = [1, 2, 3]
  }

  requires {
    gpu_count >= 1
  }

  prefers {
    placement = "spread"
  }

  checkpoint {
    path = "s3://my-bucket/vlmlp/${dataset}/${pred_len}/${seed}"
    interval = "10m"
  }

  resume {
    mode = "resume"
    requeue_on_preemption = true
  }

  run = """
bash -lc "uv run python run_experiment.py ${dataset} ${pred_len} --seed ${seed}"
"""
}
```

## 一个务实的演化路线

我会建议这门语言按阶段长，而不是一口气做成“大而全的调度语言”。

### V0：当前原型

- `pool`
- `policy`
- `experiment`
- `matrix`
- 编译到现有 YAML runtime

### V1：typed requirements

- 加 `requires { ... }`
- 引入显式资源字段，比如 `gpu_count`、`gpu_mem_gb`、`backend`
- 加更强的合法性检查
- 能明确报告 unschedulable task

### V2：preferences 和 explainability

- 加 `prefers { ... }`
- placement 排序与 tie-break
- `explain` 输出
- 更强的 pool 组合能力

### V3：preemption-aware execution

- 加 `checkpoint { ... }`
- 加 `preemption { ... }`
- 加 `resume { ... }`
- compile-time report 能区分 `ready`、`unschedulable`、`needs-multi-slot-runtime`
- 为 Spot 这类资源生成 drain / recovery 计划

### V4：全局策略

- `strategy = "greedy" | "spread" | "pack" | "fair-share"`
- reservation / anti-affinity
- 更复杂的 multi-slot / multi-host placement 语义

## 可解释性

语言一复杂，用户最需要的就不只是“结果”，而是“原因”。

例如：

```text
task train_large is unschedulable:
- requires gpu_count >= 4
- requires host_tags contains "multi-gpu"
- no host in pool txstate_shared satisfies both constraints
```

或者：

```text
task vlmlp_ETTh2_96_s1 scheduled on sun-g2:
- satisfies backend = ssh
- satisfies gpu_count >= 1
- matches preferred tag txstate
- moon is deprioritized by shared-host policy
```

```text
task train_large on spot-worker-17 is draining:
- pool market = spot
- rebalance recommendation received
- checkpoint path = s3://my-bucket/checkpoints/train_large
- action = checkpoint-and-requeue
```

如果没有 explainability，这门语言越强，反而越难让人信任。

## 设计原则

我觉得这门语言最核心的一条原则应该是：

> 用 DSL 描述意图，让 runtime 保持简单

也就是说：

- DSL 负责描述资源、任务需求、调度偏好、主机策略
- `slot-scheduler` runtime 继续负责透明执行

这样做的好处是：

- 语言可以不断变强
- runtime 仍然容易理解、容易 debug
- 不会把整个项目一下子拖成一个过重的平台
