# Auto Screen Monitoring (Android Companion)

## What changed
- Added an **Auto Screen Monitoring** button.
- It reads the previously saved HTTPS service URL and pairing key from the app's private preferences; it does not ask the user to type them again.
- Added a Platform account label. The account name is received from the Government AI Platform through the `govmonitor://monitor` Android deep link and is included in the `/v1/heartbeat` payload as `login_user`.
- Kept the manual setup/start flow available.

## First-time setup
1. Open the app once and enter the HTTPS monitoring service URL and pairing key.
2. Start sharing once (or use the normal Start Screen Sharing button) so the app securely saves the settings in its private preferences.
3. Later, use **Auto Screen Monitoring**; the saved URL and key are reused.

## Platform account hand-off
The authenticated Platform page should launch this URI when the user taps its Android monitoring action:

`govmonitor://monitor?login_user=<URL-ENCODED-PLATFORM-USERNAME>&service_url=<URL-ENCODED-HTTPS-SERVICE-URL>`

The username is display metadata, not an authentication credential. The pairing key is deliberately not placed in the deep link. The app continues to require the Android system MediaProjection consent dialog before screen capture starts; Android does not permit silent screen capture.

## Server compatibility
The companion sends `login_user` in the existing `/v1/heartbeat` request. The provided mobile monitor API already accepts this field in its Heartbeat model and returns it from `/v1/devices`.
