FROM python:3.11.9-slim

WORKDIR /opt/umbrella0.4
COPY . /opt/umbrella0.4

RUN python3 -m venv /opt/umbrella0.4/.venv \
    && /opt/umbrella0.4/.venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /opt/umbrella0.4/runtime/requirements-tools.txt

ENV PATH="/opt/umbrella0.4/.venv/bin:/opt/umbrella0.4/scripts:/opt/umbrella0.4/scripts/control-plane:/opt/umbrella0.4/scripts/tools:${PATH}"

EXPOSE 8791 8792 8793 8794 8795 8796 8797 8798

CMD ["/opt/umbrella0.4/scripts/control-plane/manage-service-mesh", "bringup", "--umbrella-root", "/opt/umbrella0.4"]
