# 本地开发/验证入口。没有 CI —— 所有检查都在这台机器上跑得起来。
.PHONY: help install test lint smoke fake-up fake-down e2e all

PY ?= python3
FAKE_ADDR ?= 127.0.0.1:9999
export XSTECH_BASE ?= http://$(FAKE_ADDR)

help:
	@echo "make install   安装依赖（含开发依赖）"
	@echo "make test      离线单测"
	@echo "make smoke     语法检查 + CLI 冒烟（两个入参，都不联网）"
	@echo "make fake-up   后台起本地假上游 → $(FAKE_ADDR)"
	@echo "make e2e       假上游 + 真 HTTP 端点，端到端跑一遍（流式/非流式）"
	@echo "make all       以上全套"

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

smoke:
	$(PY) -m compileall -q src tests
	$(PY) -m src.cli --version
	$(PY) -m src.cli --help >/dev/null

fake-up:
	@$(PY) tools/fake_upstream.py --host $(word 1,$(subst :, ,$(FAKE_ADDR))) \
		--port $(word 2,$(subst :, ,$(FAKE_ADDR))) --detach
	@sleep 0.5
	@curl -sf http://$(FAKE_ADDR)/api/site/info >/dev/null && echo "假上游已就绪: http://$(FAKE_ADDR)"

fake-down:
	@pkill -f tools/fake_upstream.py || true

e2e:
	XSTECH_CONFIG_DIR=$$(mktemp -d) $(PY) tools/e2e_local.py

all: smoke test e2e
