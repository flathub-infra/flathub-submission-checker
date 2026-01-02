FROM python:3.12-alpine AS test

RUN addgroup -S app && \
	adduser -S -G app -h /nonexistent -s /sbin/nologin app

RUN pip install \
	--no-input \
	--disable-pip-version-check \
	--no-color \
	--no-cache-dir \
	--root-user-action=ignore \
	'uv==0.9.21'

WORKDIR /src

RUN chown app:app /src

COPY --chown=app:app . .

USER app

RUN uv sync --all-extras --all-groups --frozen --allow-python-downloads

CMD ["uv", "run", "--no-sync", "pytest", "-vvv"]

FROM python:3.12-alpine AS final

RUN addgroup -S app && \
	adduser -S -G app -h /nonexistent -s /sbin/nologin app

WORKDIR /tmp

COPY pyproject.toml .
COPY flathub_submission_checker ./flathub_submission_checker

RUN pip install \
	--no-input \
	--disable-pip-version-check \
	--no-color \
	--no-cache-dir \
	--root-user-action=ignore .

RUN rm -rf /root/.cache/pip /tmp

USER app

ENTRYPOINT ["flathub-submission-checker"]
