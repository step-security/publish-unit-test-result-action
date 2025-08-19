FROM python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844

LABEL repository="https://github.com/step-security/publish-unit-test-result-action"
LABEL homepage="https://github.com/step-security/publish-unit-test-result-action"

LABEL com.github.actions.name="Publish Test Results"
LABEL com.github.actions.description="A GitHub Action to publish test results."
LABEL com.github.actions.icon="check-circle"
LABEL com.github.actions.color="green"

RUN apk add --no-cache --upgrade expat libuuid

COPY python/requirements-post-3.8.txt /action/requirements.txt
RUN apk add --no-cache build-base libffi-dev; \
    pip install --upgrade --force --no-cache-dir pip && \
    pip install --upgrade --force --no-cache-dir -r /action/requirements.txt; \
    apk del build-base libffi-dev

COPY python/publish /action/publish
COPY python/publish_test_results.py /action/

ENTRYPOINT ["python", "/action/publish_test_results.py"]
