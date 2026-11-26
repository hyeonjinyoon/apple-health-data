# HealthSync iOS app
#   make signing TEAM=XXXXXXXXXX [PREFIX=com.yourname]   one-time: your Apple Developer team
#   make project                                         generate HealthSync.xcodeproj (XcodeGen)
#   make test                                            unit tests on a simulator
#   make device                                          build + install on a connected iPhone
SIM ?= iPhone 17
DERIVED := build/DerivedData
PREFIX ?= com.example

.PHONY: project open build test device signing clean

project:
	@test -f Config/Signing.xcconfig || echo "⚠️  Config/Signing.xcconfig 없음 — 'make signing TEAM=...' 실행 후 실기기 빌드가 가능합니다"
	xcodegen generate --quiet

open: project
	open HealthSync.xcodeproj

build: project
	xcodebuild -project HealthSync.xcodeproj -scheme HealthSync -configuration Debug \
	  -destination 'generic/platform=iOS Simulator' -derivedDataPath $(DERIVED) -quiet build

test: project
	xcodebuild -project HealthSync.xcodeproj -scheme HealthSync \
	  -destination 'platform=iOS Simulator,name=$(SIM)' -derivedDataPath $(DERIVED) -quiet test

# Needs the iPhone connected by cable (or paired over Wi-Fi) with Developer Mode on.
device: project
	xcodebuild -project HealthSync.xcodeproj -scheme HealthSync -configuration Release \
	  -destination 'generic/platform=iOS' -derivedDataPath $(DERIVED) -allowProvisioningUpdates -quiet build
	@xcrun devicectl list devices --json-output build/devices.json >/dev/null; \
	DEV=$$(python3 -c "import json; d=[x['identifier'] for x in json.load(open('build/devices.json'))['result']['devices'] \
	  if x['hardwareProperties'].get('reality') == 'physical' and x['hardwareProperties'].get('platform') == 'iOS']; print(d[0] if d else '')"); \
	if [ -z "$$DEV" ]; then echo "연결된 iPhone이 없습니다 (xcrun devicectl list devices)"; exit 1; fi; \
	xcrun devicectl device install app --device $$DEV $(DERIVED)/Build/Products/Release-iphoneos/HealthSync.app

signing:
	@test -n "$(TEAM)" || (echo "usage: make signing TEAM=XXXXXXXXXX [PREFIX=com.yourname]"; exit 1)
	printf 'DEVELOPMENT_TEAM = $(TEAM)\nBUNDLE_ID_PREFIX = $(PREFIX)\n' > Config/Signing.xcconfig
	@echo "wrote Config/Signing.xcconfig"

clean:
	rm -rf build HealthSync.xcodeproj
