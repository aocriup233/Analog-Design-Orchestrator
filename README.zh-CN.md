# Analog Design Orchestrator（ADO）

**面向模拟集成电路设计、仿真与分析的可追溯工作流。**

[English](README.md)

ADO 把电路决策与 EDA 环境操作分开：用户或外部 LLM 控制器提出电路版本，三个独立进程分别准备网表、执行配置好的仿真、测量结果。每一步都在独立的 run 目录留下持久化交接文件与哈希。通用引擎不内置 CTLE 拓扑、工艺库、集群地址或某种电路的专用公式。

为兼容现有工程，Python 包和命令仍叫 `analog-agent`；**ADO 是项目正式名称，不是新的 CLI 命令。**

## 已实现的能力与边界

- 设计网表 `input.scs` 与仿真 agent 管理的 `simulation.scs` 分离；DC、AC、瞬态分析通过可复用规则文件配置。
- Bridge、本地 Spectre，以及异步 LSF 的**后端接口**。LSF 必须由用户实现私有站点适配器；仓库提供的是接口模板，不是已经连通的集群配置。
- 持久化状态 `CREATED → NETLIST_READY → STAGED → SUBMITTED → RUN → DONE → RETRIEVED → VERIFIED → ANALYZED`。短作业可能跳过 `RUN`，明确失败进入 `FAILED`。`resume` 每次轮询一次；作业完成后继续取回与校验。
- 通用波形分窗、重叠、取点、参考线对照、眼图及 FFT；电路专属指标由项目插件提供。
- 公用配置与用户私有的配置、脚本、skill 分层。

当前三个 worker 是确定性的 Python 子进程，**不是内置的三个自主 LLM**。外部 LLM 可以根据交接结果决定下一版电路；自动更新专家知识和“适用于所有集群的现成适配器”尚未实现。

## 安装与首次试运行

使用 Python 3.10+，并选择能够运行目标仿真器的环境。Cadence Bridge 模式需要环境中已有 `virtuoso_bridge`；ADO 不提供 Spectre、Virtuoso、PDK 或许可证。在本仓库目录安装：

```sh
python -m pip install -e .
# 绘图功能可选：python -m pip install -e '.[analysis]'
```

`project.example.json` 是 RC 冒烟测试，里面的路径相对于**本仓库**。只验证网表生成、不启动仿真器时：

```sh
cp project.example.json project.json
analog-agent new project.json
```

命令会返回 `runs/<run_id>` 路径。尚未配置仿真器或私有站点适配器前，不要执行 `submit`。独立的用户工程应把自己的 `project.json`、设计模板或生成器、分析规则和 `private/` 放在同一项目根目录；从示例复制配置后，必须修改所有相对路径。

## 用户究竟需要配置什么

| 范围 | 用户填写的内容 | 位置 |
|---|---|---|
| 电路项目 | 设计模板**或** `netlist.generator`、合法参数与候选值、分析类型、保存信号、指标插件和由用户定义的判定规则 | `<project>/project.json`、模板及规则/插件文件 |
| 私有全局环境 | 仿真器路径或 Bridge profile；若用 LSF，还需主机与传输拓扑、队列及资源、远端目录、PDK 模型路径和 section | `<project>/private/global/config/` 与私有复用脚本 |
| 网表角色 | 工艺合法器件、引脚顺序、模型 include 和电路专属生成器 | `<project>/private/agents/netlist/` |
| 仿真角色 | DC/AC/tran/自定义规则、执行后端与站点适配器、轮询/回传/清理策略 | `<project>/private/agents/simulation/` 及 `simulation` 配置 |
| 分析角色 | 电路专属测量函数与阈值、可选波形操作请求 | `<project>/private/agents/analysis/` 及 `metrics`/`rules` 配置 |

可参照 `private.example/` 的文件形状。四个可选的 `private/**/config/overrides.json` 会在运行时覆盖公用配置；其内容不复制到 run 的 `config.json`，run 只记录这些文件的 SHA-256，配置中途改变会拒绝续跑。示例中的 `pdk.json`、`transfer.json` 等其他私有文件**不会被核心自动读取**，需要用户的网表生成器或站点适配器主动读取。密码只应通过交互提示或操作系统密钥设施取得，不能写入配置、命令参数、运行产物或仓库。若用户工程单独使用 Git，也应忽略其 `private/` 与 `runs/`。

远端站点建议依次配置并验证：（1）登录链路及实际远端身份、项目目录；（2）PDK/模型可见性及网表器件、include 映射；（3）带哈希校验的上传和下载；（4）LSF 队列、资源、job ID 与状态轮询；（5）Spectre 命令、模式、输出目录和成功判据；（6）结果回传、指标解析和验证后的清理。对于 SSH→Telnet→LSF 站点，这些步骤由私有适配器及复用脚本实现。每个关键步骤应保存实际输出，不能只凭命令退出就推断登录或仿真成功。

### 选择仿真后端

| 后端 | 用户需提前完成 | 运行方式 |
|---|---|---|
| `bridge` | 可用的 Virtuoso Bridge 环境/profile 与 Spectre 访问 | 同步提交并解析校验结果 |
| `local` | 本地 Spectre 可执行文件（`simulation.spectre_cmd`） | 同步提交并解析校验结果 |
| `lsf` | 在 `simulation.adapter` 指定私有工厂函数，实现六个站点方法 | 异步提交；每次 `resume` 轮询一次 |

LSF 用户可以在私有 `overrides.json` 中选择后端，不公开站点细节：

```json
{
  "simulation": {
    "backend": "lsf",
    "adapter": "private/agents/simulation/scripts/site_lsf_adapter.py:create",
    "remote_cleanup": "manual"
  }
}
```

复制[适配器模板](private.example/agents/simulation/scripts/site_lsf_adapter.py)，用本站可复用函数实现 `stage`、`submit`、`poll`、`retrieve`、`verify`、`cleanup`。其中 `stage` 要确认仿真 deck 哈希，`submit` 返回真实 job ID，`poll` 区分已提交/RUN/DONE/失败，`retrieve` 给出本地文件及 SHA-256（提供 `remote_sha256` 才能进行两端对照），`verify` 同时核实调度器和仿真器成功并返回解析后的数据。建议以 run ID 作为远端幂等键，避免“已提交但本地交接尚未落盘”时的重试产生重复作业。通用核心会核对状态及本地文件，并在提供远端哈希时进行对照；但不能替未实现的适配器证明登录或传输成功。

无论选哪种后端，用户还需选择 `simulation.rules`（可参考公用 [DC](agents/simulation/config/dc.json)、[AC](agents/simulation/config/ac.json)、[tran](agents/simulation/config/tran.json)）、`save_signals`、仿真模式、超时以及 PSF ASCII 输出。安装环境支持的其他分析可使用 Python 规则渲染器。PDK include 应由项目设计网表/生成器或配置的 include 文件提供，不写进通用引擎。

## 运行与断线续跑

```sh
analog-agent new project.json --iteration 0 --set R=2k
analog-agent submit RUN_DIR
analog-agent status RUN_DIR
analog-agent resume RUN_DIR       # 异步 LSF 可重复调用
analog-agent analyze RUN_DIR      # 仅在本地结果验证完成后
```

Bridge/本地模式的 `submit` 一次完成；LSF 模式返回 run，待作业状态变化后再次调用 `resume`。`analog-agent run project.json` 遇到异步提交也会返回，不会无限等待。每个 run 保留任务状态历史、输入哈希、阶段交接、日志或回传产物及分析报告。**同一个 run 应始终在相同的路径环境中继续**；例如创建时记录的是 WSL `/mnt/e/...`，不能中途改用 Windows `E:\...` 路径续跑。

`workflow.submission` 为 `manual` 或 `auto`；`return_mode` 为精简 `summary` 或完整解析数据；`cleanup` 为 `never` 或通过分析后清理本地 raw 的 `after_success`。`simulation.remote_cleanup` 可选 `never`、`manual`、`after_verified`；`cleanup-remote RUN_DIR` 只在本地验证后调用私有后端。队列接受任务或工具命令退出，都不能单独证明仿真成功。

### 可续跑的并行设计会话

用户或分析 AI 决定扫参点时，可在 `project.json` 增加可选的 `campaign` 配置。以下仅是**执行额度**，不是电路初值或性能标准：

```json
{
  "campaign": {
    "max_parallel": 2,
    "max_points_per_cycle": 16,
    "max_cycles": 10,
    "max_total_points": 100,
    "poll_interval_s": 10,
    "metric_directions": {"gain_1g_db": "max"}
  }
}
```

创建会话，审核候选点文件（例如 [RC 示例](examples/campaign_points.json)），再执行一轮：

```sh
analog-agent campaign new project.json
analog-agent campaign add CAMPAIGN_DIR examples/campaign_points.json
analog-agent campaign run CAMPAIGN_DIR --max-seconds 3600
analog-agent campaign status CAMPAIGN_DIR
```

`campaign run` 同时最多运行 `max_parallel` 个点，轮询由本地脚本负责，不必让 LLM 逐个盯作业。每个任务完成后即取回、校验、分析；全部任务分析或失败后才生成本轮对比报告，包含参数、指标、判定、失败信息和可选的 Pareto 集。核心**不内置**评分公式、gₘ/Iᴅ 初值、优化算法或统一 spec。`metric_directions` 仅控制可选的 Pareto 方向；下一轮点位和最终选择由用户及分析 AI 决定。

审核后，可用 `campaign add CAMPAIGN_DIR NEXT_POINTS.json --decision "理由" --select POINT_ID` 开启下一轮，或用 `campaign finish CAMPAIGN_DIR --decision "理由" --select POINT_ID` 收尾。用户可在私有配置中指定 `campaign.proposer`（如 `private/agents/analysis/scripts/site_proposer.py:propose`），通过 `campaign propose CAMPAIGN_DIR` 生成候选建议；审核保存的 JSON 后再明确调用 `add`。仓库的[私有提案模板](private.example/agents/analysis/scripts/site_proposer.py)不包含 PDK 方法或自动优化器。

若聊天因 token 不足而中断，先运行 `campaign doctor CAMPAIGN_DIR` 检查持久化状态，再用 `campaign brief CAMPAIGN_DIR` 获取供 AI 接续的精简摘要；随后 `campaign run` 或 `campaign step` 可脱离旧聊天记录续跑。`campaign pause`/`resume` 暂停或恢复本地调度，不会停止已提交的远端作业。若提交可能已发生但本地没有回执，状态会被标为不确定，**绝不盲目重投**。LSF 私有适配器可选实现 `reconcile(run, config, staged, intent)`，按 run ID 查找真实作业；`analog-agent reconcile RUN_DIR` 接管查证过的回执后，才可用 `campaign retry CAMPAIGN_DIR POINT_ID` 重新启用该步骤。没有查询能力时，须人工核对调度器，不可直接重试。同一轮会冻结公用配置、私有 override 和已声明的模板、规则、插件、include 文件哈希；其他站点依赖可列入 `campaign.dependencies`。应在两轮之间修改电路配置。会话数据放在被忽略的 `<project>/campaigns/`，独立用户工程也应忽略该目录；同一会话必须在相同的 Windows 或 WSL 路径环境中续跑。

若站点必须通过交互式或其他进程外方式执行仿真，可设 `simulation.backend` 为 `external`。相同的 `campaign step/run/doctor` 工作流只生成并暂存带哈希的 deck，返回 `AWAITING_EXECUTION`，不会自行提交。私有站点仿真角色完成传输、提交、取回和验证，写入标准 run 交接文件并达到 `VERIFIED`；下一次 `campaign step` 继续调用分析角色和设计循环。通用核心不包含 SSH、Telnet、LSF 或凭据处理；站点角色必须遵守 run 状态与哈希契约。`external` 是可恢复的协调边界，不代表无人值守仿真。

## 结果、波形与角色边界

网表 worker 负责 `netlist_result.json`；仿真 worker 负责 `simulation_plan.json`、暂存/提交/轮询/回传文件，验证后才写 `simulation_result.json`；分析 worker 负责 `analysis_result.json`。设计网表交接后不可再改动。项目专属指标插件接收解析后的数据并返回数值。`PASS` **仅表示用户配置的 `rules` 通过**，不是某种电路的通用性能保证。

使用 [waveform_tool.py](agents/analysis/scripts/waveform_tool.py) 和 `agents/analysis/config/` 下的 JSON 请求，可进行分窗、重叠、插值取点、参考线、眼图折叠与 FFT。绘图功能需可选的 NumPy、Matplotlib。大量波形保存在文件中；给 LLM 的默认上下文应只包含关键指标、判定、相关图路径和按需截取的数据。

`AGENTS.md` 为各角色指引项目内 skills。公用 skill 与脚本应保持稳定；工艺和电路专属知识放在用户工程中。以后可围绕这些有证据的交接产物增加模型驱动的设计循环和专家知识库，而无需改变确定性的执行契约。

## 验证与发布边界

本地测试命令为 `python -m unittest discover -s tests -q`。测试覆盖同步交接、规则生成、波形工具及**假** LSF 适配器的跨进程续跑；不能代替任何用户的真实集群验收。发布某个站点配置前，应凭该站点实际捕获的输出核对登录身份、PDK 可读性、传输哈希、调度器 job ID/状态、仿真日志、结果回传和清理。

CLI 与交接格式仍在演进（`pyproject.toml` 当前版本为 `0.1.0`）。项目采用 [MIT 许可证](LICENSE)。
