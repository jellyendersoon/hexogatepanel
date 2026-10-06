# Reserve-start sidecar: activates reserved (on_hold) services at first connect
# and handles early activation requests. Built on the Hexogate panel image, so
# it shares the panel's code (/code/app, config.py, role.py) and dependencies.
FROM ghcr.io/jellyendersoon/hexogatepanel:latest

COPY --chmod=0755 reserve_start /opt/reserve-start/reserve_start
RUN find /opt/reserve-start -type f -exec chmod 0644 {} +

ENV PYTHONPATH=/opt/reserve-start:/code \
    PYTHONDONTWRITEBYTECODE=1 \
    RESERVE_STATE_DIR=/state
WORKDIR /code
USER 65532:65532
ENTRYPOINT ["python", "-m", "uvicorn", "reserve_start.server:app", "--uds", "/var/lib/pasarguard/reserve-start/reserve-start.sock", "--workers", "1", "--no-access-log"]
