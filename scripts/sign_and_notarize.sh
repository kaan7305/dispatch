#!/usr/bin/env bash
# Sign every nested binary in Dispatch.app inside-out, notarize, and staple.
# Turns the raw PyInstaller output into something a non-technical Mac user can
# double-click without Gatekeeper blocking it. See docs/BUNDLING.md for the
# full story and how to get the required Apple credentials.
#
#   ./scripts/sign_and_notarize.sh
#
# Requires: an Apple Developer ID Application cert in the login keychain, and a
# notarytool keychain profile named "dispatch-notary" (see BUNDLING.md).
set -euo pipefail

APP="dist/Dispatch.app"
ID="${DISPATCH_SIGN_ID:?set DISPATCH_SIGN_ID to your 'Developer ID Application: NAME (TEAMID)'}"
ENTITLEMENTS="build/entitlements.plist"
PROFILE="${DISPATCH_NOTARY_PROFILE:-dispatch-notary}"

[ -d "$APP" ] || { echo "no $APP — run: .venv/bin/pyinstaller Dispatch.spec" >&2; exit 1; }

echo "==> Signing nested binaries inside-out"
# Sign every Mach-O helper (the vendored node, any .dylib/.so, CLI binaries)
# BEFORE the outer bundle. --options runtime = hardened runtime (notarization
# requires it); the entitlements let Node JIT.
find "$APP" -type f \( -name "*.dylib" -o -name "*.so" -o -name "node" \
    -o -name "node.exe" -o -perm +111 \) -print0 |
while IFS= read -r -d '' f; do
  # Skip plain text scripts; sign real Mach-O binaries.
  if file "$f" | grep -q "Mach-O"; then
    codesign --force --timestamp --options runtime \
      --entitlements "$ENTITLEMENTS" --sign "$ID" "$f" 2>/dev/null || true
  fi
done

echo "==> Signing the app bundle"
codesign --force --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" --sign "$ID" "$APP"

echo "==> Verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> Building a DMG"
DMG="dist/Dispatch.dmg"
rm -f "$DMG"
hdiutil create -volname "Dispatch" -srcfolder "$APP" -ov -format UDZO "$DMG"
codesign --force --timestamp --sign "$ID" "$DMG"

echo "==> Notarizing (this uploads to Apple; can take a few minutes)"
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

echo "==> Stapling the ticket"
xcrun stapler staple "$DMG"
xcrun stapler staple "$APP"

echo "Done → $DMG  (ship this)."
