FROM pasarguard/panel@sha256:019bd43749e334b08d01eff39ab4530f8a18f5c34348b8998dfb3dad3f93e9bb

# The build context must contain a sanitized source-only snapshot from the
# running panel. No .env, credentials, live database, or /var/lib is copied.
COPY --chmod=0755 native-runtime/app /code/app
COPY --chmod=0644 native-runtime/config.py native-runtime/role.py /code/
COPY --chmod=0755 reserve_start /opt/reserve-start/reserve_start
RUN find /code/app /opt/reserve-start -type f -exec chmod 0644 {} +

ENV PYTHONPATH=/opt/reserve-start:/code \
    PYTHONDONTWRITEBYTECODE=1 \
    RESERVE_STATE_DIR=/state
WORKDIR /code
USER 65532:65532
ENTRYPOINT ["python", "-m", "uvicorn", "reserve_start.server:app", "--uds", "/var/lib/pasarguard/reserve-start/reserve-start.sock", "--workers", "1", "--no-access-log"]
