# Python 通用 gm/Id 工具

代码与 ADO 同包，支持本地桌面界面、JSON CLI、Python API、scipy/TuRBO objective 和 campaign proposer。无需 MATLAB。查表估算不代表电路仿真通过。

## 启动

使用 Python >=3.10，在自己的环境安装：

```sh
python -m pip install -e ".[gmid]"
gmid-gui
gmid-lookup examples/gmid/synthetic.csv examples/gmid/size.json
```

桌面界面需要 Python 的 tkinter 支持。Windows 也可运行 `scripts/gmid-gui.ps1 -Python <你的Python路径>`。示例表全部由简单公式生成，不对应任何真实工艺。

## 统一数据格式 v1

UTF-8 CSV，每行一个 DC 工作点，必须有以下列（顺序不限）：

| 列 | 含义/单位 |
|---|---|
| model, corner | 任意非空器件和 corner 名称；精确匹配 |
| temp_c | 摄氏度；离散选择，不做温度插值 |
| l, w_ref | 长度、仿真参考总宽度，m |
| vgs, vds, vsb | 归一化偏置，V |
| id, gm, gds, cgg | A、S、S、F；id/gm/cgg 必须正，gds 可为零 |

NMOS/PMOS 都采用电流、跨导、电容的正幅值；PMOS 用源极参考的正导通电压幅值，vsb 的方向由数据提供方统一。工具不猜测符号。可添加任意有限数值列，例如 cgd、cdd、vdsat。额外列的单位由提供方约定。

每个 (model, corner, temp_c) 是独立的 l × vds × vsb × vgs 完整矩形网格；各轴可不等距，也可只有一个值。缺点、重复点、NaN/Inf、越界查询直接报错。不同器件可以使用不同网格。不要把未仿真的组合补零。

查询对原始物理量做多线性插值，再计算 gm_id=gm/id、id_w=id/w_ref、ft=gm/(2πcgg)、gm_gds=gm/gds（gds=0 时省略）。反查沿 VGS 求解 gm-target*id=0；多个解需要指定零起始 branch。平台区间拒绝反查。尺寸 W=current/id_w，假设电流随宽度线性缩放；短窄沟道、指数量化等需后续电路验证。这里的 ft 是由 gm/Cgg 得到的近似值。

仅有原来的 MAT 数组无法可靠恢复偏置和单位，不能自动把它当作通用表。

## 旧 MATLAB CSV 的导入

旧程序写死了行列裁剪、长度列表和符号翻转。新导入器使用显式本地 JSON 映射，不沿用这些工艺常数。每个 series 对应一个固定 l/vds/vsb 的 VGS 扫描。下面全是示意值：

```json
{"series": [{
  "constants": {"model":"demo","corner":"tt","temp_c":27,"l":0.000001,"vds":1,"vsb":0,"w_ref":0.000001},
  "sources": {
    "id_w":{"path":"density.csv","x_column":0,"y_column":1,"skip_rows":1,"scale":1,"x_scale":1,"magnitude":true},
    "gm_id":{"path":"efficiency.csv","x_column":0,"y_column":1,"skip_rows":1},
    "gm_gds":{"path":"gain.csv","x_column":0,"y_column":1,"skip_rows":1},
    "ft":{"path":"frequency.csv","x_column":0,"y_column":1,"skip_rows":1,"magnitude":true}
  }
}]}
```

列索引从零起，scale/x_scale 把原单位换成 SI，x_magnitude 可对 PMOS 的 VGS 取幅值。每个源的 VGS 向量必须对齐。也可直接映射 id/gm/gds/cgg。由比值恢复物理量时，id_w 的单位必须是 A/m，ft 必须是 Hz。映射文件、原表、转换结果全部放本地私有目录。输出文件必须尚不存在。

## Python API 与优化器

```python
from analog_agent.gmid import LookupTable, SizingObjective
from scipy.optimize import minimize

lut = LookupTable('private/gmid/table.csv')
bias = dict(model='demo', corner='tt', temp_c=27, vds=1, vsb=0)
row = lut.query(**bias, l=1e-6, vgs=0.4)
size = lut.size(**bias, l=1e-6, gm_id=20, current=40e-6)
# x = [gm_id, L(m), Id(A)]；设计域和 loss 由项目定义。
objective = SizingObjective(lut, bias, lambda r: r['width'])
# scipy/TuRBO 可直接调用 objective(x)。越界返回有限惩罚，默认 1e12。
```

所有优化边界必须处于已表征数据域内。优化值是查表估算，必须作为 ADO 新 run 的参数交给 netlist -> simulation -> analysis，而非把它登记成 VERIFIED。

## ADO 接线

公开 project.json 只指定：

```json
{"campaign":{"proposer":"analog_agent.proposers.gmid:propose"}}
```

项目 `private/global/config/overrides.json` 中合并以下配置（示例只含虚构值）：

```json
{"campaign":{"gmid":{
  "table":"private/gmid/table.csv",
  "outputs":{"width":"W_in","l":"L_in"},
  "requests":[{"sizing":{"model":"demo","corner":"tt","temp_c":27,"l":0.000001,"vds":1,"vsb":0,"gm_id":20,"current":0.00004},"parameters":{}}]
}}}
```

W_in/L_in 必须已在项目 parameters 声明。outputs 把 size 返回字段映射为项目参数；其余参数由 parameters 或项目 initial 提供。ADO 的 `campaign propose CAMPAIGN_DIR` 会加载插件、生成候选 JSON、验证参数及预算，并保留 review_required。再按原有 campaign 审阅和三角色交接执行。该 proposer 可独立提供 gm/Id 尺寸种子。若直接使用现有 MOBO，保持 campaign.proposer 为 analog_agent.proposers.mobo:propose，并设置 campaign.bo.gmid_seeds=true；同时按原 MOBO 规范配置至少两个 metric_directions、ref_point 和预算。MOBO 冷启动自动把私有 gm/Id 请求作为 seed_points，仍按参数边界和已有历史去重；热启动继续使用原 GP/acquisition。查表预测不会作为实测历史。SizingObjective 则提供直接驱动 scipy/TuRBO 的独立接口。

## 保密约束

真实工艺数据只存项目 private/（现有 .gitignore 已忽略）。ADO proposer 强制要求表位于 private/，返回值只含候选参数，不输出原始表、器件名或偏置。候选尺寸及报告也可能是敏感派生数据，应留在忽略的 campaigns/runs/output/private 目录。不要用 git add -f 绕过规则。代码不会调用网络、GitHub、git commit 或 git push。版本库示例和测试只允许合成数据。

## 单工况表与缺失条件

不要求扫描完整PVT。一个明确的corner、一个明确的温度、一个VSB值就能作为独立表使用；查询必须精确匹配corner和温度，固定轴也禁止外推。未扫描的条件不能补零或冒充已表征条件。

若来源根本未记录temp_c/vds/vsb，可将对应列整列留空；查询时省略它，结果含missing_context。禁止指定该未知轴的值。corner可用明确的unspecified标签，但不能理解为TT。ADO proposer默认拒绝缺失条件；allow_incomplete_context仅用于明确的本地兼容验证，不用于把未知条件送入正式优化。

导入器的stop_row是Python零起始、末端不含的切片边界。比值恢复的id/gm/cgg是归一化重建量，不代表原始仿真的绝对测量值。

## 公开发布边界

只公开通用代码、格式说明和合成示例。真实表、模型、映射清单、PDK参数范围、仿真激励、模型路径、服务器信息及派生结果留在项目private/。不同M/nf/wf的数据应由私有配置明确选择切片；核心表不自动猜测工艺几何语义。

发布前按[公开发布检查](public-release.md)检查Git内容和实际wheel/sdist。忽略规则不能清除已提交的历史，不能替代逐项审阅。

## 验证

```sh
python -m unittest discover -s tests -p test_gmid.py -v
```

测试仅用合成表，覆盖正反查、尺寸估算、单工况边界、未知条件、多根、导入、私有路径限制和ADO/MOBO初值入口。软件测试不替代使用者对真实表单位与来源的核验。
