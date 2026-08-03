"""AI 生成自定义交易策略（#30）。

模型侧（provider）与工程侧（契约 / 校验 / 落盘 / 前端）分离：
除 providers/ 外的所有模块都与具体模型无关。

    base.py         CodegenProvider 基类 + Turn
    registry.py     provider 自动发现
    providers/      各家实现
    prompt.py       策略契约 prompt
    extract.py      从回复提取 key + 代码
    orchestrator.py 生成 → 闸门 → 回灌 → 落盘 主循环
    guard.py        闸门 2：AST 静态校验
    smoke.py        闸门 3：子进程冒烟回测
    store.py        _staging → custom 落盘 / 归档 / 删除
"""
