FROM python:3.11.9-slim

WORKDIR umbrella0.4
COPY . .

RUN python3 -m venv .venv \
    && .venv/bin/pip install --no-cache-dir --disable-pip-version-check -r runtime/requirements-tools.txt

ENV PATH=".venv/bin:scripts:scripts/control-plane:scripts/tools:${PATH}"

EXPOSE 8791 8792 8793 8794 8795 8796 8797 8798

CMD ["./scripts/control-plane/manage-service-mesh", "bringup", "--umbrella-root", "."]
