# Apple Health → Mac mini → Grafana
#   make install      서버 설치/업데이트 (ClickHouse 계정·스키마, launchd 서비스, Grafana 프로비저닝)
#   make demo         install + 데모 데이터(health_demo) 적재
#   make qr           iPhone 앱 설정용 QR 코드
#   make status       서비스 상태 / make logs  서버 로그
#   make test         서버 단위+통합 테스트, 대시보드 SQL 검증
#   make ios-test     iOS 단위 테스트 (시뮬레이터)
#   make ios-device   연결된 iPhone에 앱 설치
HOME_DIR := $(HOME)/.local/apple-health
LABEL := local.apple-health.ingest

.PHONY: install demo qr status logs test dashboards check ios-test ios-device uninstall

install:
	python3 deploy/install.py

demo:
	python3 deploy/install.py --demo

qr:
	python3 deploy/show_config.py

status:
	@launchctl print gui/$$(id -u)/$(LABEL) 2>/dev/null | grep -E "^\s+(state|pid) =" || echo "서비스가 등록되어 있지 않습니다 (make install)"
	@curl -s http://127.0.0.1:8787/health && echo

logs:
	tail -f $(HOME_DIR)/logs/ingest.log

test:
	cd server && uv run pytest -q
	cd server && HEALTH_IT_CH_PASSWORD_FILE=$(HOME)/.local/clickhouse/.password uv run pytest -q -m integration
	python3 tools/check_dashboards.py --database health_demo | tail -1

dashboards:
	python3 grafana/build_dashboards.py
	python3 grafana/build_dashboards.py --out $(HOME_DIR)/grafana/dashboards

check:
	python3 tools/check_dashboards.py --database health_demo

ios-test:
	$(MAKE) -C ios test

ios-device:
	$(MAKE) -C ios device

# Stops the service and removes Grafana provisioning. Data (ClickHouse DB `health`) is kept:
#   DROP DATABASE health / health_demo, DROP USER health_writer, health_reader  — to remove it too.
uninstall:
	-launchctl bootout gui/$$(id -u)/$(LABEL)
	rm -f $(HOME)/Library/LaunchAgents/$(LABEL).plist
	rm -f $(HOME)/.local/grafana/provisioning/datasources/apple-health.yaml $(HOME)/.local/grafana/provisioning/dashboards/apple-health.yaml
	brew services restart grafana
	@echo "서비스와 Grafana 프로비저닝을 제거했습니다. 데이터와 $(HOME_DIR) 는 남아 있습니다."
