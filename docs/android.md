# Android

Android Chrome is the primary phone for the launcher. iOS Safari is still supported (see [iphone-debugging.md](iphone-debugging.md)); nothing in the page is Android-only. Everything marked *to be verified on device* is expected from the page's code and has not been observed on an Android handset.

## Install the PWA

1. Open the launcher's HTTPS URL in Chrome on the phone (the Tailscale name, or the VPN name set up under [Remote access](../README.md#remote-access--tailscale-or-any-vpn)).
2. Sign in with the bearer token or the password.
3. Chrome menu (the three dots), then **Install app** (or **Add to Home screen** on versions that offer only that). The launcher then opens full screen from its own icon.

The page declares itself an installable app in `app/webapp/static/manifest.webmanifest`: `display: standalone`, `start_url: /`, a dark `background_color` and `theme_color`, and 192 and 512 pixel icons plus a 512 pixel maskable icon so Android can shape the launcher icon. Installing on a real handset is to be verified on device.

## HTTPS is required

Chrome offers installation, and grants a service worker, WebAuthn passkeys and clipboard access, only on a secure origin. Serve the launcher over HTTPS with a certificate the phone trusts: `scripts/gen_tailscale_cert.py` provisions one for a Tailscale MagicDNS name, or drop any trusted `cert.pem` and `key.pem` pair into `webapp/certificates/`. A self-signed certificate the phone does not trust blocks install and the passkey gate. The passkey relying party (`webauthn_rp_id`) must be the exact hostname the phone browses to.

## What to check on a device

The viewport meta in `app/webapp/static/index.html` is `width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content`. That asks the browser to draw edge to edge and to resize the layout when the keyboard opens. The checks below confirm it behaves that way on Android; each is to be verified on device.

- **Safe areas and the gesture bar.** With `viewport-fit=cover` the page extends under the status bar and the gesture bar. Headers and the bottom controls must stay clear of both, in portrait and landscape, installed and in a Chrome tab.
- **Bottom navigation above system navigation.** The floating bottom-tab pill must sit above the gesture bar or the three-button bar and stay tappable, with no gap that shows content scrolling underneath.
- **Keyboard not covering the chat bar.** With `interactive-widget=resizes-content` the layout shrinks when the keyboard opens, so the terminal compose bar and the reply box stay above it. Check the compose bar in a live session and the drill-down drawer's reply field on the Board.
- **Terminal input.** Typing, paste, Ctrl-keys and image attach in the compose bar; Android keyboards with predictive text are the usual source of surprises.
- **Rotation and resume.** Rotate the phone and background the app for a minute; the terminal should reconnect and replay its scrollback.

Report a failure with the Chrome version, the Android version and whether the app ran installed or in a tab.
