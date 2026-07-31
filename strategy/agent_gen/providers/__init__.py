"""代码生成 provider 实现。

本目录下**非下划线开头**的 .py 会被 strategy.agent_gen.registry 自动发现；
每个文件恰好一个 CodegenProvider 子类，且 key == 文件名。

加一个新 provider 的完整清单见 #30「九」：抄 _template.py，填元数据与
start/resume，依赖加进 requirements.txt，完——不需要改注册表、orchestrator、
闸门、前端。
"""
