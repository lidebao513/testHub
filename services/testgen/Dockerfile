# testgen sidecar 镜像（容器化部署用）
# 仅内部网络可达：compose 用 expose 而非 ports，不映射宿主机端口，绝不公网暴露。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖层（利用缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 复制业务代码（注意：.env / .venv / data 已被 .dockerignore 或 .gitignore 排除）
COPY . .

# 凭证经 env_file 注入（.env 不进镜像）；默认监听内部网络
EXPOSE 8100

# 真实入口：cli.main serve；容器内监听 0.0.0.0 仅在 compose 内部网络可达
CMD ["python", "-m", "cli.main", "serve", "--host", "0.0.0.0", "--port", "8100"]
