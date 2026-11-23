# Android

Android Chrome is the primary phone for the launcher. iOS Safari is still supported (see [iphone-debugging.md](iphone-debugging.md)); nothing in the page is Android-only. The owner ran every check under [What to check on a device](#what-to-check-on-a-device) on an Android phone and reported that all passed (2026-11-21). The Chrome and Android versions were not given, so they are recorded as *not given*; nothing here names a version it was not told.

## Install the PWA

1. Open the launcher's HTTPS URL in Chrome on the phone (the Tailscale name, or the VPN name set up under [Remote access](../README.md#remote-access--tailscale-or-any-vpn)).
2. Sign in with the bearer token or the password.
3. Chrome menu (the three dots), then **Install app** (or **Add to Home screen** on versions that offer only that). The launcher then opens full screen from its own icon.

The page declares itself an installable app in `app/webapp/static/manifest.webmanifest`: `display: standalone`, `start_url: /`, a dark `background_color` and `theme_color`, and 192 and 512 pixel icons plus a 512 pixel maskable icon so Android can shape the launcher icon. Installing on a handset: passed on device (owner, 2026-11-21); versions not given.

## A managed phone: full screen without installing

On a phone whose browser the organisation manages (for example Edge in an Android work profile), the browser may refuse to install Lite as an app, and Lite then shows the browser's address bar and toolbar. Three options, from easiest to most involved:

1. **Full screen.** Settings, Appearance, **Full screen on Edge** (the fleet's shared component, so every app has the same row). It appears only in Edge, and asks the browser for full screen, which hides the address bar and toolbar, with no install and no policy change. The choice is kept on that device. A browser grants full screen only to a tap, so after a reload the switch stays on and the first tap anywhere goes full screen again. The back gesture leaves full screen without turning the setting off (the next tap goes back in); the switch is the one way to turn it off.
2. **A home-screen shortcut.** In the browser's menu choose **Add to Home screen** (or **Add to phone**). That is a shortcut to the page, not an installed app, so it needs no trusted certificate; whether it opens without the toolbar depends on the browser.
3. **Trust Lite's certificate in the work profile.** A browser installs a web app only from a secure origin with a certificate the device trusts, and a self-signed certificate is marked not secure. If the device policy lets the profile add a certificate authority, trusting Lite's certificate there lets the browser install Lite as an app. Where the policy forbids it, use option 1 or 2, or serve Lite with a certificate the device already trusts (see [HTTPS is required](#https-is-required)). Lite's certificate setup is not changed by any of this.

Settings, Appearance, **Install as app** reports where the browser stands: **Installed** (already running as an app), **Ready** (the browser offers to install it), **Blocked** (the page is not on an HTTPS address), **Unavailable** (the browser has no install prompt for web apps) or **Not offered**. A page cannot read whether its certificate is trusted, so Not offered names the usual cause, an untrusted certificate, without claiming it.

## HTTPS is required

Chrome offers installation, and grants a service worker, WebAuthn passkeys and clipboard access, only on a secure origin. Serve the launcher over HTTPS with a certificate the phone trusts: `scripts/gen_tailscale_cert.py` provisions one for a Tailscale MagicDNS name, or drop any trusted `cert.pem` and `key.pem` pair into `webapp/certificates/`. A self-signed certificate the phone does not trust blocks install and the passkey gate, but not the microphone: Settings > Connections > **Serve over** can create one, and once the phone's browser is past its warning (Advanced, Proceed) the page is a secure origin and dictation records. Over plain HTTP the record button says that HTTPS is needed. The passkey relying party (`webauthn_rp_id`) must be the exact hostname the phone browses to.

## What to check on a device

The viewport meta in `app/webapp/static/index.html` is `width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content`. That asks the browser to draw edge to edge and to resize the layout when the keyboard opens. The checks below confirm it behaves that way on Android. Each one is recorded as *passed on device (owner, 2026-11-21)*; Chrome version and Android version: not given.

- **Safe areas and the gesture bar.** With `viewport-fit=cover` the page extends under the status bar and the gesture bar. Headers and the bottom controls must stay clear of both, in portrait and landscape, installed and in a Chrome tab. *Passed on device (owner, 2026-11-21).*
- **Bottom navigation above system navigation.** The floating bottom-tab pill must sit above the gesture bar or the three-button bar and stay tappable, with no gap that shows content scrolling underneath. *Passed on device (owner, 2026-11-21).*
- **Keyboard not covering the chat bar.** With `interactive-widget=resizes-content` the layout shrinks when the keyboard opens, so the terminal compose bar and the reply box stay above it. Check the compose bar in a live session and the drill-down drawer's reply field on the Board. *Passed on device (owner, 2026-11-21).*
- **Terminal input.** Typing, paste, Ctrl-keys and image attach in the compose bar; Android keyboards with predictive text are the usual source of surprises. *Passed on device (owner, 2026-11-21).*
- **Rotation and resume.** Rotate the phone and background the app for a minute; the terminal should reconnect and replay its scrollback. *Passed on device (owner, 2026-11-21).*

The owner's report covered the Android Chrome checks only. The Git client flow in [gitlab.md](gitlab.md#working-from-an-android-git-client) was not reported and stays `unknown`.

Report a failure with the Chrome version, the Android version and whether the app ran installed or in a tab.
