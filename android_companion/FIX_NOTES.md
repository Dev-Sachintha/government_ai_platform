# What was fixed in the Android companion app

**Problem:** the dashboard always said "No Android phones are connected".

**Cause:** the screen-capture loop (`MonitorService`) ran on the Android *main thread*. Android forbids network
calls there (`NetworkOnMainThreadException`) and the code silently swallowed that error, so no heartbeat or
screen picture was ever sent to the server.

**Fix:**
* capture + network now run on a background `HandlerThread`;
* the HTTP result is checked and shown on the app's main screen (**Status:** line);
* new **TEST CONNECTION** button tells you if the URL or the pairing key is wrong;
* longer timeouts (a free Render service needs up to ~1 minute to wake up);
* asks for the notification permission on Android 13+ (the "screen sharing is active" notice);
* `minSdk` raised to 26 (the notification code needs it).

**You must rebuild the app:** Android Studio > Build > Build Bundle(s)/APK(s) > Build APK(s), then install the new APK.
