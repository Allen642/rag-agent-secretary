# 项目约定

- 运行测试：`.\.venv\Scripts\python.exe -m pytest`
- 启动 Web：`.\.venv\Scripts\streamlit.exe run app.py`
- 启动 CLI：`.\.venv\Scripts\python.exe chat.py`
- 不要修改：`data/docs/`、`data/runtime/`、`.env`、`pack/*.zip`
- 保持单机单进程可运行，不引入 Redis/Celery/Docker
- 新依赖必须说明理由，并更新 `requirements.txt`
- 新模块要有中文 docstring，并在 `tests/` 下补对应测试
- 不要重写现有模块，优先增量修改
- 改完必须跑 pytest，并贴出真实输出
